"""Deterministic Chinese words plus intact identifiers, filenames and path parts."""
from functools import lru_cache
import re
import unicodedata

import jieba

_HAN = re.compile(r'([\u3400-\u9fff\U00020000-\U000323af]+)')
_WORD = re.compile(r'[^\W_]+(?:[._/\\-][\w]+)*', re.UNICODE)
_PART = re.compile(r'[^\W_]+', re.UNICODE)
_STOP = frozenset(('的', '了', '吗', '呢', '是', '和', '在', '我', '你', 'an', 'the'))


@lru_cache(maxsize=1)
def _dictionary():
    # One immutable default dictionary per process, not one allocation per repository.
    tokenizer = jieba.Tokenizer()
    tokenizer.initialize()
    return tokenizer


class Tokenizer:
    version = 'jieba-0.42.1-nfkc-identifiers-v1'

    def tokenize(self, text: str, *, for_index: bool = False) -> list[str]:
        text = unicodedata.normalize('NFKC', text).casefold()
        result = []
        # Separate Chinese prose first: “修改API-client.py文件” must not become
        # one identifier. Mixed Chinese filenames remain searchable by their words.
        for position, part in enumerate(_HAN.split(text)):
            if position % 2:
                cutter = _dictionary().cut_for_search if for_index else _dictionary().cut
                result.extend(cutter(part, HMM=False))
                continue
            for match in _WORD.finditer(part):
                word = match.group()
                result.append(word)
                if for_index and any(char in word for char in '._/\\-'):
                    # Include filenames within paths as well as identifier components.
                    components = _PART.findall(word)
                    spellings = [part for part in re.split(r'[/\\]', word)
                                 if part != word and part not in components]
                    result.extend(spellings)
                    result.extend(components)
        # Keep repeated terms: BM25 needs term frequency, not only a set.
        return [term for term in result if term not in _STOP and term]
