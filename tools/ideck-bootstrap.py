#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Madeira Converter Exception: see LICENSE-EXCEPTION.md
"""Stage every input a public iDeck checkout needs before xcodebuild can link.

A clean clone is source-only. Four families of input are deliberately untracked:

  * the compiled Wine/FEX/DXMT runtime payloads and the app's resource folders,
    shipped inside the published IPA,
  * the LGPL relink archives (20 static libraries) from the relink companion,
  * FEX headers, which the app target #includes directly,
  * the Microsoft VC++ runtime DLLs, which nobody may redistribute.

This script fetches, verifies and lays them out exactly where
app/Madeira.xcodeproj expects them, then runs tools/ideck-preflight.py.
It never writes tracked files. It is idempotent: present inputs are kept.

Runs on macOS (the target) and on Windows/Linux (staging and inspection only,
where the actual compile cannot happen).

    python3 tools/ideck-bootstrap.py --assets ~/Downloads/ideck-alpha4
    python3 tools/ideck-bootstrap.py --mirror https://ghfast.top/
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

REPO_DEFAULT = Path(__file__).resolve().parents[1]

RELEASES = 'https://github.com/kuscholar/iDeck/releases/download'
PINNED = {
    # Submodule commits the checked-out tree points at (git ls-tree main).
    'fex_commit': 'f59cab95659e8add0c88f0c76ec1cf483c536dbf',
    'fex_url': 'https://github.com/willfaust/FEX.git',
    'fex_header_submodules': ('External/fmt', 'External/range-v3', 'External/unordered_dense'),
    'llvm_mingw': ('llvm-mingw-20260421-ucrt-macos-universal.tar.xz',
                   'bd85a3975723815cef28dbbd2ca2cb0c926f6b348a12a0453f39f7af273cb3f7',
                   116 << 20),
}
VCRUNTIME_DLLS = (
    'concrt140.dll', 'msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll',
    'msvcp140_atomic_wait.dll', 'msvcp140_codecvt_ids.dll', 'vcamp140.dll',
    'vccorlib140.dll', 'vcomp140.dll', 'vcruntime140.dll', 'vcruntime140_1.dll',
    'vcruntime140_threads.dll', 'mfc140.dll', 'mfc140u.dll',
)
# Resource folders shipped inside Payload/Madeira.app, and where the project
# reads each one from. Everything except Frameworks lands in app/Madeira.
APP_RESOURCES = ('aarch64-windows', 'arm64ec-windows', 'i386-windows', 'd3d12', 'nls',
                 'legal', 'licenses', 'prefix-template.tar.gz', 'cacert.pem',
                 'madeira-jit.js', 'Madeira JIT.shortcut', 'default.metallib',
                 'SteamClientPackages.json', 'ideck-control.exe')
# Relink archives the project references, grouped by the LIBRARY_SEARCH_PATHS
# entry that has to contain them (see LIBRARY_SEARCH_PATHS in project.pbxproj).
RELINK_PATHS = {
    'libFEXCore.a': 'FEX/build-ios/FEXCore/Source',
    'libFEXCore_Base.a': 'FEX/build-ios/FEXCore/Source',
    'libJemallocLibs.a': 'FEX/build-ios/FEXCore/Source',
    'libfmt.a': 'FEX/build-ios/External/fmt',
    'libcephes_128bit.a': 'FEX/build-ios/External/cephes',
    'libxxhash.a': 'FEX/build-ios/External/xxhash/cmake_unofficial',
    'libsoftfloat_3e.a': 'FEX/build-ios/External/SoftFloat-3e',
}

log_prefix = ''


def log(msg):
    print('%s%s' % (log_prefix, msg), flush=True)


def step(msg):
    print('\n== %s' % msg, flush=True)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def human(n):
    return '%.1f MB' % (n / (1 << 20))


def download(url, dest, expect_sha=None, min_bytes=1 << 20):
    """Download with resume. GitHub throughput is the bottleneck, not the CPU."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size >= min_bytes:
        if expect_sha is None or sha256(dest) == expect_sha:
            log('  cached  %s (%s)' % (dest.name, human(dest.stat().st_size)))
            return dest
        log('  hash mismatch, re-downloading %s' % dest.name)
        dest.unlink()
    part = dest.with_suffix(dest.suffix + '.part')
    for attempt in range(1, 6):
        have = part.stat().st_size if part.exists() else 0
        request = urllib.request.Request(url)
        if have:
            request.add_header('Range', 'bytes=%d-' % have)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                total = response.headers.get('Content-Length')
                total = have + int(total) if total else None
                mode = 'ab' if have and response.status == 206 else 'wb'
                if mode == 'wb':
                    have = 0
                last = -1
                with open(part, mode) as out:
                    while True:
                        chunk = response.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                        have += len(chunk)
                        if total and have * 10 // total != last:
                            last = have * 10 // total
                            log('  %3d%%  %s' % (last * 10, dest.name))
        except Exception as exc:  # noqa: BLE001
            log('  attempt %d failed: %s' % (attempt, exc))
            continue
        if expect_sha and sha256(part) != expect_sha:
            log('  attempt %d produced a bad hash' % attempt)
            part.unlink(missing_ok=True)
            continue
        part.replace(dest)
        log('  ok      %s (%s)' % (dest.name, human(dest.stat().st_size)))
        return dest
    raise SystemExit('could not fetch %s after 5 attempts' % url)


