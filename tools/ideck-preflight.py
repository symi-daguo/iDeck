#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Madeira Converter Exception: see LICENSE-EXCEPTION.md
"""Check that a checkout carries every input the Madeira/iDeck app target needs.

A clean clone of the public source is source-only: compiled runtime payloads,
the LGPL relink archives and the Microsoft VC++ runtime are not tracked
(docs/PUBLIC_SOURCE_EXCLUSIONS.json, .gitignore). `scripts/mac/bootstrap.sh`
stages them. This tool reads the Xcode project itself, so it fails when the
project references something the checkout does not have -- which is exactly
what `xcodebuild` would fail on, but seconds earlier and on any OS.

It runs without Xcode. It does not compile anything, and passing here is not
evidence that a link or a launch will succeed.

Exit codes: 0 ready, 1 blocking input missing, 2 ready with warnings.
"""

import argparse
import hashlib
import plistlib
import re
import shutil
import sys
from pathlib import Path

PASS, WARN, FAIL = 'ok', 'warn', 'fail'
MARK = {PASS: '[ ok ]', WARN: '[warn]', FAIL: '[FAIL]'}

# Payload counts observed in the published Build 99 IPA inventory. A checkout
# that stages a different release may differ; falling below the floor means the
# staging step copied an incomplete tree, not that the release changed.
FLOORS = {
    'app/Madeira/arm64ec-windows': 240,
    'app/Madeira/i386-windows': 650,
    'app/Madeira/aarch64-windows': 120,
    'app/Madeira/nls': 10,
    'app/Frameworks/StikJIT.xcframework/ios-arm64/StikJIT.framework': 5,
}

VCRUNTIME_DLLS = (
    'concrt140.dll', 'msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll',
    'msvcp140_atomic_wait.dll', 'msvcp140_codecvt_ids.dll', 'vcamp140.dll',
    'vccorlib140.dll', 'vcomp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll',
    'vcruntime140_threads.dll', 'mfc140.dll', 'mfc140u.dll',
)

ENTITLEMENT_KEYS = {
    'get-task-allow',
    'com.apple.security.cs.allow-jit',
    'com.apple.developer.kernel.increased-memory-limit',
}

results = []


def add(status, area, subject, detail=''):
    results.append((status, area, subject, detail))


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def count_files(path):
    return sum(1 for p in path.rglob('*') if p.is_file())


# ---------------------------------------------------------------- pbxproj ----


class Ref:
    def __init__(self, ident, name, path, tree, kind):
        self.ident, self.name, self.path = ident, name, path
        self.tree, self.kind = tree, kind


def parse_pbxproj(text):
    refs = {}
    # Both the main-branch layout (two tabs, /* comment */) and the
    # automatic-steam-install branch's (eight spaces, no comment) must parse.
    pattern = re.compile(
        r'^[\t ]*([0-9A-F]{8,32})\b(?: /\* (.*?) \*/)? = \{isa = PBXFileReference;([^}]*)\};',
        re.MULTILINE)
    for m in pattern.finditer(text):
        body = m.group(3)

        def field(key):
            found = re.search(r'\b%s = ("(?:[^"\\]|\\.)*"|[^;]+);' % key, body)
            if not found:
                return None
            value = found.group(1).strip()
            return value[1:-1] if value.startswith('"') else value

        path = field('path')
        name = m.group(2) or (path.rsplit('/', 1)[-1] if path else None)
        refs[m.group(1)] = Ref(m.group(1), name, path,
                               field('sourceTree') or '<group>',
                               field('lastKnownFileType') or '')
    return refs


def resolve(ref, root):
    """Resolve a <group> reference. The app group is app/Madeira."""
    if not ref.path:
        return None
    path = ref.path
    if path.startswith('$(SRCROOT)'):
        path = path.replace('$(SRCROOT)', 'app', 1)
        return (root / path).resolve()
    bases = [root / 'app' / 'Madeira', root / 'app', root]
    candidate = None
    for base in bases:
        try:
            candidate = (base / path).resolve()
        except OSError:
            continue
        if candidate.exists():
            return candidate
    return (root / 'app' / 'Madeira' / path).resolve()


# ----------------------------------------------------------------- checks ----


