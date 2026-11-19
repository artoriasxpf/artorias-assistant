#!/usr/bin/env python3
"""Preview or reset Artorias SQLite databases and Blob stores."""
import argparse
import os
from pathlib import Path
import shutil

from dotenv import dotenv_values, find_dotenv


def targets(data: Path) -> list[tuple[Path, bool]]:
    databases = [
        (data / (name + suffix), False)
        for name in ("app.sqlite", "checkpoints.sqlite")
        for suffix in ("", "-wal", "-shm", "-journal")
    ]
    return databases + [(data / "blobs", True), (data / "cloud" / "blobs", True)]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Preview removal of Artorias databases and Blob stores. Stop Artorias before using --apply."
    )
    parser.add_argument("--data", type=Path, help="Override ARTORIAS_DATA and the default ./data")
    parser.add_argument("--apply", action="store_true", help="Permanently remove the listed paths")
    args = parser.parse_args()

    env_file = find_dotenv(usecwd=True)
    configured = os.environ.get("ARTORIAS_DATA")
    if configured is None and env_file:
        configured = dotenv_values(env_file).get("ARTORIAS_DATA")
    data = (args.data or Path(configured if configured is not None else "data")).resolve()
    if data == Path(data.anchor) or not data.is_dir():
        parser.error(f"Expected an existing Artorias data directory: {data}")
    if (data / "cloud").is_symlink():
        parser.error(f"Refusing to follow a linked cloud directory: {data / 'cloud'}")

    existing = []
    for path, directory in targets(data):
        if path.is_symlink():
            parser.error(f"Refusing to remove a symbolic link: {path}")
        if path.exists():
            if not (path.is_dir() if directory else path.is_file()):
                parser.error(f"Unexpected path type: {path}")
            existing.append((path, directory))

    if not existing:
        print(f"No Artorias databases or Blob stores found in {data}")
        return

    print("Removing:" if args.apply else "Preview only. Would remove:")
    for path, directory in existing:
        print(f"  {path}", flush=True)
        if args.apply:
            if directory:
                shutil.rmtree(path)
            else:
                path.unlink()


if __name__ == "__main__":
    main()
