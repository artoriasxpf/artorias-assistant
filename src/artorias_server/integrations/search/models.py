"""Domain-independent search contracts. Connections/transactions belong to callers."""
from collections.abc import Iterable
from dataclasses import dataclass
from sqlite3 import Connection
from typing import Literal, Protocol

Mode = Literal['any', 'all']


class IndexUnavailable(RuntimeError):
    """Missing index, missing FTS5, or incompatible index version; rebuild required."""


@dataclass(frozen=True)
class Scope:
    collection: str
    partition: str


@dataclass(frozen=True)
class Document:
    id: str
    title: str = ''
    keywords: str = ''
    body: str = ''


@dataclass(frozen=True)
class SearchHit:
    id: str
    score: float  # SQLite BM25: smaller (more negative) means more relevant.


@dataclass(frozen=True)
class WriteResult:
    updated: int
    skipped: int


class TextIndex(Protocol):
    def initialize(self, conn: Connection) -> None: ...
    def is_current(self, conn: Connection, collection: str, projection_version: str) -> bool: ...
    def upsert(self, conn: Connection, scope: Scope, documents: Iterable[Document], *,
               projection_version: str) -> WriteResult: ...
    def delete(self, conn: Connection, scope: Scope, document_ids: Iterable[str]) -> int: ...
    def search(self, conn: Connection, scope: Scope, query: str, *, projection_version: str,
               limit: int = 20, mode: Mode = 'any') -> list[SearchHit]: ...
    def rebuild(self, conn: Connection, collection: str, documents: Iterable[tuple[str, Document]], *,
                projection_version: str) -> int: ...
