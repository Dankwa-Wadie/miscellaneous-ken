#!/usr/bin/env bash
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$PROJECT_DIR/build/Miscellaneous Ken.app"
mkdir -p "$APP_DIR/Contents/MacOS" "$APP_DIR/Contents/Resources"
if [ -f "$PROJECT_DIR/assets/app-icon/MiscellaneousKen.icns" ]; then
  cp "$PROJECT_DIR/assets/app-icon/MiscellaneousKen.icns" "$APP_DIR/Contents/Resources/MiscellaneousKen.icns"
fi
clang -fobjc-arc -fno-modules -mmacosx-version-min=12.0 -framework Cocoa -framework WebKit "$PROJECT_DIR/macos/Studio.m" -o "$APP_DIR/Contents/MacOS/MiscellaneousKen"
/usr/bin/python3 - "$APP_DIR" <<'PY'
import plistlib,sys
from pathlib import Path
app=Path(sys.argv[1])
with (app/'Contents/Info.plist').open('wb') as f:
    plistlib.dump(dict(CFBundleName='Miscellaneous Ken',CFBundleDisplayName='Miscellaneous Ken',
        CFBundleIdentifier='com.miscellaneousken.studioapp',CFBundleExecutable='MiscellaneousKen',
        CFBundlePackageType='APPL',CFBundleShortVersionString='1.0',CFBundleVersion='2',
        CFBundleIconFile='MiscellaneousKen',
        LSMinimumSystemVersion='12.0',NSHighResolutionCapable=True,NSAppTransportSecurity={'NSAllowsLocalNetworking':True}),f)
PY
codesign --force --sign - "$APP_DIR"
