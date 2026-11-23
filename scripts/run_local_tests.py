"""Run the local robot code's offline tests with one command.

    .venv-debug\\Scripts\\python.exe scripts/run_local_tests.py            # Python, pages, lessons file
    .venv-debug\\Scripts\\python.exe scripts/run_local_tests.py --only python -- -k grasp

Collected: pytest files and Node page scripts under the local trees below,
plus the format check of the pending lessons file. Left out: the upstream
software/ and simulation/ trees, and the two first-motion tests directly under
fabrication/assembly, which import lerobot and run in .venv-robot. Each part
runs even when an earlier one fails; the exit code is non-zero if any failed.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TREES = ('fabrication/assembly/debug_console', 'deployment', 'presentation', 'scripts')
SKIPPED_FOLDERS = {'node_modules', '__pycache__', 'evidence', 'platform-data'}


def node_scripts(root=ROOT, trees=TREES):
    found = []
    for tree in trees:
        for path in sorted((root/tree).rglob('test_*')):
            if path.suffix in ('.cjs', '.mjs') and not SKIPPED_FOLDERS & set(path.relative_to(root).parts):
                found.append(path)
    return found


def run_python(extra, run=subprocess.call):
    return run([sys.executable, '-m', 'pytest', *TREES, '-q', *extra], cwd=ROOT)


def run_node(run=subprocess.call, node=None):
    node = node or shutil.which('node')
    scripts = node_scripts()
    if not node:
        print('node 不在 PATH 中，页面脚本未运行：%d 个' % len(scripts))
        return 1
    failed = []
    for path in scripts:
        if run([node, str(path)], cwd=ROOT, stdout=subprocess.DEVNULL):
            failed.append(path.relative_to(ROOT).as_posix())
    print('页面脚本 %d 个，失败 %d 个%s' % (len(scripts), len(failed), '：' + '、'.join(failed) if failed else ''))
    return 1 if failed else 0


def run_lessons(run=subprocess.call):
    return run([sys.executable, '-X', 'utf8', '-m', 'deployment.autonomy.lessons', 'check'], cwd=ROOT)


PARTS = {'python': run_python, 'node': run_node, 'lessons': run_lessons}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--only', choices=tuple(PARTS), action='append', help='run just this part; repeatable')
    parser.add_argument('pytest_args', nargs='*', help='after --, passed to pytest')
    args = parser.parse_args(argv)
    results = {}
    for name in args.only or tuple(PARTS):
        began = time.monotonic()
        code = run_python(args.pytest_args) if name == 'python' else PARTS[name]()
        results[name] = (code, time.monotonic() - began)
    print()
    for name, (code, seconds) in results.items():
        print('%-8s %s  %.0f s' % (name, '通过' if code == 0 else '失败（退出码 %s）' % code, seconds))
    return 1 if any(code for code, _ in results.values()) else 0


if __name__ == '__main__':
    raise SystemExit(main())
