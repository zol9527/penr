"""不依赖 PostgreSQL 的单元测试。"""

from penr.report.build import build_markdown_report
from penr.knowledge.service import chunk_text
from penr import __version__


def test_version():
    assert __version__


def test_chunk_text():
    chunks = chunk_text("a" * 2000, size=500, overlap=50)
    assert len(chunks) >= 4


def test_markdown_report():
    md = build_markdown_report(
        title="t",
        goal="g",
        findings=[{"summary": "xss", "severity": "high", "vulnclass": "xss"}],
        overview={"facts": 1, "findings": 1, "open_intents": []},
    )
    assert "xss" in md
    assert "任务目标" in md
