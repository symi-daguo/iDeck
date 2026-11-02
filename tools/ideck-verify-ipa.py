#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Madeira Converter Exception: see LICENSE-EXCEPTION.md
"""Check that an iDeck .ipa is a complete, installable-shaped package.

Catches the packaging mistakes that only surface after a sideloader has already
spent ten minutes: a dropped JIT helper extension, a missing StikJIT framework,
an empty Wine DLL farm, or a bundle identifier that does not match the profile.

Works on any OS. It reads the archive only; it cannot tell you whether the code
runs, whether JIT attaches, or whether Steam signs in.

    python3 tools/ideck-verify-ipa.py build/iDeck.ipa --expect-bundle-id com.symi.ideck.steamui
"""

import argparse
import hashlib
import os
import plistlib
import sys
import zipfile

PASS, WARN, FAIL = 'ok', 'warn', 'fail'
MARK = {PASS: '[ ok ]', WARN: '[warn]', FAIL: '[FAIL]'}
APP = 'Payload/Madeira.app/'
rows = []


def add(status, subject, detail=''):
    rows.append((status, subject, detail))


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('ipa')
    parser.add_argument('--expect-bundle-id', default=None)
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args()

    try:
        zf = zipfile.ZipFile(args.ipa)
    except (OSError, zipfile.BadZipFile) as exc:
        print('not a readable .ipa: %s' % exc)
        return 1
    names = zf.namelist()
    if APP + 'Info.plist' not in names:
        print('no %sInfo.plist in the archive' % APP)
        return 1

    digest = hashlib.sha256()
    with open(args.ipa, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    total = sum(info.file_size for info in zf.infolist())
    add(PASS, 'archive', '%d entries, %.0f MB packed, %.0f MB installed, sha256 %s'
        % (len(names), os.path.getsize(args.ipa) / (1 << 20), total / (1 << 20),
           digest.hexdigest()[:16]))

    info = plistlib.loads(zf.read(APP + 'Info.plist'))
    bundle_id = info.get('CFBundleIdentifier', '')
    if args.expect_bundle_id and bundle_id != args.expect_bundle_id:
        add(FAIL, 'CFBundleIdentifier', '%s, expected %s' % (bundle_id, args.expect_bundle_id))
    else:
        add(PASS, 'CFBundleIdentifier', bundle_id)
    add(PASS if info.get('IDeckEnabled') is True else WARN, 'IDeckEnabled',
        'true (product shell on)' if info.get('IDeckEnabled') else 'missing; launcher may be hidden')
    schemes = [s for t in info.get('CFBundleURLTypes', []) for s in t.get('CFBundleURLSchemes', [])]
    add(PASS if schemes else FAIL, 'URL schemes', ', '.join(schemes) or 'none; JIT cannot return')
    add(PASS if info.get('MinimumOSVersion', '0') >= '26' else WARN, 'MinimumOSVersion',
        str(info.get('MinimumOSVersion')))

    helper = APP + 'PlugIns/MadeiraJITHelper.appex/'
    helper_names = [n for n in names if n.startswith(helper)]
    if not helper_names:
        add(FAIL, 'MadeiraJITHelper.appex',
            'missing; built-in JIT cannot work (fall back to StikDebug)')
    else:
        hinfo = plistlib.loads(zf.read(helper + 'Info.plist'))
        point = hinfo.get('NSExtension', {}).get('NSExtensionPointIdentifier', '')
        add(PASS if point == 'com.apple.ar.viewer' else WARN, 'MadeiraJITHelper.appex',
            '%d files, extension point %s' % (len(helper_names), point or 'unknown'))

    stikjit = APP + 'Frameworks/StikJIT.framework/StikJIT'
    if stikjit in names:
        add(PASS, 'Frameworks/StikJIT.framework', '%.1f MB' % (zf.getinfo(stikjit).file_size / (1 << 20)))
    else:
        add(FAIL, 'Frameworks/StikJIT.framework', 'missing; the helper cannot attach a debugger')

    for folder, floor in (('arm64ec-windows', 240), ('i386-windows', 650), ('aarch64-windows', 120)):
        count = sum(1 for n in names if n.startswith(APP + folder + '/'))
        add(PASS if count >= floor else WARN, folder,
            '%d files' % count if count else 'empty; the Wine farm is not bundled')

    for rel, label in (('prefix-template.tar.gz', 'Wine prefix template'),
                       ('d3d12/libmetalirconverter.dylib', 'Metal shader converter'),
                       ('madeira-jit.js', 'JIT debugger script'),
                       ('cacert.pem', 'CA bundle')):
        add(PASS if APP + rel in names else WARN, label, 'present' if APP + rel in names else 'missing')

    binary = APP + 'Madeira'
    if binary in names:
        blob = zf.read(binary)
        has_debug = b'get-task-allow' in blob
        add(PASS if has_debug else WARN, 'get-task-allow in signature',
            'found' if has_debug else 'not found; JIT will refuse to enable (re-sign with a development profile)')
    else:
        add(FAIL, 'app executable', 'missing')

    width = max(len(r[1]) for r in rows) + 2
    for status, subject, detail in rows:
        if args.quiet and status == PASS:
            continue
        print('%s %-*s %s' % (MARK[status], width, subject, detail))

    fails = sum(1 for r in rows if r[0] == FAIL)
    warns = sum(1 for r in rows if r[0] == WARN)
    print('\n%d checks: %d ok, %d warn, %d fail' % (len(rows), len(rows) - fails - warns,
                                                    warns, fails))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