def check_project_links(root, refs):
    """Every archive.ar / framework the project links must exist on disk."""
    archives = [r for r in refs.values() if r.kind == 'archive.ar']
    if not archives:
        add(FAIL, 'project', 'static archives', 'no archive.ar references parsed; pbxproj format changed')
        return
    missing = []
    for ref in archives:
        target = resolve(ref, root)
        if target is None or not target.is_file():
            missing.append(ref.name)
    if missing:
        add(FAIL, 'project', 'static archives (Frameworks phase)',
            '%d/%d missing: %s' % (len(missing), len(archives), ', '.join(sorted(missing)[:6])
                                   + (', ...' if len(missing) > 6 else '')))
    else:
        total = sum(resolve(r, root).stat().st_size for r in archives) / (1 << 20)
        add(PASS, 'project', 'static archives (Frameworks phase)',
            '%d present, %.0f MB linked in' % (len(archives), total))


def check_resources(root, refs):
    """Folder references in the Resources phase must exist and be populated."""
    folders = [r for r in refs.values() if r.kind == 'folder']
    for ref in sorted(folders, key=lambda r: r.name):
        target = resolve(ref, root)
        rel = None
        try:
            rel = target.relative_to(root).as_posix()
        except ValueError:
            pass
        if target is None or not target.is_dir():
            add(FAIL, 'resources', ref.name, 'missing directory (Resources phase copies it)')
            continue
        files = count_files(target)
        floor = FLOORS.get(rel)
        if floor and files < floor:
            add(FAIL, 'resources', ref.name, '%d files, expected >= %d' % (files, floor))
        elif files == 0 and ref.name == 'x86_64-vcruntime':
            # Copied as-is; an empty folder builds, it only starves x86_64 guests.
            add(PASS, 'resources', ref.name, 'empty but present; see the runtime check below')
        elif files == 0:
            add(FAIL, 'resources', ref.name, 'directory is empty')
        else:
            add(PASS, 'resources', ref.name, '%d files' % files)


def check_stikjit(root, refs):
    fw = [r for r in refs.values() if 'StikJIT' in (r.name or '')]
    binary = root / 'app/Frameworks/StikJIT.xcframework/ios-arm64/StikJIT.framework/StikJIT'
    if not binary.is_file():
        add(FAIL, 'jit', 'StikJIT.framework/StikJIT',
            'missing binary; the JIT helper target links -framework StikJIT')
        return
    size = binary.stat().st_size
    add(PASS if size > 1 << 20 else FAIL, 'jit', 'StikJIT.framework/StikJIT',
        '%.1f MB' % (size / (1 << 20)))
    for extra in ('Modules/module.modulemap', 'Headers/StikJIT-Swift.h'):
        path = binary.parent / extra
        add(PASS if path.is_file() else WARN, 'jit', 'StikJIT ' + extra,
            '' if path.is_file() else 'missing; Swift import may fail')


def check_vcruntime(root):
    folder = root / 'app/Madeira/x86_64-vcruntime'
    if not folder.is_dir():
        add(FAIL, 'runtime', 'x86_64-vcruntime',
            'missing folder reference; run scripts/mac/bootstrap.sh')
        return
    present = {p.name.lower() for p in folder.glob('*.dll')}
    missing = [d for d in VCRUNTIME_DLLS if d not in present]
    if missing:
        add(WARN, 'runtime', 'x86_64-vcruntime',
            '%d/%d Microsoft DLLs; missing %s' % (len(present), len(VCRUNTIME_DLLS),
                                                  ', '.join(missing[:4])))
    else:
        signed = 0
        for path in folder.glob('*.dll'):
            try:
                data = path.read_bytes()[:4096]
                import struct
                pe = struct.unpack_from('<I', data, 0x3c)[0]
                off, size = struct.unpack_from('<II', data, pe + 24 + 112 + 4 * 8)
                if size and off + size <= path.stat().st_size:
                    signed += 1
            except Exception:  # noqa: BLE001
                pass
        add(PASS, 'runtime', 'x86_64-vcruntime',
            '14/14 DLLs, %d still carry Microsoft\'s signature payload' % signed)


