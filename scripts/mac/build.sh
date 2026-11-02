#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Madeira Converter Exception: see LICENSE-EXCEPTION.md
#
# Build iDeck for a physical iPhone/iPad on macOS.
#
# Two modes:
#   unsigned (default)  produces an .ipa you sign elsewhere (Sideloadly, AltStore).
#                       Nothing about your team or device is needed to compile.
#   signed  (--team)    Xcode signs app + JIT helper with your team, so
#                       `--install` can push it to a connected device.
#
# Debug is the configuration that runs games; a Release build has crashed the
# guest (docs/BUILDING.md). Do not switch it.
#
#   scripts/mac/build.sh --bundle-id com.symi.ideck.steamui
#   scripts/mac/build.sh --team ABCDE12345 --bundle-id com.symi.ideck.steamui --install
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

CONFIG=Debug
BUNDLE_ID=""
TEAM=""
DEVICE=""
OUT=""
DERIVED="$ROOT/build/DerivedData-ideck"
DO_INSTALL=0
DO_PACKAGE=1
SKIP_PREFLIGHT=0
FORCE=0
BUILD_NUMBER="${IDECK_BUILD_NUMBER:-$(date +%y%m%d%H)}"

usage() { sed -n '3,20p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --team) TEAM="${2:?}"; shift 2 ;;
    --bundle-id) BUNDLE_ID="${2:?}"; shift 2 ;;
    --out) OUT="${2:?}"; shift 2 ;;
    --device) DEVICE="${2:?}"; shift 2 ;;
    --derived) DERIVED="${2:?}"; shift 2 ;;
    --build-number) BUILD_NUMBER="${2:?}"; shift 2 ;;
    --install) DO_INSTALL=1; shift ;;
    --no-package) DO_PACKAGE=0; shift ;;
    --force) FORCE=1; shift ;;
    --skip-preflight) SKIP_PREFLIGHT=1; shift ;;
    -h|--help) usage 0 ;;
    *) echo "unknown option: $1" >&2; usage 1 ;;
  esac
done

if [ "$(uname -s)" != Darwin ]; then
  cat >&2 <<'EOF'
error: xcodebuild needs macOS.
  Compile on a Mac, or sideload the published IPA from GitHub Releases instead.
  Staging the inputs (tools/ideck-bootstrap.py) and the preflight check work on
  any OS, but the app target can only be compiled here.
EOF
  exit 1
fi

command -v xcodebuild >/dev/null || { echo "error: Xcode command line tools missing (xcode-select --install)" >&2; exit 1; }
SDK_VERSION="$(xcrun --sdk iphoneos --show-sdk-version)"
case "$SDK_VERSION" in
  2[6-9].*|3[0-9].*) ;;
  *) echo "error: iOS SDK $SDK_VERSION found, but the app targets iOS 26.0+. Install Xcode 27 or newer." >&2; exit 1 ;;
esac

if [ -z "$BUNDLE_ID" ]; then
  BUNDLE_ID="${MADEIRA_BUNDLE_IDENTIFIER:-com.kuscholar.ideck.steamui}"
fi
# The product shell keys off IDeckEnabled, not this suffix, but keeping it makes
# the app recognisable next to the upstream build on a shared device.
case "$BUNDLE_ID" in
  *.*) ;;
  *) echo "error: --bundle-id needs a reverse-DNS identifier, got '$BUNDLE_ID'" >&2; exit 1 ;;
esac
if [ "$BUNDLE_ID" = "com.kuscholar.ideck.steamui" ]; then
  echo "note: building with the upstream bundle identifier. Pass --bundle-id for your own team."
fi

LLVM_MINGW="${LLVM_MINGW:-$(ls -d "$ROOT"/toolchains/llvm-mingw-*/ 2>/dev/null | head -1)}"
LLVM_MINGW="${LLVM_MINGW%/}"
if [ -z "$LLVM_MINGW" ] || [ ! -x "$LLVM_MINGW/bin/x86_64-w64-mingw32-clang" ]; then
  cat >&2 <<'EOF'
error: llvm-mingw not found.
  The "Bundle Wine Mono" phase compiles ideck-control.exe with
  x86_64-w64-mingw32-clang, so the build fails without it.
  Fix: python3 tools/ideck-bootstrap.py --vcruntime-dir <dir>
  or:  export LLVM_MINGW=/path/to/llvm-mingw-20260421-ucrt-macos-universal
EOF
  exit 1
fi
export LLVM_MINGW

