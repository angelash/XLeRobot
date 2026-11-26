"""Build the PICO launcher APK with the Android SDK command-line tools (no Gradle).

    python deployment/pico_vr/build_apk.py --sdk D:/androidSdk --jdk "C:/Program Files/Java/jdk-17"

The signing key is created on first use under ``.local-deploy/pico-vr-signing``
(ignored by Git) and reused, so later builds install as updates.
"""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SOURCE = HERE / 'android'
VERSION_CODE = 1
VERSION_NAME = '1.0'
MIN_SDK, TARGET_SDK = 24, 29


def newest(folder, need):
    candidates = [p for p in Path(folder).iterdir() if p.is_dir() and all((p / n).exists() for n in need)]
    if not candidates:
        raise SystemExit(f'{folder} 下找不到包含 {need} 的版本')
    def key(p):
        # 34.0.0 sorts above 34.0.0-rc3: a pre-release only wins with a higher number.
        number, _, tag = p.name.removeprefix('android-').partition('-')
        return [int(x) if x.isdigit() else -1 for x in number.split('.')] + [0 if tag else 1]
    return max(candidates, key=key)


def run(command, env=None):
    printable = ' '.join(str(c) for c in command)
    print('>', printable)
    subprocess.run([str(c) for c in command], check=True, env=env)


def build(sdk, jdk, out, signing):
    sdk, jdk, out, signing = Path(sdk), Path(jdk), Path(out), Path(signing)
    exe = '.exe' if os.name == 'nt' else ''
    bat = '.bat' if os.name == 'nt' else ''
    tools = newest(sdk / 'build-tools', [f'aapt2{exe}', f'd8{bat}', f'zipalign{exe}', f'apksigner{bat}'])
    platform = newest(sdk / 'platforms', ['android.jar'])
    android_jar = platform / 'android.jar'
    javac, keytool = jdk / 'bin' / f'javac{exe}', jdk / 'bin' / f'keytool{exe}'
    env = {**os.environ, 'JAVA_HOME': str(jdk), 'PATH': str(jdk / 'bin') + os.pathsep + os.environ.get('PATH', '')}
    work = out / 'build'
    if work.exists():
        shutil.rmtree(work)
    (work / 'gen').mkdir(parents=True)
    (work / 'classes').mkdir()
    (work / 'dex').mkdir()
    run([tools / f'aapt2{exe}', 'compile', '--dir', SOURCE / 'res', '-o', work / 'res.zip'], env)
    run([tools / f'aapt2{exe}', 'link', '-o', work / 'base.apk', '-I', android_jar,
         '--manifest', SOURCE / 'AndroidManifest.xml', '--java', work / 'gen',
         '--min-sdk-version', MIN_SDK, '--target-sdk-version', TARGET_SDK,
         '--version-code', VERSION_CODE, '--version-name', VERSION_NAME, work / 'res.zip'], env)
    sources = sorted((SOURCE / 'src').rglob('*.java')) + sorted((work / 'gen').rglob('*.java'))
    run([javac, '-encoding', 'UTF-8', '-source', '8', '-target', '8', '-Xlint:-options',
         '-bootclasspath', android_jar, '-d', work / 'classes', *sources], env)
    classes = sorted((work / 'classes').rglob('*.class'))
    run([tools / f'd8{bat}', '--release', '--min-api', MIN_SDK, '--lib', android_jar,
         '--output', work / 'dex', *classes], env)
    with zipfile.ZipFile(work / 'base.apk', 'a', zipfile.ZIP_DEFLATED) as apk:
        apk.write(work / 'dex' / 'classes.dex', 'classes.dex')
    run([tools / f'zipalign{exe}', '-f', '-p', '4', work / 'base.apk', work / 'aligned.apk'], env)
    signing.mkdir(parents=True, exist_ok=True)
    keystore, password = signing / 'launcher.jks', signing / 'storepass.txt'
    if not keystore.exists():
        password.write_text(secrets.token_urlsafe(24), encoding='ascii')
        run([keytool, '-genkeypair', '-keystore', keystore, '-alias', 'xlerobot-vr', '-keyalg', 'RSA',
             '-keysize', '2048', '-validity', '10000', '-storepass:file', password, '-keypass:file', password,
             '-dname', 'CN=XLeRobot VR Launcher'], env)
    out.mkdir(parents=True, exist_ok=True)
    apk = out / 'XLeRobotVR.apk'
    run([tools / f'apksigner{bat}', 'sign', '--ks', keystore, '--ks-key-alias', 'xlerobot-vr',
         '--ks-pass', f'file:{password}', '--out', apk, work / 'aligned.apk'], env)
    run([tools / f'apksigner{bat}', 'verify', '--min-sdk-version', MIN_SDK, apk], env)
    shutil.rmtree(work)
    for leftover in out.glob('*.idsig'):
        leftover.unlink()
    return apk


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--sdk', default=os.environ.get('ANDROID_HOME') or os.environ.get('ANDROID_SDK_ROOT'))
    parser.add_argument('--jdk', default=os.environ.get('JAVA_HOME'))
    parser.add_argument('--out', default=str(REPO / 'dist' / 'pico-vr-apk'))
    parser.add_argument('--signing', default=str(REPO / '.local-deploy' / 'pico-vr-signing'))
    args = parser.parse_args(argv)
    if not args.sdk or not args.jdk:
        raise SystemExit('需要 --sdk 与 --jdk（JDK 11 以上），或设置 ANDROID_HOME 与 JAVA_HOME')
    print(build(args.sdk, args.jdk, args.out, args.signing))


if __name__ == '__main__':
    main()
