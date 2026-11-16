from penr.knowledge.promote import _slug
from penr.knowledge.service import chunk_text


def test_slug():
    assert "sql" in _slug("SQL Injection / #1")


def test_chunk():
    assert len(chunk_text("x" * 1000, size=300, overlap=50)) >= 3