if [ "$SKIP_PREFLIGHT" -eq 0 ]; then
  echo "== preflight"
  if ! python3 tools/ideck-preflight.py --repo "$ROOT"; then
    echo
    echo "Preflight failed. Stage the missing inputs with:" >&2
    echo "  python3 tools/ideck-bootstrap.py" >&2
    exit 1
  fi
fi

mkdir -p "$DERIVED"
ARGS=(
  -project app/Madeira.xcodeproj
  -scheme Madeira
  -configuration "$CONFIG"
  -destination "generic/platform=iOS"
  -derivedDataPath "$DERIVED"
  CODE_SIGNING_ALLOWED=NO
  CURRENT_PROJECT_VERSION="$BUILD_NUMBER"
  IPHONEOS_DEPLOYMENT_TARGET=26.0
  MADEIRA_BUNDLE_IDENTIFIER="$BUNDLE_ID"
  LD_GENERATE_MAP_FILE=YES
)
if [ -n "$TEAM" ]; then
  ARGS=("${ARGS[@]/CODE_SIGNING_ALLOWED=NO/CODE_SIGNING_ALLOWED=YES}")
  ARGS+=(-allowProvisioningUpdates DEVELOPMENT_TEAM="$TEAM")
  # get-task-allow comes from the development profile Xcode requests here.
  # A distribution-only profile cannot enable JIT (docs/JIT.md).
fi

echo "== xcodebuild ($CONFIG, iOS SDK $SDK_VERSION, bundle id $BUNDLE_ID)"
echo "   LLVM_MINGW=$LLVM_MINGW"
LOG="$DERIVED/xcodebuild-$(date +%H%M%S).log"
set +e
xcodebuild "${ARGS[@]}" build 2>&1 | tee "$LOG"
STATUS=${PIPESTATUS[0]}
set -e
if [ "$STATUS" -ne 0 ]; then
  echo
  echo "Build failed (exit $STATUS). First errors:" >&2
  grep -E "(^| )error: " "$LOG" | head -20 >&2 || true
  echo "Full log: $LOG" >&2
  exit "$STATUS"
fi

APP="$DERIVED/Build/Products/$CONFIG-iphoneos/Madeira.app"
[ -d "$APP" ] || { echo "error: built product not found at $APP" >&2; exit 1; }
echo "== built $(du -sh "$APP" | cut -f1) app bundle"

echo "== verifying the product (no device needed)"
if ! python3 tools/ideck-verify-app.py "$APP" --expect-bundle-id "$BUNDLE_ID"; then
  echo "error: the product failed verification; not packaging it" >&2
  exit 1
fi

if [ "$DO_PACKAGE" -eq 1 ]; then
  OUT="${OUT:-$ROOT/build/iDeck-$(date +%Y%m%d-%H%M)-$BUNDLE_ID.ipa}"
  if [ -e "$OUT" ] && [ "$FORCE" -eq 0 ]; then
    echo "error: $OUT exists; pass --force to replace it" >&2
    exit 1
  fi
  rm -f "$OUT"
  echo "== packaging $OUT"
  python3 tools/package-ipa.py "$APP" "$OUT" --entitlements app/Madeira/Madeira.entitlements
  python3 tools/ideck-verify-ipa.py "$OUT" --expect-bundle-id "$BUNDLE_ID"
  echo
  echo "IPA: $OUT"
  if [ -z "$TEAM" ]; then
    cat <<EOF

This IPA is ad-hoc signed and carries no profile: it will NOT install as-is.
Sign it with your own development certificate on the machine with your device:
  - Windows: Sideloadly (keep MadeiraJITHelper.appex, keep get-task-allow)
  - macOS:   scripts/mac/build.sh --team <TEAMID> --install
Then follow docs/INSTALL_IPHONE_ZH.md for JIT (LocalDevVPN + StikDebug or the
built-in StikJIT helper).
EOF
  else
    echo "Signed with team $TEAM. Install with: xcrun devicectl device install app --device <UDID> \"$APP\""
  fi
fi

if [ "$DO_INSTALL" -eq 1 ]; then
  if [ -z "$TEAM" ]; then
    echo "error: --install needs --team (an unsigned bundle cannot be installed)" >&2
    exit 1
  fi
  if [ -z "$DEVICE" ]; then
    echo "== connected devices (pass --device <UDID> to install automatically)"
    xcrun devicectl list devices || true
    echo
    echo "Then: xcrun devicectl device install app --device <UDID> \"$APP\""
    exit 0
  fi
  echo "== installing on $DEVICE"
  xcrun devicectl device install app --device "$DEVICE" "$APP"
fi
