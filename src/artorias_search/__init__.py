"""Reusable lexical search, independent of Runtime Server and business models."""
from .contracts import Document, IndexUnavailable, Scope, SearchHit, TextIndex, TextTokenizer, WriteResult

__all__ = ['Document', 'IndexUnavailable', 'Scope', 'SearchHit', 'TextIndex',
           'TextTokenizer', 'WriteResult', 'SQLiteTextIndex', 'Tokenizer']


def __getattr__(name):
    # HTTP clients can import contracts without installing/loading jieba.
    if name == 'SQLiteTextIndex':
        from .index import SQLiteTextIndex
        return SQLiteTextIndex
    if name == 'Tokenizer':
        from .tokenizer import Tokenizer
        return Tokenizer
    raise AttributeError(name)
