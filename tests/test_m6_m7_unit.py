"""M6/M7 聚焦单元测试（不依赖 PostgreSQL / 原 web E2E）。"""

from __future__ import annotations

from pathlib import Path

from penr.enrich.service import _severity_to_score
from penr.report.build import build_markdown_report
from penr.report.jobs import get_report_job
from penr.skills.loader import (
    inject_skills_block,
    list_skill_summaries,
    parse_skill_md,
    skill_prompt_block,
    write_skill_md,
)
from penr.tools.mcp import mcp_tool_crew_name, re_sub_safe


def test_parse_skill_frontmatter():
    meta, body = parse_skill_md(
        "---\nname: demo\ndescription: 测试\nmcps:\n  - foo\n---\n\n# Hello\n"
    )
    assert meta["name"] == "demo"
    assert meta["description"] == "测试"
    assert meta["mcps"] == ["foo"]
    assert "Hello" in body


def test_write_and_list_skill(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ANTIFOX_SKILL_DIR", str(tmp_path))
    from penr.config import get_settings

    get_settings.cache_clear()
    write_skill_md("unit-skill", description="d", instructions="# body\n")
    items = list_skill_summaries()
    assert any(s["name"] == "unit-skill" for s in items)
    block = skill_prompt_block(max_chars=2000)
    assert "unit-skill" in block
    prompt = inject_skills_block("BASE")
    assert prompt.startswith("BASE")
    assert "unit-skill" in prompt
    get_settings.cache_clear()


def test_mcp_tool_naming():
    assert re_sub_safe("Scope Sentry!") == "Scope_Sentry"
    assert mcp_tool_crew_name("ScopeSentry", "list_assets") == "mcp__ScopeSentry__list_assets"


def test_severity_score():
    assert _severity_to_score("critical") >= 9
    assert _severity_to_score("low") < 5


def test_report_job_idle():
    job = get_report_job("999999")
    assert job["status"] == "idle"


def test_markdown_report_still_works():
    md = build_markdown_report(
        title="t",
        goal="g",
        findings=[{"summary": "xss", "severity": "high"}],
        overview={"facts": 0, "findings": 1, "open_intents": []},
    )
    assert "xss" in md


def test_app_imports_m6_m7_routers():
    from penr.api.app import create_app

    app = create_app()
    # FastAPI≥0.162 的 include_router 使用 _IncludedRouter，app.routes 不展开 path；
    # 用 OpenAPI 核对挂载结果。
    paths = sorted(app.openapi().get("paths", {}).keys())
    assert any(p.startswith("/api/conversations") for p in paths)
    assert any(p.startswith("/api/mcp") for p in paths)
    assert any(p.startswith("/api/skills") for p in paths)
    assert any(p.startswith("/api/report") for p in paths)
    assert any("/enrich" in p for p in paths)
    assert any(p.startswith("/api/knowledge") for p in paths)
