#!/bin/bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

APP_VERSION="2.4.0"
RUNTIME_VERSION="4.1"
RUNTIME_ARCHIVE="scrcpy-macos-aarch64-v${RUNTIME_VERSION}.tar.gz"
RUNTIME_URL="https://github.com/Genymobile/scrcpy/releases/download/v${RUNTIME_VERSION}/${RUNTIME_ARCHIVE}"
RUNTIME_SHA256="20fd47c9014dd5e0fa77091f3cb7adbda8445a360c4584aeaa0150b5b3988ff3"
RUNTIME_ROOT="$PROJECT_ROOT/scrcpy-runtime-macos-arm64"
VENV_ROOT="$PROJECT_ROOT/.venv-macos-arm64"
BUILD_ROOT="$PROJECT_ROOT/build-macos-arm64"
DIST_ROOT="$PROJECT_ROOT/versions/macos/v${APP_VERSION}"
APP_PATH="$DIST_ROOT/Convrse Device Control.app"
DMG_PATH="$DIST_ROOT/Convrse-Device-Control-${APP_VERSION}-Apple-Silicon.dmg"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This builder must run on macOS. The Windows V2.3 build was not changed."
  exit 1
fi
if [[ "$(uname -m)" != "arm64" ]]; then
  echo "This edition targets Apple Silicon. Run it on an M-series Mac."
  exit 1
fi
if [[ -n "${CDC_NOTARY_PROFILE:-}" && -z "${CDC_CODESIGN_IDENTITY:-}" ]]; then
  echo "CDC_NOTARY_PROFILE requires a Developer ID in CDC_CODESIGN_IDENTITY."
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3 was not found. Install Python 3.10 or newer, then run this file again."
  exit 1
fi

echo "[1/7] Preparing the verified scrcpy ${RUNTIME_VERSION} Apple Silicon runtime..."
if [[ ! -f "$RUNTIME_ROOT/scrcpy" || ! -f "$RUNTIME_ROOT/adb" || ! -f "$RUNTIME_ROOT/scrcpy-server" ]]; then
  TEMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/convrse-cdc.XXXXXX")"
  trap 'rm -rf "$TEMP_ROOT"' EXIT
  curl --fail --location --progress-bar "$RUNTIME_URL" -o "$TEMP_ROOT/$RUNTIME_ARCHIVE"
  ACTUAL_SHA256="$(shasum -a 256 "$TEMP_ROOT/$RUNTIME_ARCHIVE" | awk '{print $1}')"
  if [[ "$ACTUAL_SHA256" != "$RUNTIME_SHA256" ]]; then
    echo "scrcpy checksum verification failed. Refusing to package an unverified runtime."
    exit 1
  fi
  tar -xzf "$TEMP_ROOT/$RUNTIME_ARCHIVE" -C "$TEMP_ROOT"
  rm -rf "$RUNTIME_ROOT"
  mkdir -p "$RUNTIME_ROOT"
  ditto "$TEMP_ROOT/scrcpy-macos-aarch64-v${RUNTIME_VERSION}" "$RUNTIME_ROOT"
fi
chmod 755 "$RUNTIME_ROOT/scrcpy" "$RUNTIME_ROOT/adb"
xattr -cr "$RUNTIME_ROOT" 2>/dev/null || true
EXPECTED_SCRCPY_SHA256="e318a04c11986d9afa7f438a81cc9c7cc0f3ea66945db1e127f373eb02f4e1d3"
EXPECTED_ADB_SHA256="9fdf861259dc807937b13afdd5f053c7fda9f3b7726933fe0e0f45130ecb8dc7"
EXPECTED_SERVER_SHA256="deacb991ed2509715160ffdc7907e47b4160eb30d1566217e9047fd5b8850cae"
for ENTRY in \
  "scrcpy:$EXPECTED_SCRCPY_SHA256" \
  "adb:$EXPECTED_ADB_SHA256" \
  "scrcpy-server:$EXPECTED_SERVER_SHA256"; do
  FILE_NAME="${ENTRY%%:*}"
  EXPECTED_HASH="${ENTRY#*:}"
  ACTUAL_HASH="$(shasum -a 256 "$RUNTIME_ROOT/$FILE_NAME" | awk '{print $1}')"
  if [[ "$ACTUAL_HASH" != "$EXPECTED_HASH" ]]; then
    echo "Runtime verification failed for $FILE_NAME."
    exit 1
  fi