def check_licenses(root):
    for src, dst in (('COPYING', 'LICENSE-MADEIRA-GPL-3.0.txt'),
                     ('LICENSE-EXCEPTION.md', 'LICENSE-MADEIRA-EXCEPTION.txt')):
        target = root / 'app/Madeira/licenses' / dst
        if not target.is_file():
            add(FAIL, 'licenses', dst, 'missing; the "Sign bundled dylibs" phase fails')
        elif sha256(target) != sha256(root / src):
            add(FAIL, 'licenses', dst, 'stale copy; run build/stage-licenses.sh')
        else:
            add(PASS, 'licenses', dst, 'matches ' + src)


def check_prefix_template(root):
    path = root / 'app/Madeira/prefix-template.tar.gz'
    if not path.is_file():
        add(FAIL, 'runtime', 'prefix-template.tar.gz', 'missing; bundled as a resource')
    else:
        add(PASS, 'runtime', 'prefix-template.tar.gz', '%.1f MB' % (path.stat().st_size / (1 << 20)))


def check_plists(root):
    app_plist = root / 'app/Madeira/Info.plist'
    helper_plist = root / 'app/MadeiraJITHelper/Info.plist'
    try:
        info = plistlib.loads(app_plist.read_bytes())
    except Exception as exc:  # noqa: BLE001
        add(FAIL, 'plist', 'app Info.plist', 'unreadable: %s' % exc)
        return
    add(PASS, 'plist', 'app Info.plist', 'parsed')
    if info.get('IDeckEnabled') is True:
        add(PASS, 'plist', 'IDeckEnabled', 'product shell enabled')
    else:
        add(WARN, 'plist', 'IDeckEnabled', 'not true; a non-.steamui bundle ID disables the product shell')
    schemes = [s for t in info.get('CFBundleURLTypes', []) for s in t.get('CFBundleURLSchemes', [])]
    if schemes:
        add(PASS, 'plist', 'URL schemes', ', '.join(schemes))
    else:
        add(FAIL, 'plist', 'URL schemes', 'no CFBundleURLSchemes; the JIT shortcut cannot return')
    if info.get('UIFileSharingEnabled') is True:
        add(PASS, 'plist', 'UIFileSharingEnabled', 'Files app can deliver payloads')
    try:
        helper = plistlib.loads(helper_plist.read_bytes())
        add(PASS, 'plist', 'helper Info.plist', helper.get('NSExtension', {}).get('NSExtensionPointIdentifier', '?'))
    except Exception as exc:  # noqa: BLE001
        add(FAIL, 'plist', 'helper Info.plist', 'unreadable: %s' % exc)


def check_entitlements(root):
    path = root / 'app/Madeira/Madeira.entitlements'
    try:
        ent = plistlib.loads(path.read_bytes())
    except Exception as exc:  # noqa: BLE001
        add(FAIL, 'entitlements', 'Madeira.entitlements', 'unreadable: %s' % exc)
        return
    unknown = set(ent) - ENTITLEMENT_KEYS
    if unknown:
        add(FAIL, 'entitlements', 'unknown keys', ', '.join(sorted(unknown)))
    if ent.get('get-task-allow') is not True:
        add(FAIL, 'entitlements', 'get-task-allow', 'false; JIT cannot be enabled')
    else:
        add(PASS, 'entitlements', 'get-task-allow', 'true (development signing only)')
    if ent.get('com.apple.developer.kernel.increased-memory-limit') is not True:
        add(WARN, 'entitlements', 'increased-memory-limit', 'false; 6 GB devices raise the OOM risk')
    else:
        add(PASS, 'entitlements', 'increased-memory-limit', 'true (team must allow the capability)')
    add(PASS if ent.get('com.apple.security.cs.allow-jit') is True else WARN,
        'entitlements', 'allow-jit', 'present' if ent.get('com.apple.security.cs.allow-jit') else 'missing')


