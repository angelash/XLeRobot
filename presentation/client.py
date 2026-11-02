"""Publish explicit public progress/media through the local presence interface."""
import argparse
import json
from pathlib import Path
from urllib.request import Request, urlopen

from .api import local_peer


def main():
    parser = argparse.ArgumentParser(description='发布可公开的过程事件；不读取私有引擎诊断')
    parser.add_argument('command', choices=['emit', 'media'])
    parser.add_argument('--url', default='http://127.0.0.1:8780')
    parser.add_argument('--file', required=True)
    args = parser.parse_args()
    endpoint = local_peer(args.url)
    data = Path(args.file).read_bytes()
    route = 'events' if args.command == 'emit' else 'media'
    if route == 'events':
        json.loads(data.decode('utf-8-sig'))
        data = data.removeprefix(b'\xef\xbb\xbf')
    request = Request(endpoint + '/api/presence/' + route, data=data,
                      headers={'Content-Type': 'application/json' if route == 'events' else 'application/octet-stream',
                               'X-Presence-Producer': '1'})
    with urlopen(request, timeout=8) as response:
        print(response.read().decode('utf-8'))


if __name__ == '__main__':
    main()
