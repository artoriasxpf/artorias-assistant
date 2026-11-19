"""Local all-in-one launcher."""
import argparse
import os
import threading
import webbrowser

from .bootstrap import create_app


def main():
    import uvicorn
    parser = argparse.ArgumentParser(description='Artorias local assistant')
    parser.add_argument('--data', default=None)
    parser.add_argument('--port', type=int, default=int(os.getenv('ARTORIAS_PORT', '8765')))
    parser.add_argument('--no-browser', action='store_true', help='启动后不自动打开浏览器')
    args = parser.parse_args()
    url = f'http://127.0.0.1:{args.port}'
    timer = None
    if not args.no_browser:
        timer = threading.Timer(0.8, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    try:
        uvicorn.run(create_app(args.data), host='127.0.0.1', port=args.port)
    finally:
        if timer is not None:
            timer.cancel()
