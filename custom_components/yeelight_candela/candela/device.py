"""Connection handling and state for one Yeelight Candela lamp."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BLEAK_RETRY_EXCEPTIONS,
    BleakClientWithServiceCache,
    establish_connection,
)

from . import protocol
from .esphome_notify import start_notify_without_cccd
from .protocol import FlickerUpdate, StateUpdate

_LOGGER = logging.getLogger(__name__)

IDLE_DISCONNECT_SECONDS = 60.0
RESPONSE_TIMEOUT_SECONDS = 1.0
# Upper bound for one command, including waiting for earlier commands and connecting.
# Within it a command keeps reconnecting until the lamp takes it: some lamps accept
# only a fraction of connection attempts.
COMMAND_TIMEOUT_SECONDS = 90.0
# Attempts per establish_connection round; rounds repeat until the deadline.
CONNECT_MAX_ATTEMPTS = 3
# Pause between failed rounds on a fresh connection.
CONNECT_RETRY_PAUSE_SECONDS = 1.0
# Keep-connected mode: waits before each reconnect attempt; the last one repeats.
RECONNECT_DELAYS_SECONDS = (5.0, 10.0, 30.0, 60.0)

_COMMAND_ERRORS = (BleakError, *BLEAK_RETRY_EXCEPTIONS)

type _Update = StateUpdate | FlickerUpdate


class CandelaError(Exception):
    """Raised when a command cannot be delivered to the lamp."""


class _Superseded(Exception):
    """A newer state-changing command replaced this one before it was sent."""


@dataclass
class CandelaState:
    """Last known lamp state; None means unknown."""

    is_on: bool | None = None
    brightness: int | None = None
    flicker: bool = False


class CandelaDevice:
    """Controls one lamp over an on-demand BLE connection."""

    def __init__(
        self,
        ble_device: BLEDevice,
        *,
        name: str | None = None,
        idle_timeout: float = IDLE_DISCONNECT_SECONDS,
        response_timeout: float | None = None,
        command_timeout: float | None = None,
        retry_pause: float | None = None,
        keep_connected: bool = False,
        reconnect_delays: tuple[float, ...] | None = None,
    ) -> None:
        self._ble_device = ble_device
        self._name = name
        # Keep the link open (no idle disconnect) and restore it in the background
        # when it drops: for lamps that are slow or unreliable to connect to.
        self._keep_connected = keep_connected
        self._reconnect_delays = reconnect_delays or RECONNECT_DELAYS_SECONDS
        self._reconnect_task: asyncio.Task[None] | None = None
        # Set by disconnect() so nothing reopens the link in the background until a
        # caller issues a new command.
        self._closed = False
        # Caller commands in progress (waiting for the lock or retrying).
        self._active_commands = 0
        # Report the next failed background attempt at INFO (once per start()).
        self._report_unreachable = False
        self._idle_timeout = idle_timeout
        self._response_timeout = (
            RESPONSE_TIMEOUT_SECONDS if response_timeout is None else response_timeout
        )
        self._command_timeout = (
            COMMAND_TIMEOUT_SECONDS if command_timeout is None else command_timeout
        )
        self._retry_pause = CONNECT_RETRY_PAUSE_SECONDS if retry_pause is None else retry_pause
        self._client: BleakClientWithServiceCache | None = None
        self._lock = asyncio.Lock()
        self._idle_handle: asyncio.TimerHandle | None = None
        self._idle_task: asyncio.Task[None] | None = None
        self._pending: list[_Update] | None = None
        self._response: asyncio.Event | None = None
        self._callbacks: list[Callable[[], None]] = []
        self._disconnect_callbacks: list[Callable[[], None]] = []
        # Set when a newer state-changing command starts: the older one gives up.
        self._latest_state_command: asyncio.Event | None = None
        self.state = CandelaState()
        # True once the lamp's own reports have been received: the state is then
        # read from the lamp, not just assumed from the commands sent.
        self.reports_state = False

    @property
    def address(self) -> str:
        return self._ble_device.address

    @property
    def name(self) -> str:
        """Name used in errors and logs; the address unless one was given."""
        return self._name or self._ble_device.address

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def keep_connected(self) -> bool:
        """Whether the link is kept open and restored in the background."""
        return self._keep_connected

    def start(self) -> None:
        """Keep-connected mode: open the connection in the background, starting now.

        Returns at once; the state is read when the connection comes up, and failed
        attempts repeat with the reconnect backoff. Does nothing in on-demand mode.
        """
        if not self._keep_connected:
            return
        self._closed = False
        self._report_unreachable = True
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.get_running_loop().create_task(
            self._reconnect_loop(immediate=True)
        )

    def set_ble_device(self, ble_device: BLEDevice) -> None:
        """Use a fresher BLEDevice (e.g. via another proxy) for the next connection."""
        self._ble_device = ble_device

    def register_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call `callback` whenever the state changes; returns an unsubscribe function."""
        self._callbacks.append(callback)

        def _remove() -> None:
            self._callbacks.remove(callback)

        return _remove

    def register_disconnect_callback(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Call `callback` whenever an open link goes down; returns an unsubscribe function."""
        self._disconnect_callbacks.append(callback)

        def _remove() -> None:
            self._disconnect_callbacks.remove(callback)

        return _remove

    def update_from_advertisement(self, is_on: bool) -> None:
        """Apply on/off state decoded from an advertisement."""
        if self.state.is_on == is_on:
            return
        self.state.is_on = is_on
        if not is_on:
            self.state.flicker = False
        self._fire_callbacks()

    async def turn_on(self) -> None:
        def frames() -> list[bytes]:
            if self.state.flicker:
                return protocol.build_exit_flicker(self.state.brightness or protocol.MAX_BRIGHTNESS)
            return [protocol.build_on()]

        await self._send(frames, is_on=True, flicker=False, state_change=True)

    async def turn_off(self) -> None:
        await self._send(
            lambda: [protocol.build_off()], is_on=False, flicker=False, state_change=True
        )

    async def set_brightness(self, pct: int) -> None:
        pct = protocol.clamp_brightness(pct)

        def frames() -> list[bytes]:
            prefix = [] if self.state.is_on else [protocol.build_on()]
            if self.state.flicker:
                return prefix + protocol.build_exit_flicker(pct)
            return [*prefix, protocol.build_brightness(pct)]

        await self._send(frames, is_on=True, brightness=pct, flicker=False, state_change=True)

    async def set_flicker(self) -> None:
        await self._send(
            lambda: [protocol.build_on(), protocol.build_flicker()],
            is_on=True,
            flicker=True,
            state_change=True,
        )

    async def stop_flicker(self, pct: int | None = None) -> None:
        """Leave candle flicker mode, even if the known state says it is not active.

        Keeps the last known brightness unless `pct` is given.
        """
        target = None if pct is None else protocol.clamp_brightness(pct)

        def frames() -> list[bytes]:
            # Read the last brightness only now, after any earlier queued commands.
            level = target or self.state.brightness or protocol.MAX_BRIGHTNESS
            prefix = [] if self.state.is_on else [protocol.build_on()]
            return prefix + protocol.build_exit_flicker(level)

        await self._send(frames, is_on=True, brightness=target, flicker=False, state_change=True)

    async def update(self) -> None:
        await self._send(lambda: [protocol.build_get_state()])

    async def poll(self, *, time_limit: float | None = None, persistent: bool = False) -> None:
        """Read the state; a connection opened just for this is closed right away.

        A background read by default: one connection round, so a lamp that is hard to
        reach does not hold the lock for the whole deadline. With `persistent` it keeps
        reconnecting until `time_limit` (default: the command timeout) like a command.
        """
        await self._send(
            lambda: [protocol.build_get_state()],
            short=True,
            persistent=persistent,
            time_limit=time_limit,
        )

    async def send_raw(self, frame: bytes) -> None:
        """Send one arbitrary frame (developer CLI only)."""
        await self._send(lambda: [frame])

    async def connect(self) -> None:
        """Open the connection without sending anything."""
        await self._send(list)

    async def disconnect(self) -> None:
        """Close the connection and stop any background reconnect.

        Commands already waiting or retrying give up at their next attempt, and none of
        them can start a new reconnect loop afterwards.
        """
        self._closed = True
        await self._cancel_reconnect()
        async with self._lock:
            await self._disconnect_locked()
        await self._cancel_reconnect()

    async def _send(
        self,
        frames: Callable[[], list[bytes]],
        *,
        is_on: bool | None = None,
        brightness: int | None = None,
        flicker: bool | None = None,
        short: bool = False,
        persistent: bool = True,
        time_limit: float | None = None,
        state_change: bool = False,
        background: bool = False,
    ) -> None:
        """Send frames and apply the expected state.

        With `short`, a connection opened for this command is closed right after the
        reply instead of being kept for the idle timeout. The whole command, including
        waiting for earlier ones, is bounded by the command timeout (or `time_limit`).
        With `persistent`, failed connections and writes are retried on a new
        connection until that deadline; otherwise only a write on an already open
        connection is retried once.

        A `state_change` command is superseded by a newer one: if it has not been sent
        by then, it returns quietly without applying its expected state (latest wins).

        Anything but the reconnect loop's own reads (`background`) is a caller's
        command: it reopens a device closed by disconnect(), and in keep-connected mode
        a link it leaves down is restored in the background.
        """
        if not background:
            self._closed = False
            self._active_commands += 1
        superseded = self._start_state_command() if state_change else None
        deadline = asyncio.timeout(self._command_timeout if time_limit is None else time_limit)
        try:
            async with deadline:
                await self._send_serialized(
                    frames,
                    is_on=is_on,
                    brightness=brightness,
                    flicker=flicker,
                    short=short,
                    persistent=persistent,
                    superseded=superseded,
                )
        except _Superseded:
            _LOGGER.debug("%s: command superseded by a newer one", self.name)
            return
        except TimeoutError as err:
            if not deadline.expired():
                raise
            raise CandelaError(f"Timed out sending command to {self.name}") from err
        finally:
            if not background:
                self._active_commands -= 1
                self._reconnect_if_kept()
        self._fire_callbacks()

    async def _send_serialized(
        self,
        frames: Callable[[], list[bytes]],
        *,
        is_on: bool | None,
        brightness: int | None,
        flicker: bool | None,
        short: bool,
        persistent: bool,
        superseded: asyncio.Event | None,
    ) -> None:
        async with self._lock:
            self._cancel_idle()
            had_connection = self.is_connected
            try:
                payload = frames()
                updates = await self._deliver(payload, persistent=persistent, superseded=superseded)
                self._apply_expected(is_on, brightness, flicker)
                for update in updates:
                    self._apply_update(update)
            finally:
                # Also runs on cancellation; a no-op when the connection is gone.
                if short and not had_connection and not self._keep_connected:
                    await self._disconnect_locked()
                else:
                    self._schedule_idle()

    async def _deliver(
        self, payload: list[bytes], *, persistent: bool, superseded: asyncio.Event | None
    ) -> list[_Update]:
        """Write the frames, reconnecting after failures (caller holds the lock)."""
        attempt = 0
        while True:
            attempt += 1
            _raise_if_set(superseded)
            if self._closed:
                raise CandelaError(f"{self.name} was disconnected")
            reused_connection = self.is_connected
            try:
                client = await self._ensure_connected()
                # A newer command may have arrived while connecting; it gets the link.
                _raise_if_set(superseded)
                return await self._write_frames(client, payload)
            except _COMMAND_ERRORS as err:
                await self._disconnect_locked()
                if reused_connection:
                    # A kept link can die silently; retry at once on a new connection.
                    _LOGGER.debug(
                        "%s: write on open connection failed (%s); retrying", self.name, err
                    )
                    continue
                if not persistent:
                    raise CandelaError(f"Could not send command to {self.name}: {err}") from err
                _LOGGER.debug("%s: attempt %d failed (%s); retrying", self.name, attempt, err)
                await self._pause(superseded)

    async def _pause(self, superseded: asyncio.Event | None) -> None:
        """Wait before the next attempt; a superseded command stops waiting at once."""
        if superseded is None:
            await asyncio.sleep(self._retry_pause)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(superseded.wait(), self._retry_pause)

    def _start_state_command(self) -> asyncio.Event:
        """Supersede the previous state-changing command; return this one's event."""
        if self._latest_state_command is not None:
            self._latest_state_command.set()
        self._latest_state_command = asyncio.Event()
        return self._latest_state_command

    async def _write_frames(
        self, client: BleakClientWithServiceCache, frames: list[bytes]
    ) -> list[_Update]:
        updates: list[_Update] = []
        self._pending = updates
        waiting = True
        try:
            for frame in frames:
                response = self._response = asyncio.Event()
                await client.write_gatt_char(protocol.COMMAND_CHAR_UUID, frame, response=True)
                if not waiting:
                    continue
                try:
                    await asyncio.wait_for(response.wait(), self._response_timeout)
                except TimeoutError:
                    # No notifications: keep the optimistic state, stop waiting for replies.
                    waiting = False
        finally:
            self._pending = None
            self._response = None
        return updates

    async def _ensure_connected(self) -> BleakClientWithServiceCache:
        if self._client is not None and self._client.is_connected:
            return self._client
        client = await establish_connection(
            BleakClientWithServiceCache,
            self._ble_device,
            self.name,
            disconnected_callback=self._on_disconnected,
            ble_device_callback=lambda: self._ble_device,
            max_attempts=CONNECT_MAX_ATTEMPTS,
        )
        # Track the client before start_notify so a failure there can still disconnect it.
        self._client = client
        # Reached once: later failures are reconnects, not "not reachable yet".
        self._report_unreachable = False
        try:
            await client.start_notify(protocol.NOTIFY_CHAR_UUID, self._on_notification)
        except BleakError as err:
            if await self._start_proxy_notify(client):
                _LOGGER.debug(
                    "%s: %s; registered notifications on the proxy directly", self.name, err
                )
            else:
                _LOGGER.debug(
                    "%s: notifications unavailable (%s); using optimistic state", self.name, err
                )
        return client

    async def _start_proxy_notify(self, client: BleakClientWithServiceCache) -> bool:
        try:
            return await start_notify_without_cccd(
                client, protocol.NOTIFY_CHAR_UUID, self._on_notification
            )
        except Exception as err:  # proxy API errors vary; fall back to optimistic state
            _LOGGER.debug("%s: direct proxy registration failed: %r", self.name, err)
            return False

    async def _disconnect_locked(self) -> None:
        self._cancel_idle()
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception as err:  # backends (e.g. ESPHome proxies) may raise anything
            _LOGGER.debug("%s: error while disconnecting: %r", self.name, err)
        self._fire_disconnect_callbacks()

    def _on_disconnected(self, client: BleakClientWithServiceCache) -> None:
        if client is not self._client:
            return
        _LOGGER.debug("%s: disconnected", self.name)
        self._client = None
        self._cancel_idle()
        self._fire_disconnect_callbacks()
        self._reconnect_if_kept()

    def _reconnect_if_kept(self) -> None:
        """In keep-connected mode, restore a lost link in the background.

        Not while a caller's command is in progress (it reconnects itself and calls
        this again when done) and never after disconnect().
        """
        if not self._keep_connected or self._closed or self._active_commands:
            return
        if self.is_connected:
            return
        if self._reconnect_task is not None and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.get_running_loop().create_task(self._reconnect_loop())

    async def _reconnect_loop(self, *, immediate: bool = False) -> None:
        delays = (0.0, *self._reconnect_delays) if immediate else self._reconnect_delays
        attempt = 0
        while True:
            delay = delays[min(attempt, len(delays) - 1)]
            attempt += 1
            await asyncio.sleep(delay)
            if self._closed or self._active_commands or self.is_connected:
                # Closed, or a caller's command is handling the connection.
                return
            try:
                # Reading the state also catches changes made by hand meanwhile. One
                # connection round per attempt: the loop does its own backoff.
                await self._send(
                    lambda: [protocol.build_get_state()], persistent=False, background=True
                )
            except CandelaError as err:
                if self._report_unreachable:
                    # Once, so users know why the lamp shows an unknown/assumed state.
                    self._report_unreachable = False
                    _LOGGER.info(
                        "%s: not reachable yet (%s); retrying in the background", self.name, err
                    )
                else:
                    _LOGGER.debug("%s: reconnect attempt %d failed: %s", self.name, attempt, err)
                continue
            if self.is_connected:
                _LOGGER.debug("%s: reconnected", self.name)
                return
            _LOGGER.debug("%s: link dropped again right after reconnecting", self.name)

    async def _cancel_reconnect(self) -> None:
        task, self._reconnect_task = self._reconnect_task, None
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _schedule_idle(self) -> None:
        self._cancel_idle()
        if self._client is None or self._keep_connected:
            return
        self._idle_handle = asyncio.get_running_loop().call_later(
            self._idle_timeout, self._on_idle_timeout
        )

    def _cancel_idle(self) -> None:
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None

    def _on_idle_timeout(self) -> None:
        self._idle_handle = None
        _LOGGER.debug("%s: idle, disconnecting", self.name)
        self._idle_task = asyncio.get_running_loop().create_task(self._disconnect_idle())

    async def _disconnect_idle(self) -> None:
        # Not disconnect(): an idle close must not stop commands that start meanwhile.
        async with self._lock:
            if self._idle_handle is None:  # else a command meanwhile restarted the timer
                await self._disconnect_locked()

    def _on_notification(self, _characteristic: object, data: bytearray) -> None:
        update = protocol.parse_notification(bytes(data))
        if update is None:
            return
        self.reports_state = True
        if self._pending is not None:
            self._pending.append(update)
            if self._response is not None:
                self._response.set()
            return
        self._apply_update(update)
        self._fire_callbacks()

    def _apply_expected(
        self, is_on: bool | None, brightness: int | None, flicker: bool | None
    ) -> None:
        if is_on is not None:
            self.state.is_on = is_on
        if brightness is not None:
            self.state.brightness = brightness
        if flicker is not None:
            self.state.flicker = flicker

    def _apply_update(self, update: _Update) -> None:
        if isinstance(update, StateUpdate):
            self.state.is_on = update.is_on
            self.state.brightness = update.brightness
            if not update.is_on:
                self.state.flicker = False
        else:
            self.state.flicker = update.active

    def _fire_callbacks(self) -> None:
        for callback in list(self._callbacks):
            callback()

    def _fire_disconnect_callbacks(self) -> None:
        for callback in list(self._disconnect_callbacks):
            callback()


def _raise_if_set(superseded: asyncio.Event | None) -> None:
    if superseded is not None and superseded.is_set():
        raise _Superseded
