"""Standalone search tests: no Runtime Server, model provider or Task dependency."""
import sqlite3

import pytest

from artorias_search import Document, IndexUnavailable, Scope, SQLiteTextIndex, Tokenizer, WriteResult


@pytest.fixture
def database(tmp_path):
    connection = sqlite3.connect(tmp_path / 'search.sqlite')
    index = SQLiteTextIndex()
    with connection:
        index.initialize(connection)
        index.rebuild(connection, 'docs', [], projection_version='v1')
    yield connection, index
    connection.close()


def put(database, *documents, partition='one'):
    conn, index = database
    with conn:
        return index.upsert(conn, Scope('docs', partition), documents, projection_version='v1')


def find(database, query, *, partition='one', **kwargs):
    conn, index = database
    return [hit.id for hit in index.search(conn, Scope('docs', partition), query,
                                           projection_version='v1', **kwargs)]


def test_chinese_boundaries_identifiers_and_normalization(database):
    put(database, Document('report', '南京市长江大桥的分析报告'),
        Document('code', 'src/API-client.py TASK_123 README.md'),
        Document('mixed', '中文目录/目录摘要.md'))
    assert find(database, '长江大桥') == ['report']
    assert find(database, '京市长') == []  # Not an overlapping-bigram index.
    for query in ('ＡＰＩ-client.py', 'api', 'client', 'TASK_123', '123', 'src/API-client.py',
                  '请修改API-client.py文件', 'readme.md'):
        assert find(database, query) == ['code']
    assert find(database, '目录摘要.md', mode='all') == ['mixed']
    assert find(database, '的 和 我 the') == []


def test_bm25_weights_scope_any_all_and_fts_syntax(database):
    put(database, Document('title', title='quartz', body='filler'),
        Document('body', title='filler', body='quartz'),
        Document('both', title='topaz quartz'), Document('syntax', title='OR'))
    put(database, Document('foreign', 'quartz'), partition='two')
    assert find(database, 'quartz') == ['both', 'title', 'body']  # Equal title hits tie by ID.
    assert find(database, 'quartz topaz', mode='all') == ['both']
    assert set(find(database, 'quartz topaz', mode='any')) == {'title', 'both', 'body'}
    assert find(database, '" OR *', mode='all') == ['syntax']
    assert find(database, '* :()') == []
    with pytest.raises(ValueError):
        find(database, 'quartz', limit=101)
    with pytest.raises(ValueError):
        find(database, 'quartz', mode='sql')
    with pytest.raises(ValueError):
        find(database, 'x' * 30001)
    conn, index = database
    with conn:
        index.rebuild(conn, 'other', [('one', Document('other', 'quartz'))], projection_version='v1')
    assert 'other' not in find(database, 'quartz')


def test_update_delete_hash_skip_and_transactions(database):
    conn, index = database
    assert put(database, Document('d', 'amber')) == WriteResult(1, 0)
    assert put(database, Document('d', 'amber')) == WriteResult(0, 1)
    assert put(database, Document('d', 'sapphire')) == WriteResult(1, 0)
    assert find(database, 'amber') == []
    assert find(database, 'sapphire') == ['d']
    with pytest.raises(RuntimeError), conn:
        index.upsert(conn, Scope('docs', 'one'), [Document('d', 'ruby')], projection_version='v1')
        raise RuntimeError('business write failed')
    assert find(database, 'sapphire') == ['d']
    assert find(database, 'ruby') == []
    with conn:
        assert index.delete(conn, Scope('docs', 'two'), ['d']) == 0
        assert index.delete(conn, Scope('docs', 'one'), ['d', 'd']) == 1
    assert find(database, 'sapphire') == []


def test_versioned_rebuild_is_atomic_and_scoped(database):
    conn, index = database
    put(database, Document('d', 'amber'))
    with conn:
        index.rebuild(conn, 'other', [('one', Document('keep', 'amber'))], projection_version='v1')
    before = conn.execute('SELECT * FROM text_search_collections').fetchall()

    def broken_source():
        yield 'one', Document('replacement', 'sapphire')
        raise RuntimeError('source failed')

    with pytest.raises(RuntimeError), conn:
        index.rebuild(conn, 'docs', broken_source(), projection_version='v2')
    assert conn.execute('SELECT * FROM text_search_collections').fetchall() == before
    assert find(database, 'amber') == ['d']
    with conn:
        assert index.rebuild(conn, 'docs', [('one', Document('new', 'sapphire'))], projection_version='v2') == 1
    with pytest.raises(IndexUnavailable):
        find(database, 'amber')
    assert [hit.id for hit in index.search(conn, Scope('docs', 'one'), 'sapphire', projection_version='v2')] == ['new']
    assert [hit.id for hit in index.search(conn, Scope('other', 'one'), 'amber', projection_version='v1')] == ['keep']
    newer = Tokenizer()
    newer.version = 'dictionary-v2'
    with pytest.raises(IndexUnavailable):
        SQLiteTextIndex(newer).search(conn, Scope('docs', 'one'), 'sapphire', projection_version='v2')


def test_long_tokens_do_not_alias_and_ties_are_stable(database):
    put(database, Document('b', 'sapphire'), Document('a', 'sapphire'),
        Document('long', 'a' * 5000 + 'b'))
    assert find(database, 'sapphire') == ['a', 'b']
    assert find(database, 'a' * 5000 + 'b') == ['long']
    assert find(database, 'a' * 5000 + 'c') == []


def test_query_term_limit(database):
    put(database, Document('d', 'matchme'))
    assert find(database, ' '.join(['absent'] * 50) + ' matchme') == ['d']  # Distinct terms count.
    assert find(database, ' '.join(f'absent{i}' for i in range(32)) + ' matchme') == []