def check_build_settings(root):
    text = (root / 'app/Madeira.xcodeproj/project.pbxproj').read_text(encoding='utf-8', errors='replace')
    targets = sorted(set(re.findall(r'IPHONEOS_DEPLOYMENT_TARGET = ([0-9.]+);', text)))
    if '26.0' in targets:
        add(PASS, 'settings', 'IPHONEOS_DEPLOYMENT_TARGET',
            '%s (app target 26.0; device must run iOS/iPadOS 26+)' % ', '.join(targets))
    else:
        add(FAIL, 'settings', 'IPHONEOS_DEPLOYMENT_TARGET', ', '.join(targets) or 'not set')
    bundle_ids = sorted(set(re.findall(r'MADEIRA_BUNDLE_IDENTIFIER = ([^;]+);', text)))
    add(PASS if bundle_ids else FAIL, 'settings', 'MADEIRA_BUNDLE_IDENTIFIER',
        ', '.join(bundle_ids) or 'not set')
    if '$(MADEIRA_BUNDLE_IDENTIFIER).JITHelper' in text:
        add(PASS, 'settings', 'helper bundle id', '$(MADEIRA_BUNDLE_IDENTIFIER).JITHelper')
    if 'CODE_SIGN_STYLE = Automatic;' in text:
        add(PASS, 'settings', 'CODE_SIGN_STYLE', 'Automatic')
    if 'x86_64-w64-mingw32-clang' in (root / 'build/ideck-control/bundle.sh').read_text(errors='replace'):
        mingw = None
        for cand in sorted((root / 'toolchains').glob('llvm-mingw-*/bin/x86_64-w64-mingw32-clang')):
            mingw = cand
        add(PASS if mingw else WARN, 'toolchain', 'llvm-mingw x86_64-w64-mingw32-clang',
            str(mingw.relative_to(root)) if mingw else
            'not staged; build/ideck-control/bundle.sh fails without LLVM_MINGW')


def check_fex_headers(root):
    needed = [
        'FEX/FEXCore/include/FEXCore/Core/Context.h',
        'FEX/FEXHeaderUtils/FEXHeaderUtils',
        'FEX/External/fmt/include/fmt',
        'FEX/External/range-v3/include/range/v3',
        'FEX/External/unordered_dense/include/ankerl',
    ]
    missing = [p for p in needed if not (root / p).exists()]
    if missing:
        add(FAIL, 'fex', 'FEX headers',
            'missing: %s (app sources #include <FEXCore/...>; run scripts/mac/bootstrap.sh)'
            % ', '.join(m.split('/')[-1] for m in missing))
    else:
        add(PASS, 'fex', 'FEX headers', '%d include roots present' % len(needed))


def check_misc(root):
    for rel in ('app/Madeira/madeira-jit.js', 'app/Madeira/Madeira JIT.shortcut',
                'app/Madeira/cacert.pem'):
        path = root / rel
        if path.exists():
            add(PASS, 'misc', rel.split('/')[-1], 'present')
        else:
            add(WARN, 'misc', rel.split('/')[-1], 'missing')
    for rel in ('app/Madeira/SteamClientPackages.json', 'app/Madeira/ideck-control.exe'):
        path = root / rel
        add(PASS if path.exists() else WARN, 'misc', rel.split('/')[-1],
            'staged' if path.exists() else 'absent; automatic Steam install cannot run')


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repo', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--quiet', action='store_true', help='only show non-passing checks')
    args = parser.parse_args()
    root = args.repo.resolve()

    pbxproj = root / 'app/Madeira.xcodeproj/project.pbxproj'
    if not pbxproj.is_file():
        print('not a checkout: %s' % pbxproj)
        return 1
    refs = parse_pbxproj(pbxproj.read_text(encoding='utf-8', errors='replace'))

    check_project_links(root, refs)
    check_resources(root, refs)
    check_stikjit(root, refs)
    check_vcruntime(root)
    check_licenses(root)
    check_prefix_template(root)
    check_plists(root)
    check_entitlements(root)
    check_build_settings(root)
    check_fex_headers(root)
    check_misc(root)

    width = max(len(r[2]) for r in results) + 2
    area = None
    for status, group, subject, detail in results:
        if args.quiet and status == PASS:
            continue
        if group != area:
            print('\n%s' % group.upper())
            area = group
        print('  %s %-*s %s' % (MARK[status], width, subject, detail))

    fails = sum(1 for r in results if r[0] == FAIL)
    warns = sum(1 for r in results if r[0] == WARN)
    print('\n%d checks: %d ok, %d warn, %d fail' % (len(results), len(results) - fails - warns,
                                                    warns, fails))
    if fails:
        print('NOT READY: stage the inputs with scripts/mac/bootstrap.sh, then re-run.')
        return 1
    if warns:
        print('READY with warnings. Warnings above do not stop xcodebuild.')
        return 2
    print('READY: every input the project references is present.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
