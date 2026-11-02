#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Madeira Converter Exception: see LICENSE-EXCEPTION.md
"""Verify a built Madeira.app before it is signed, packaged or installed.

This is the last check that can run without a device, and it reads the product
the way iOS will: the Mach-O platform (a simulator build links none of the
device-only archives and must never ship), the Info.plist contract the JIT
callback depends on, the embedded helper extension, the Wine payload folders,
and whether the signature carries the two entitlements JIT needs.

It does not launch anything. Launch, JIT attach, Steam sign-in and rendering
need the real iPad.

    python3 tools/ideck-verify-app.py <path>/Madeira.app
"""

import argparse
import plistlib
import shutil
import struct
import subprocess
import sys
from pathlib import Path

PASS, WARN, FAIL = 'ok', 'warn', 'fail'
MARK = {PASS: '[ ok ]', WARN: '[warn]', FAIL: '[FAIL]'}

CPU_TYPE_ARM64 = 0x0100000C
PLATFORMS = {1: 'macOS', 2: 'iOS', 3: 'tvOS', 4: 'watchOS', 6: 'Mac Catalyst',
             7: 'iOS Simulator', 8: 'tvOS Simulator', 9: 'watchOS Simulator',
             11: 'visionOS', 12: 'visionOS Simulator'}
LC_BUILD_VERSION = 0x32
LC_VERSION_MIN_IPHONEOS = 0x25

rows = []


def add(status, subject, detail=''):
    rows.append((status, subject, detail))


def count_files(path):
    return sum(1 for p in path.rglob('*') if p.is_file()) if path.is_dir() else 0


def macho_identity(path):
    """(cpu, platform, minos) from a thin Mach-O, or (None, None, None)."""
    try:
        data = path.read_bytes()[:8192]
    except OSError:
        return None, None, None
    if len(data) < 32:
        return None, None, None
    magic = struct.unpack_from('<I', data, 0)[0]
    if magic == 0xFEEDFACF:          # MH_MAGIC_64, little endian host
        cpu, _sub, _ft, ncmds, sizeofcmds = struct.unpack_from('<iiIII', data, 4)
        offset = 32
    elif magic == 0xCFFAEDFE:        # byte-swapped
        cpu = struct.unpack_from('>i', data, 4)[0]
        ncmds, sizeofcmds = struct.unpack_from('>II', data, 16)
        offset = 32
    else:
        return None, None, None
    platform = minos = None
    end = min(offset + sizeofcmds, len(data))
    for _ in range(ncmds):
        if offset + 8 > end:
            break
        cmd, cmdsize = struct.unpack_from('<II', data, offset)
        if cmdsize < 8:
            break
        if cmd == LC_BUILD_VERSION and offset + 16 <= end:
            platform, minos = struct.unpack_from('<II', data, offset + 8)
            break
        if cmd == LC_VERSION_MIN_IPHONEOS and offset + 12 <= end:
            platform = 2
            break
        offset += cmdsize
    return cpu, platform, minos


