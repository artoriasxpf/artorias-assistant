"""Standalone service entry point, independent of assistant runtime imports."""
import argparse
import uvicorn


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8766)
    args = parser.parse_args()
    uvicorn.run('artorias_search_service.app:create_app', factory=True,
                host=args.host, port=args.port)


if __name__ == '__main__':
    main()
