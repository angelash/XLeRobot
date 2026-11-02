"""Fast-forward a clean laptop source checkout; never touch the robot runtime."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path


def sync_once(git, workspace, timeout=90):
    workspace = Path(workspace).resolve()
    if not (workspace / ".git").exists():
        raise ValueError("Expected an existing, separate Git source checkout")

    def run(*arguments, check=True):
        result = subprocess.run([str(git), "-c", "http.sslBackend=schannel", "-C", str(workspace), *arguments],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        if check and result.returncode:
            raise RuntimeError("Git operation failed: " + arguments[0])
        return result

    dirty = run("status", "--porcelain").stdout.strip()
    if dirty:
        return {"state": "blocked_local_changes", "paths": dirty.splitlines(), "at": time.time()}
    before = run("rev-parse", "HEAD").stdout.strip()
    run("fetch", "--no-tags", "origin", "main")
    remote = run("rev-parse", "refs/remotes/origin/main").stdout.strip()
    if remote == before:
        return {"state": "current", "head": before, "at": time.time()}
    if run("merge-base", "--is-ancestor", before, remote, check=False).returncode:
        return {"state": "blocked_divergence", "head": before, "remote": remote, "at": time.time()}
    # Recheck after the network wait so an edit made during fetch is preserved.
    if run("status", "--porcelain").stdout.strip():
        return {"state": "blocked_local_changes", "at": time.time()}
    run("merge", "--ff-only", remote)
    if run("status", "--porcelain").stdout.strip():
        raise RuntimeError("Checkout is dirty after synchronization")
    return {"state": "updated", "previous": before, "head": remote, "at": time.time()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--git", required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()
    print(json.dumps(sync_once(args.git, args.workspace), ensure_ascii=False))


if __name__ == "__main__":
    main()
