#!/bin/bash
# Собирает build/Kachalka.app (универсальный: Intel + Apple Silicon).
# Нужны только Command Line Tools: xcode-select --install
set -euo pipefail
cd "$(dirname "$0")/.."
APP=build/Kachalka.app
TMP=build/tmp
rm -rf "$APP" "$TMP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$TMP"

for arch in x86_64 arm64; do
  swiftc -O -swift-version 5 -parse-as-library -target "$arch-apple-macos14.0" \
    app/Kachalka.swift -o "$TMP/Kachalka-$arch"
done
lipo -create "$TMP/Kachalka-x86_64" "$TMP/Kachalka-arm64" -output "$APP/Contents/MacOS/Kachalka"
cp app/Info.plist "$APP/Contents/Info.plist"

# значок
swift app/make_icon.swift "$TMP/icon.png"
ICONSET="$TMP/AppIcon.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$TMP/icon.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s * 2)) $((s * 2)) "$TMP/icon.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

# подпись «для себя», чтобы macOS не ругалась на повреждённое приложение
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true
rm -rf "$TMP"
echo "Готово: $APP"
