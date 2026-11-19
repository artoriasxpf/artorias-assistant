"""Cloud deployment entry point; deployment owns auth, storage and adapters."""
import argparse
import importlib
import uvicorn

def main():
    parser = argparse.ArgumentParser(description='Start a configured server application')
    parser.add_argument('--factory', required=True, help='module:callable returning an ASGI app')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args()
    module, name = args.factory.split(':', 1)
    app = getattr(importlib.import_module(module), name)()
    uvicorn.run(app, host=args.host, port=args.port)
