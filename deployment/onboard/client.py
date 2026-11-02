"""Local client; use SSH to run this on the laptop from another computer."""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def request(path, payload=None, port=8781):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:" + str(port) + path, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(exc.read().decode("utf-8", "replace")) from None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("health", "send", "read", "cancel", "reset"))
    parser.add_argument("--message-file")
    parser.add_argument("--id")
    parser.add_argument("--port", type=int, default=8781)
    args = parser.parse_args()
    if args.action == "send":
        if not args.message_file:
            parser.error("send needs --message-file")
        result = request("/v1/messages", {"id": args.id or str(uuid.uuid4()), "sender": "workstation",
                                         "message": Path(args.message_file).read_text(encoding="utf-8-sig")}, args.port)
    elif args.action == "read":
        if not args.id:
            parser.error("read needs --id")
        result = request("/v1/messages/" + args.id, port=args.port)
    elif args.action == "health":
        result = request("/health", port=args.port)
    else:
        result = request("/v1/" + args.action, {}, args.port)
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