def version_string(encoded):
    if not encoded:
        return None
    return '%d.%d.%d' % (encoded >> 16, (encoded >> 8) & 0xFF, encoded & 0xFF)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('app', type=Path)
    parser.add_argument('--expect-bundle-id', default=None)
    args = parser.parse_args()
    app = args.app
    if not app.is_dir():
        print('not an .app bundle: %s' % app)
        return 1

    size = sum(p.stat().st_size for p in app.rglob('*') if p.is_file())
    print('== %s (%.0f MB)' % (app, size / (1 << 20)))

    # --- platform and architecture -----------------------------------------
    binary = app / app.stem
    if not binary.is_file():
        binary = app / 'Madeira'
    cpu, platform, minos = macho_identity(binary) if binary.is_file() else (None, None, None)
    if cpu is None:
        add(FAIL, 'app executable', 'missing or not a Mach-O: %s' % binary.name)
    else:
        name = PLATFORMS.get(platform, 'unknown (%s)' % platform)
        if platform == 2:
            add(PASS, 'build platform', 'iOS device, minos %s' % (version_string(minos) or '?'))
        elif platform in (7, 8, 9, 12):
            add(FAIL, 'build platform',
                '%s; the relink archives are arm64-apple-ios26.0 and cannot link for a simulator' % name)
        else:
            add(WARN, 'build platform', name)
        if cpu == CPU_TYPE_ARM64:
            add(PASS, 'architecture', 'arm64')
        else:
            add(FAIL, 'architecture', 'cpu type 0x%x, expected arm64' % cpu)

    # --- Info.plist contract ------------------------------------------------
    info_path = app / 'Info.plist'
    try:
        info = plistlib.loads(info_path.read_bytes())
        add(PASS, 'Info.plist', 'parsed')
    except Exception as exc:  # noqa: BLE001
        add(FAIL, 'Info.plist', 'unreadable: %s' % exc)
        info = {}
    bundle_id = info.get('CFBundleIdentifier', '')
    if args.expect_bundle_id and bundle_id != args.expect_bundle_id:
        add(FAIL, 'CFBundleIdentifier', '%s, expected %s' % (bundle_id, args.expect_bundle_id))
    elif bundle_id:
        add(PASS, 'CFBundleIdentifier', bundle_id)
    min_os = str(info.get('MinimumOSVersion', ''))
    add(PASS if min_os.startswith(('26', '27', '28', '29', '3')) else WARN,
        'MinimumOSVersion', min_os or 'missing')
    add(PASS if info.get('IDeckEnabled') is True else WARN, 'IDeckEnabled',
        'true' if info.get('IDeckEnabled') is True else
        'not true; a non-.steamui bundle id leaves the launcher hidden')
    schemes = [s for t in info.get('CFBundleURLTypes', []) for s in t.get('CFBundleURLSchemes', [])]
    add(PASS if schemes else FAIL, 'URL schemes', ', '.join(schemes) or 'none; the JIT callback cannot return')

    # --- JIT helper extension ----------------------------------------------
    helper = app / 'PlugIns/MadeiraJITHelper.appex'
    if not helper.is_dir():
        add(FAIL, 'MadeiraJITHelper.appex',
            'missing; built-in JIT cannot attach, StikDebug is the only fallback')
    else:
        hexe = helper / 'MadeiraJITHelper'
        if hexe.is_file():
            hcpu, hplat, _ = macho_identity(hexe)
            add(PASS if hplat == 2 else WARN, 'MadeiraJITHelper.appex',
                '%d files, %.0f KB executable%s' % (
                    count_files(helper), hexe.stat().st_size / 1024,
                    '' if hplat == 2 else ', platform %s' % PLATFORMS.get(hplat, hplat)))
        else:
            add(FAIL, 'MadeiraJITHelper.appex', 'executable missing')
        try:
            hinfo = plistlib.loads((helper / 'Info.plist').read_bytes())
            point = hinfo.get('NSExtension', {}).get('NSExtensionPointIdentifier', '')
            add(PASS if point == 'com.apple.ar.viewer' else WARN, 'helper extension point',
                point or 'unreadable')
            hbid = hinfo.get('CFBundleIdentifier', '')
            add(PASS if hbid == bundle_id + '.JITHelper' else WARN, 'helper bundle id',
                hbid or 'missing')
        except Exception as exc:  # noqa: BLE001
            add(FAIL, 'helper Info.plist', 'unreadable: %s' % exc)

    # --- payload folders ----------------------------------------------------
    stikjit = app / 'Frameworks/StikJIT.framework/StikJIT'
    add(PASS if stikjit.is_file() else FAIL, 'Frameworks/StikJIT.framework',
        '%.1f MB' % (stikjit.stat().st_size / (1 << 20)) if stikjit.is_file()
        else 'missing; the helper loads it from ../../Frameworks')
    for folder, floor in (('arm64ec-windows', 240), ('i386-windows', 650), ('aarch64-windows', 120)):
        n = count_files(app / folder)
        if n == 0:
            add(FAIL, folder, 'missing; Wine cannot start a guest without it')
        else:
            add(PASS if n >= floor else FAIL, folder, '%d files' % n)
    for rel, label in (('prefix-template.tar.gz', 'Wine prefix template'),
                       ('d3d12/libmetalirconverter.dylib', 'Metal shader converter'),
                       ('madeira-jit.js', 'JIT debugger script'),
                       ('cacert.pem', 'CA bundle')):
        path = app / rel
        add(PASS if path.is_file() else WARN, label, 'present' if path.is_file() else 'missing')
    vc = count_files(app / 'x86_64-vcruntime')
    if vc:
        add(PASS if vc >= 14 else WARN, 'x86_64-vcruntime',
            '%d DLLs%s' % (vc, '' if vc >= 14 else '; x86_64 guests miss the MSVC runtime'))

    # --- signature ----------------------------------------------------------
    if shutil.which('codesign'):
        verified = subprocess.run(['codesign', '--verify', '--deep', '--strict', str(app)],
                                  capture_output=True, text=True)
        add(PASS if verified.returncode == 0 else FAIL, 'signature',
            'valid (deep, strict)' if verified.returncode == 0
            else (verified.stderr.strip().splitlines() or ['verification failed'])[0])
        ent = subprocess.run(['codesign', '-d', '--entitlements', ':-', str(app)],
                             capture_output=True, text=True).stdout
        add(PASS if 'get-task-allow' in ent else FAIL, 'entitlement get-task-allow',
            'present' if 'get-task-allow' in ent else 'absent; JIT refuses this signature')
        add(PASS if 'increased-memory-limit' in ent else WARN, 'entitlement increased-memory-limit',
            'present' if 'increased-memory-limit' in ent else 'absent; a 6-8 GB device raises OOM risk')
    else:
        # Without codesign, the entitlements XML still sits inside the binary.
        blob = binary.read_bytes() if binary.is_file() else b''
        add(PASS if b'get-task-allow' in blob else WARN, 'entitlement get-task-allow',
            'found in the binary' if b'get-task-allow' in blob
            else 'not found; expected only after signing with a development profile')
    if (app / 'embedded.mobileprovision').exists():
        add(WARN, 'embedded.mobileprovision', 'present; expected only for a signed build')

    width = max(len(r[1]) for r in rows) + 2
    for status, subject, detail in rows:
        print('%s %-*s %s' % (MARK[status], width, subject, detail))
    fails = sum(1 for r in rows if r[0] == FAIL)
    warns = sum(1 for r in rows if r[0] == WARN)
    print('\n%d checks: %d ok, %d warn, %d fail'
          % (len(rows), len(rows) - fails - warns, warns, fails))
    if fails:
        print('Do not install this bundle.')
        return 1
    print('Next: sign, install on the iPad, then Enable JIT (docs/INSTALL_IPHONE_ZH.md).')
    return 0


if __name__ == '__main__':
    sys.exit(main())