def release_url(tag, name, mirror):
    base = '%s/%s/%s' % (RELEASES, tag, name)
    return (mirror.rstrip('/') + '/' + base) if mirror else base


def expected_sha(assets, name):
    sums = assets / 'SHA256SUMS'
    if not sums.is_file():
        return None
    for line in sums.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip('*') == name:
            return parts[0]
    return None


def extract_all(tar, path):
    """extractall without tripping Python 3.12+'s filter deprecation, on 3.9 too."""
    try:
        tar.extractall(path, filter='tar')
    except TypeError:
        tar.extractall(path)


def run(cmd, cwd=None, check=True):
    printable = ' '.join(str(c) for c in cmd)
    log('  $ %s' % printable)
    result = subprocess.run(cmd, cwd=cwd, text=True)
    if check and result.returncode != 0:
        raise SystemExit('command failed (%d): %s' % (result.returncode, printable))
    return result.returncode


# ------------------------------------------------------------------ stages ----


def stage_ipa(args, root, assets):
    step('IPA runtime payloads')
    if not (assets / 'SHA256SUMS').is_file():
        # Without the published sums every download goes unverified.
        try:
            download(release_url(args.release, 'SHA256SUMS', args.mirror),
                     assets / 'SHA256SUMS', None, min_bytes=16)
        except SystemExit:
            log('  WARNING no SHA256SUMS; downloads cannot be verified against the release')
    name = 'iDeck-%s-build%s.ipa' % (args.version, args.build)
    ipa = assets / name
    if not ipa.is_file():
        download(release_url(args.release, name, args.mirror), ipa,
                 expected_sha(assets, name), min_bytes=100 << 20)
    work = assets / 'ipa-extract'
    app = work / 'Payload' / 'Madeira.app'
    if not app.is_dir():
        work.mkdir(parents=True, exist_ok=True)
        import zipfile
        log('  extracting %s' % name)
        with zipfile.ZipFile(ipa) as zf:
            for member in zf.infolist():
                if member.filename.startswith('Payload/'):
                    zf.extract(member, work)
    if not app.is_dir():
        raise SystemExit('IPA has no Payload/Madeira.app')
    copied = 0
    for item in APP_RESOURCES:
        src = app / item
        if not src.exists():
            log('  skip    %s (not in this IPA)' % item)
            continue
        dst = root / 'app' / 'Madeira' / item
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
            copied += 1
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            copied += 1
    log('  staged  %d app resources from the IPA' % copied)

    fw_src = app / 'Frameworks' / 'StikJIT.framework'
    fw_dst = root / 'app/Frameworks/StikJIT.xcframework/ios-arm64/StikJIT.framework'
    if fw_src.is_dir():
        shutil.copytree(fw_src, fw_dst, dirs_exist_ok=True)
        log('  staged  StikJIT.framework (%s)' % human(
            sum(p.stat().st_size for p in fw_dst.rglob('*') if p.is_file())))
    else:
        log('  WARNING IPA has no Frameworks/StikJIT.framework')


