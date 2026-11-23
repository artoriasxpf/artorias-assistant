"""Standalone service entry point, independent of assistant runtime imports."""
import argparse
import uvicorn

from .config import DEFAULT_HOST, DEFAULT_PORT


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default=DEFAULT_HOST)
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    uvicorn.run('artorias_search_service.api.app:create_app', factory=True,
                host=args.host, port=args.port)


if __name__ == '__main__':
    main()