done
file "$RUNTIME_ROOT/scrcpy" | grep -q "arm64" || {
  echo "The bundled scrcpy executable is not an Apple Silicon Mach-O binary."
  exit 1
}

echo "[2/7] Creating the native Mac app icon..."
ICON_SOURCE="$PROJECT_ROOT/assets/convrse-logo.png"
ICONSET="$BUILD_ROOT/ConvrseIcon.iconset"
mkdir -p "$BUILD_ROOT"
rm -rf "$ICONSET"
mkdir -p "$ICONSET"
for SIZE in 16 32 128 256 512; do
  DOUBLE_SIZE=$((SIZE * 2))
  sips -z "$SIZE" "$SIZE" "$ICON_SOURCE" --out "$ICONSET/icon_${SIZE}x${SIZE}.png" >/dev/null
  sips -z "$DOUBLE_SIZE" "$DOUBLE_SIZE" "$ICON_SOURCE" --out "$ICONSET/icon_${SIZE}x${SIZE}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$PROJECT_ROOT/assets/convrse-logo.icns"

echo "[3/7] Preparing the isolated Python environment..."
if [[ ! -x "$VENV_ROOT/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_ROOT"
fi
"$VENV_ROOT/bin/python" -m pip install --upgrade pip
"$VENV_ROOT/bin/python" -m pip install -r requirements.txt

echo "[4/7] Running the automated test suite..."
export QT_QPA_PLATFORM=offscreen
"$VENV_ROOT/bin/python" -m unittest discover -s tests -p 'test_*.py'
unset QT_QPA_PLATFORM

echo "[5/7] Building the Apple Silicon .app..."
export PYINSTALLER_STRICT_BUNDLE_CODESIGN_ERROR=1
"$VENV_ROOT/bin/python" -m PyInstaller \
  --noconfirm \
  --clean \
  --distpath "$DIST_ROOT" \
  --workpath "$BUILD_ROOT/pyinstaller" \
  scrcpy-remote-macos-arm64.spec

if [[ ! -d "$APP_PATH" ]]; then
  echo "Build finished without the expected app bundle: $APP_PATH"
  exit 1
fi

echo "[6/7] Verifying the app bundle and creating the drag-to-Applications DMG..."
codesign --verify --deep --strict --verbose=2 "$APP_PATH"
STAGING_ROOT="$BUILD_ROOT/dmg-staging"
rm -rf "$STAGING_ROOT"
mkdir -p "$STAGING_ROOT"
ditto "$APP_PATH" "$STAGING_ROOT/Convrse Device Control.app"
ln -s /Applications "$STAGING_ROOT/Applications"
rm -f "$DMG_PATH"
hdiutil create \
  -volname "Convrse Device Control" \
  -srcfolder "$STAGING_ROOT" \
  -ov \
  -format UDZO \
  "$DMG_PATH"
if [[ -n "${CDC_CODESIGN_IDENTITY:-}" ]]; then
  codesign --force --timestamp --sign "$CDC_CODESIGN_IDENTITY" "$DMG_PATH"
fi

echo "[7/7] Final verification..."
codesign --verify --deep --strict --verbose=2 "$APP_PATH"
spctl --assess --type execute --verbose=2 "$APP_PATH" || true

if [[ -n "${CDC_NOTARY_PROFILE:-}" ]]; then
  echo "Submitting the DMG for Apple notarization..."
  xcrun notarytool submit "$DMG_PATH" \
    --keychain-profile "$CDC_NOTARY_PROFILE" \
    --wait
  xcrun stapler staple "$DMG_PATH"
  xcrun stapler validate "$DMG_PATH"
else
  echo "No CDC_NOTARY_PROFILE was supplied; this is a local/test build."
fi

echo
echo "DONE"
echo "App: $APP_PATH"
echo "DMG: $DMG_PATH"
echo "End users only open the DMG, drag the app to Applications, and double-click it."