def stage_relink(args, root, assets):
    step('LGPL relink archives')
    name = 'iDeck-%s-relink.tar.gz' % args.version
    tar = assets / name
    if not tar.is_file():
        download(release_url(args.release, name, args.mirror), tar,
                 expected_sha(assets, name), min_bytes=20 << 20)
    out = assets / 'relink'
    if not (out / 'libraries').is_dir():
        out.mkdir(parents=True, exist_ok=True)
        log('  extracting %s' % name)
        with tarfile.open(tar) as tf:
            extract_all(tf, out)
    placed = 0
    for lib in sorted((out / 'libraries').glob('*.a')):
        rel = RELINK_PATHS.get(lib.name, 'app/Madeira')
        dst = (root / rel / lib.name) if rel.startswith('FEX') else (root / 'app/Madeira' / lib.name)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(lib, dst)
        placed += 1
    log('  staged  %d archives (%s)' % (placed, human(
        sum(p.stat().st_size for p in (out / 'libraries').glob('*.a')))))
    # Header search paths name these build directories; creating them keeps
    # xcodebuild from warning about a missing -I before the archives are used.
    for rel in set(RELINK_PATHS.values()):
        (root / rel).mkdir(parents=True, exist_ok=True)


def git(args, *parts, cwd=None, check=True):
    """git with the optional mirror applied to every github.com URL."""
    cmd = ['git']
    if args.mirror:
        cmd += ['-c', 'url.%s/https://github.com/.insteadOf=https://github.com/'
                % args.mirror.rstrip('/')]
    cmd += [str(p) for p in parts]
    return run(cmd, cwd=cwd, check=check)


def stage_fex(args, root):
    step('FEX source (headers the app target #includes)')
    fex = root / 'FEX'
    sentinel = fex / 'FEXCore/include/FEXCore/Core/Context.h'
    if sentinel.is_file() and (fex / 'External/fmt/include/fmt').is_dir():
        log('  cached  FEX headers already present')
    else:
        # Fetch the pinned commit directly rather than through `git submodule`,
        # which clones the whole default branch first and is the first thing to
        # break on a flaky link. A nested repository whose HEAD is the recorded
        # gitlink also keeps the parent tree clean.
        if not (fex / '.git').exists():
            # Only FEX/build-ios (staged archives) survives a re-initialisation;
            # git ignores it as untracked, and the relink stage re-creates it.
            if fex.exists() and not (fex / 'build-ios').exists():
                shutil.rmtree(fex)
            fex.mkdir(parents=True, exist_ok=True)
            git(args, 'init', '-q', cwd=fex)
            git(args, 'remote', 'add', 'origin', PINNED['fex_url'], cwd=fex)
        git(args, 'fetch', '--depth', '1', 'origin', PINNED['fex_commit'], cwd=fex)
        git(args, 'checkout', '-q', 'FETCH_HEAD', cwd=fex)
        for sub in PINNED['fex_header_submodules']:
            git(args, 'submodule', 'update', '--init', '--depth', '1', '--', sub, cwd=fex)
    for needed in ('FEXCore/include/FEXCore', 'FEXHeaderUtils', 'External/fmt/include/fmt',
                   'External/range-v3/include/range/v3', 'External/unordered_dense/include/ankerl'):
        path = fex / needed
        log('  %-6s %s' % ('ok' if path.exists() else 'MISS', needed))
    (fex / 'build-ios').mkdir(parents=True, exist_ok=True)


def stage_llvm_mingw(args, root):
    step('llvm-mingw (compiles ideck-control.exe during the build)')
    name, want, _ = PINNED['llvm_mingw']
    have = sorted((root / 'toolchains').glob('llvm-mingw-*/bin/x86_64-w64-mingw32-clang'))
    if have:
        log('  cached  %s' % have[0].relative_to(root))
        return
    tar = args.assets / name
    if not tar.is_file():
        url = ('https://github.com/mstorsjo/llvm-mingw/releases/download/20260421/' + name)
        download((args.mirror.rstrip('/') + '/' + url) if args.mirror else url, tar,
                 want, min_bytes=80 << 20)
    log('  extracting %s' % name)
    with tarfile.open(tar) as tf:
        extract_all(tf, root / "toolchains")
    have = sorted((root / 'toolchains').glob('llvm-mingw-*/bin/x86_64-w64-mingw32-clang'))
    log('  %s %s' % ('ok' if have else 'MISS', have[0].relative_to(root) if have else name))


