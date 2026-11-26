"""Assemble the PICO VR install package on the workstation.

    python deployment/pico_vr/build_package.py --base 62e9111

Output (under ``dist/``, ignored by Git):
  xlerobot-pico-vr-<date>/
    README.txt                 short Chinese steps
    PICO_VR_ZH.md              full guide
    pico/XLeRobotVR.apk        headset launcher (address, connection check, certificate, open page)
    pico/xlerobot-studio-ca.crt
    vehicle/install-pico-vr.ps1, manifest.json, payload/..., studio-tls/...
    SHA256SUMS.txt
  xlerobot-pico-vr-<date>.zip

``--base`` is the commit the robot laptop currently runs; its file versions are
the only ones the installer will replace. The Studio server key is included
for the laptop only. The CA private key never leaves .local-deploy.
"""
from __future__ import annotations

import argparse
import codecs
import datetime
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

CONSOLE = 'fabrication/assembly/debug_console/'
FILES = [
    CONSOLE + name for name in (
        'web_teleop.py', 'web_teleop_api.py', 'original_runtime.py', 'vr_kinematics.py',
        'studio_tls.py', 'studio_cert.py', 'platform_app.py', 'platform_catalog.py', 'start-platform.ps1',
        'platform_static/vr.html', 'platform_static/vr.js', 'platform_static/vr-mapping.js', 'platform_static/vr.css',
        'platform_static/index.html', 'platform_static/studio.js', 'platform_static/diagrams.js',
        'test_vr_teleop.py', 'test_studio_tls.py', 'test_platform.py', 'test_vr_mapping.cjs', 'test_vr_client.cjs',
        'PICO_VR_ZH.md', 'PICO_VR_REQUIREMENTS_ZH.md', 'STUDIO_ZH.md', 'WEB_TELEOP_ZH.md')
] + ['deployment/fieldmap/journal.py', 'deployment/windows/configure-studio-network.ps1',
     'deployment/pico_vr/install-vehicle.ps1']


def sha(data):
    return hashlib.sha256(data).hexdigest()


def base_versions(path, base):
    """Hashes the vehicle copy may have: the base blob with LF or CRLF endings."""
    try:
        blob = subprocess.run(['git', 'show', f'{base}:{path}'], cwd=REPO, check=True, capture_output=True).stdout
    except subprocess.CalledProcessError:
        return []
    lf = blob.replace(b'\r\n', b'\n')
    return sorted({sha(blob), sha(lf), sha(lf.replace(b'\n', b'\r\n'))})


def build(base, addresses, apk, out):
    from fabrication.assembly.debug_console import studio_cert
    # A typo must not turn every existing vehicle file into a "new file".
    base = subprocess.run(['git', 'rev-parse', '--verify', f'{base}^{{commit}}'],
                          cwd=REPO, check=True, capture_output=True, text=True).stdout.strip()
    stamp = datetime.date.today().strftime('%Y%m%d')
    root = Path(out) / f'xlerobot-pico-vr-{stamp}'
    if root.exists():
        shutil.rmtree(root)
    vehicle = root / 'vehicle'
    entries = []
    for path in FILES:
        source = REPO / path
        data = source.read_bytes()
        target = vehicle / 'payload' / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        entries.append({'path': path, 'sha256': sha(data), 'accepted': base_versions(path, base)})
    (vehicle / 'manifest.json').write_text(json.dumps(
        {'schema': 1, 'kind': 'pico_vr_update', 'base_commit': base, 'built_at': datetime.datetime.now().astimezone().isoformat(),
         'files': entries}, ensure_ascii=False, indent=2), encoding='utf-8')
    shutil.copy2(HERE / 'install-vehicle.ps1', vehicle / 'install-pico-vr.ps1')
    tls = REPO / '.local-deploy' / 'studio-tls'
    info = studio_cert.create(tls, addresses=addresses)
    (vehicle / 'studio-tls').mkdir()
    for name in ('server.crt', 'server.key', 'ca.crt'):
        shutil.copy2(tls / name, vehicle / 'studio-tls' / name)
    pico = root / 'pico'
    pico.mkdir()
    shutil.copy2(apk, pico / 'XLeRobotVR.apk')
    shutil.copy2(tls / 'ca.crt', pico / 'xlerobot-studio-ca.crt')
    shutil.copy2(REPO / CONSOLE / 'PICO_VR_ZH.md', root / 'PICO_VR_ZH.md')
    host = addresses[0]
    readme = (HERE / 'PACKAGE_README.txt').read_text(encoding='utf-8')
    readme = readme.replace('{HOST}', host).replace('{CA_SHA256}', info['ca_sha256']).replace('{BASE}', base)
    # Notepad on the laptop reads a BOM-marked UTF-8 file correctly.
    bom = codecs.BOM_UTF8
    (root / 'README.txt').write_bytes(bom + readme.replace('\n', '\r\n').encode('utf-8'))
    sums = [f'{sha(p.read_bytes())}  {p.relative_to(root).as_posix()}' for p in sorted(root.rglob('*')) if p.is_file()]
    (root / 'SHA256SUMS.txt').write_text('\n'.join(sums) + '\n', encoding='utf-8')
    archive = root.with_suffix('.zip')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for p in sorted(root.rglob('*')):
            if p.is_file():
                z.write(p, Path(root.name) / p.relative_to(root))
    return root, archive, info


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--base', required=True, help='commit currently on the robot laptop')
    parser.add_argument('--ip', action='append', default=[], help='robot address for the certificate (repeatable)')
    parser.add_argument('--apk', default=str(REPO / 'dist' / 'pico-vr-apk' / 'XLeRobotVR.apk'))
    parser.add_argument('--out', default=str(REPO / 'dist'))
    args = parser.parse_args(argv)
    if not Path(args.apk).is_file():
        raise SystemExit('先运行 deployment/pico_vr/build_apk.py 生成 APK')
    root, archive, info = build(args.base, args.ip or ['192.168.137.150'], args.apk, args.out)
    print(f'folder: {root}\narchive: {archive}\nCA SHA-256: {info["ca_sha256"]}\ncertificate addresses: {info["addresses"]}')


if __name__ == '__main__':
    main()
