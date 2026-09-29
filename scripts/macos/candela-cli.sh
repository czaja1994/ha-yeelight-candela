#!/usr/bin/env bash
# Run scripts/candela_cli.py on macOS inside a minimal .app bundle so that the
# Bluetooth (TCC) permission is attributed to this app, not to the parent process.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
APP="$ROOT/build/CandelaCLI.app"
OUT="$(mktemp -t candela-cli)"

mkdir -p "$APP/Contents/MacOS"
cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>dev.czaja1994.candela-cli</string>
<key>CFBundleName</key><string>CandelaCLI</string>
<key>CFBundleExecutable</key><string>candela-cli</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSUIElement</key><true/>
<key>NSBluetoothAlwaysUsageDescription</key><string>Control Yeelight Candela lamps</string>
</dict></plist>
PLIST
cat > "$APP/Contents/MacOS/candela-cli" <<EOF
#!/bin/bash
exec "$ROOT/.venv/bin/python" "$ROOT/scripts/candela_cli.py" "\$@"
EOF
chmod +x "$APP/Contents/MacOS/candela-cli"
codesign -s - --force "$APP" >/dev/null 2>&1

open -W -n --stdout "$OUT" --stderr "$OUT" "$APP" --args "$@"
cat "$OUT"
rm -f "$OUT"
