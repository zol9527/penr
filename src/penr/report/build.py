"""报告生成：Markdown + 原生 python-docx。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def build_markdown_report(
    *,
    title: str,
    goal: str,
    findings: list[dict[str, Any]],
    overview: dict[str, Any] | None = None,
    language: str = "zh-CN",
) -> str:
    lines = [
        f"# {title}",
        "",
        f"- 生成时间：{datetime.now(timezone.utc).isoformat()}",
        f"- 任务目标：{goal}",
        f"- 发现数量：{len(findings)}",
        "",
        "## 态势摘要",
        "",
        f"- facts: {(overview or {}).get('facts', 0)}",
        f"- findings: {(overview or {}).get('findings', 0)}",
        f"- open_intents: {len((overview or {}).get('open_intents') or [])}",
        "",
        "## 漏洞列表",
        "",
    ]
    if not findings:
        lines.append("（无确认漏洞）")
    for i, f in enumerate(findings, 1):
        lines.extend(
            [
                f"### {i}. {f.get('summary') or 'untitled'}",
                "",
                f"- 类别：{f.get('vulnclass') or '-'}",
                f"- 严重性：{f.get('severity') or '-'}",
                f"- 证据：{f.get('evidence') or '-'}",
                "",
            ]
        )
        repro = f.get("reproduction") or {}
        if isinstance(repro, str):
            lines.append(f"复现：{repro}")
            lines.append("")
        elif repro:
            lines.append("复现过程：")
            lines.append("")
            lines.append("```json")
            import json

            lines.append(json.dumps(repro, ensure_ascii=False, indent=2, default=str))
            lines.append("```")
            lines.append("")
    return "\n".join(lines)


def build_docx_report(
    *,
    title: str,
    goal: str,
    findings: list[dict[str, Any]],
    overview: dict[str, Any] | None = None,
    output_path: Path,
) -> Path:
    from docx import Document

    doc = Document()
    doc.add_heading(title, level=0)
    doc.add_paragraph(f"任务目标：{goal}")
    doc.add_paragraph(f"生成时间：{datetime.now(timezone.utc).isoformat()}")
    doc.add_paragraph(f"发现数量：{len(findings)}")
    doc.add_heading("态势摘要", level=1)
    doc.add_paragraph(
        f"facts={(overview or {}).get('facts', 0)}, "
        f"findings={(overview or {}).get('findings', 0)}"
    )
    doc.add_heading("漏洞列表", level=1)
    if not findings:
        doc.add_paragraph("（无确认漏洞）")
    for i, f in enumerate(findings, 1):
        doc.add_heading(f"{i}. {f.get('summary') or 'untitled'}", level=2)
        table = doc.add_table(rows=3, cols=2)
        table.cell(0, 0).text = "类别"
        table.cell(0, 1).text = str(f.get("vulnclass") or "-")
        table.cell(1, 0).text = "严重性"
        table.cell(1, 1).text = str(f.get("severity") or "-")
        table.cell(2, 0).text = "证据"
        table.cell(2, 1).text = str(f.get("evidence") or "-")
        repro = f.get("reproduction")
        if repro:
            doc.add_paragraph("复现过程：")
            import json

            doc.add_paragraph(
                json.dumps(repro, ensure_ascii=False, indent=2, default=str)
                if not isinstance(repro, str)
                else repro
            )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return output_path
