"""python -m presentation.server --port 8780 (no camera or engine starts)."""
import argparse

from .api import create_app
from .state import DEFAULT_DATA


def main():
    parser = argparse.ArgumentParser(description='机器人形象与过程展示，仅监听本机')
    parser.add_argument('--port', type=int, default=8780)
    parser.add_argument('--data-dir', default=str(DEFAULT_DATA))
    parser.add_argument('--onboard-url', help='本机观察/规划消息桥，例如 http://127.0.0.1:8781')
    parser.add_argument('--controller-url', help='本机相机来源，例如 http://127.0.0.1:8765；只在手动观看时连接')
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(create_app(args.data_dir, args.onboard_url, args.controller_url, args.port),
                host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