def stage_vcruntime(args, root):
    step('Microsoft VC++ runtime (x64)')
    dst = root / 'app/Madeira/x86_64-vcruntime'
    dst.mkdir(parents=True, exist_ok=True)
    source = args.vcruntime_dir
    if not source and os.name == 'nt':
        # A licensed Windows install is an acceptable source (tools/fetch-vcruntime.md).
        source = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32')
        log('  using   %s (this Windows install)' % source)
    if source:
        src = Path(source)
        copied = 0
        for name in VCRUNTIME_DLLS:
            for candidate in (src / name, src / name.lower(), src / name.upper()):
                if candidate.is_file():
                    shutil.copy2(candidate, dst / name)
                    copied += 1
                    break
        log('  copied  %d/%d DLLs from %s' % (copied, len(VCRUNTIME_DLLS), src))
    else:
        present = {p.name.lower() for p in dst.glob('*.dll')}
        log('  present %d/%d DLLs' % (len(present), len(VCRUNTIME_DLLS)))
    missing = [n for n in VCRUNTIME_DLLS if not (dst / n).is_file()]
    if missing:
        log('  WARNING %d DLLs missing. The build still succeeds (the folder is copied as-is),'
            % len(missing))
        log('          but x86_64 guests need them. Fetch Microsoft\'s VC_redist.x64.exe and run:')
        log('            python3 tools/ideck-bootstrap.py --vcruntime-dir <extracted-cab-dir>')
        log('          see tools/fetch-vcruntime.md')


def stage_licenses(root):
    step('Generated licence copies (the build phase rejects stale ones)')
    script = root / 'build/stage-licenses.sh'
    shell = shutil.which('bash')
    # On Windows `bash` may be WSL's, which cannot see this checkout's paths.
    usable_shell = shell and 'system32' not in shell.lower().replace('\\', '/')
    if script.is_file() and usable_shell:
        if run([shell, str(script)], cwd=root, check=False) == 0:
            return
    if not script.is_file():
        log('  WARNING build/stage-licenses.sh missing')
    # Same two copies the script performs; the bytes are what the build compares.
    pairs = (('COPYING', 'LICENSE-MADEIRA-GPL-3.0.txt'),
             ('LICENSE-EXCEPTION.md', 'LICENSE-MADEIRA-EXCEPTION.txt'))
    for src, dst in pairs:
        target = root / 'app/Madeira/licenses' / dst
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / src, target)
        log('  copied  %s' % dst)


def preflight(root):
    step('Preflight')
    rc = run([sys.executable, str(root / 'tools/ideck-preflight.py'), '--repo', str(root)],
             cwd=root, check=False)
    return rc


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repo', type=Path, default=REPO_DEFAULT)
    parser.add_argument('--assets', type=Path, default=None,
                        help='cache directory for release assets (default: <repo>/.ideck-assets)')
    parser.add_argument('--release', default='v0.1.3-alpha.4')
    parser.add_argument('--version', default='0.1.3-alpha.4')
    parser.add_argument('--build', default='99')
    parser.add_argument('--mirror', default=os.environ.get('IDECK_MIRROR', ''),
                        help='prefix proxying github.com, e.g. https://ghfast.top/')
    parser.add_argument('--vcruntime-dir', default=os.environ.get('IDECK_VCRUNTIME_DIR'),
                        help='directory holding the 14 Microsoft x64 runtime DLLs')
    parser.add_argument('--skip-fex', action='store_true')
    parser.add_argument('--skip-llvm-mingw', action='store_true')
    parser.add_argument('--only-preflight', action='store_true')
    args = parser.parse_args()

    root = args.repo.resolve()
    args.assets = (args.assets or root / '.ideck-assets').resolve()
    args.assets.mkdir(parents=True, exist_ok=True)

    log('repository  %s' % root)
    log('assets      %s' % args.assets)
    log('release     %s (build %s)' % (args.release, args.build))
    if args.mirror:
        log('mirror      %s' % args.mirror)

    if not args.only_preflight:
        # FEX before the relink archives: the archive step writes into
        # FEX/build-ios, which a fresh FEX checkout would otherwise replace.
        if not args.skip_fex:
            stage_fex(args, root)
        stage_ipa(args, root, args.assets)
        stage_relink(args, root, args.assets)
        if not args.skip_llvm_mingw:
            stage_llvm_mingw(args, root)
        stage_vcruntime(args, root)
        stage_licenses(root)

    rc = preflight(root)
    if rc == 0:
        log('\nNext: scripts/mac/build.sh --team <TEAMID> --bundle-id com.you.ideck.steamui')
    return rc


if __name__ == '__main__':
    sys.exit(main())
