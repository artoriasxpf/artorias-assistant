"""Local all-in-one launcher."""
import argparse
import os
from .bootstrap import create_app

def main():
    import uvicorn
    parser = argparse.ArgumentParser(description='Artorias local assistant')
    parser.add_argument('--data', default=None)
    parser.add_argument('--port', type=int, default=int(os.getenv('ARTORIAS_PORT', '8765')))
    args = parser.parse_args()
    uvicorn.run(create_app(args.data), host='127.0.0.1', port=args.port)
