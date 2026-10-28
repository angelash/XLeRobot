"""Run a separate, hardware-free original Xbox preview on loopback:8766."""
import os


def main():
    os.environ['XLEROBOT_PREVIEW_ONLY'] = '1'
    import uvicorn
    uvicorn.run('fabrication.assembly.debug_console.app:app', host='127.0.0.1',
                port=8766, log_level='warning')


if __name__ == '__main__':
    main()
