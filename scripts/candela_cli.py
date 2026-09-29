#!/usr/bin/env python3
"""Developer CLI for Yeelight Candela lamps, built on the integration's candela package."""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "custom_components" / "yeelight_candela")
)

from bleak import BleakScanner  # noqa: E402
from candela.advertisement import YEELINK_COMPANY_ID, parse_manufacturer_data  # noqa: E402
from candela.device import CandelaDevice  # noqa: E402


async def scan(seconds: float) -> None:
    found = await BleakScanner.discover(timeout=seconds, return_adv=True)
    for ble_device, adv in found.values():
        info = parse_manufacturer_data(adv.manufacturer_data)
        if info is None:
            continue
        print(
            f"{ble_device.address}  rssi={adv.rssi}  name={adv.local_name}  "
            f"mac~=F8:24:{info.mac_suffix}  flag=0x{info.state_flag:02x}"
        )


async def watch(seconds: float) -> None:
    last: dict[str, bytes] = {}

    def on_advertisement(ble_device, adv) -> None:
        payload = adv.manufacturer_data.get(YEELINK_COMPANY_ID)
        if (
            parse_manufacturer_data(adv.manufacturer_data) is None
            or last.get(ble_device.address) == payload
        ):
            return
        last[ble_device.address] = payload
        print(
            f"{time.strftime('%H:%M:%S')} {ble_device.address} payload={payload.hex()}",
            flush=True,
        )

    async with BleakScanner(detection_callback=on_advertisement):
        await asyncio.sleep(seconds)


async def command(address: str, action: str, value: str | None) -> None:
    ble_device = await BleakScanner.find_device_by_address(address, timeout=10)
    if ble_device is None:
        raise SystemExit(f"Lamp {address} not found")
    device = CandelaDevice(ble_device)
    try:
        if action == "state":
            await device.update()
        elif action == "on":
            await device.turn_on()
        elif action == "off":
            await device.turn_off()
        elif action == "flicker":
            await device.set_flicker()
        elif action == "brightness":
            await device.set_brightness(int(value))
        elif action == "raw":
            await device.send_raw(bytes.fromhex(value).ljust(18, b"\x00"))
        print(f"{time.strftime('%H:%M:%S')} {address} {action} {value or ''} -> {device.state}")
    finally:
        await device.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    for name in ("scan", "watch"):
        p = sub.add_parser(name)
        p.add_argument("--seconds", type=float, default=10.0)
    for name in ("state", "on", "off", "flicker"):
        sub.add_parser(name).add_argument("address")
    p = sub.add_parser("brightness")
    p.add_argument("address")
    p.add_argument("value", help="1-100")
    p = sub.add_parser("raw")
    p.add_argument("address")
    p.add_argument("value", help="hex frame prefix, e.g. 434228")
    args = parser.parse_args()

    if args.action == "scan":
        asyncio.run(scan(args.seconds))
    elif args.action == "watch":
        asyncio.run(watch(args.seconds))
    else:
        asyncio.run(command(args.address, args.action, getattr(args, "value", None)))


if __name__ == "__main__":
    main()
