"""Export a legacy Blob-only session into a NEW directory; never overwrite files."""
import argparse
import hashlib
import sqlite3
from pathlib import Path


def export_legacy(data, session, destination):
    data, destination = Path(data).resolve(), Path(destination).expanduser().resolve()
    with sqlite3.connect(f'{(data/"app.sqlite").as_uri()}?mode=ro', uri=True) as c:
        rows = c.execute('''SELECT path,after_hash FROM changes WHERE session=? AND rowid IN
            (SELECT MAX(rowid) FROM changes WHERE session=? GROUP BY path)''', (session,session)).fetchall()
    contents = []
    for name, h in rows:
        if h is None:
            continue
        p = Path(name)
        if not p.parts or p.is_absolute() or '..' in p.parts:
            raise ValueError('历史路径非法')
        if len(h) != 64 or any(x not in '0123456789abcdef' for x in h):
            raise ValueError('历史哈希非法')
        raw = (data/'blobs'/h).read_bytes()
        if hashlib.sha256(raw).hexdigest() != h:
            raise ValueError('备份校验失败')
        contents.append((p,raw))
    # Require a new directory even when an existing one happens to be empty.
    destination.mkdir(parents=True, exist_ok=False)
    for p, raw in contents:
        target = destination/p
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as f:
            f.write(raw)
    return len(contents)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--destination', required=True)
    args = parser.parse_args()
    print(f'Exported {export_legacy(args.data,args.session,args.destination)} files')
