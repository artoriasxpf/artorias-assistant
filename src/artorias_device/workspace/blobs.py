"""Content backup abstraction. Placement follows resource location, not Task ownership."""
import hashlib
from pathlib import Path
from typing import Callable

from artorias_shared.workspace import BlobRef, BlobStore

class FilesystemBlobStore:
    def __init__(self, root, atomic_write: Callable):
        self.root=Path(root)
        self.atomic_write=atomic_write
        self.store_id=str(self.root)
    def put(self, data):
        h=hashlib.sha256(data).hexdigest()
        self.atomic_write(self.root/h,data,0o600)
        return BlobRef(self.store_id,h)
    def get(self, ref):
        if ref.store_id != self.store_id or len(ref.sha256)!=64 or any(c not in '0123456789abcdef' for c in ref.sha256):
            raise ValueError('非法备份引用')
        data=(self.root/ref.sha256).read_bytes()
        if hashlib.sha256(data).hexdigest()!=ref.sha256:raise ValueError('备份内容校验失败')
        return data
