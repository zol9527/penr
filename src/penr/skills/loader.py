"""Skills 目录读写：列表、解析 frontmatter、注入 prompt、统计。"""

from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

import yaml

from penr.config import get_settings
from penr.db.engine import Database

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def ensure_skill_root() -> Path:
    root = get_settings().resolve_skill_dir()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_join(root: Path, *parts: str) -> Path:
    """规范化相对路径，禁止跳出 skill 根目录。"""
    candidate = (root.joinpath(*parts)).resolve()
    root_r = root.resolve()
    if not str(candidate).startswith(str(root_r)):
        raise ValueError("path escapes skill root")
    return candidate


def parse_skill_md(text: str) -> tuple[dict[str, Any], str]:
    m = _FRONTMATTER_RE.match(text.strip() + ("\n" if not text.endswith("\n") else ""))
    if not m:
        # 宽松：允许无 frontmatter
        return {}, text
    meta_raw, body = m.group(1), m.group(2)
    try:
        meta = yaml.safe_load(meta_raw) or {}
        if not isinstance(meta, dict):
            meta = {}
    except yaml.YAMLError:
        meta = {}
    return meta, body


def list_skill_summaries(*, include_body: bool = False) -> list[dict[str, Any]]:
    root = ensure_skill_root()
    items: list[dict[str, Any]] = []
    if not root.is_dir():
        return items
    for p in sorted(root.iterdir()):
        skill_md = p / "SKILL.md"
        if not (p.is_dir() and skill_md.is_file()):
            continue
        text = skill_md.read_text(encoding="utf-8", errors="replace")
        meta, body = parse_skill_md(text)
        files = _list_rel_files(p)
        item: dict[str, Any] = {
            "name": str(meta.get("name") or p.name),
            "description": str(meta.get("description") or ""),
            "license": str(meta.get("license") or ""),
            "compatibility": str(meta.get("compatibility") or ""),
            "mcps": list(meta.get("mcps") or []) if isinstance(meta.get("mcps"), list) else [],
            "files": files,
            "path": str(skill_md),
        }
        if include_body:
            item["body"] = body
            item["content"] = text
        items.append(item)
    return items


def _list_rel_files(skill_dir: Path) -> list[str]:
    out: list[str] = []
    for f in sorted(skill_dir.rglob("*")):
        rel = f.relative_to(skill_dir).as_posix()
        if f.is_dir():
            out.append(rel.rstrip("/") + "/")
        else:
            out.append(rel)
    return out


def load_skill_markdown(name: str) -> str:
    root = ensure_skill_root()
    path = _safe_join(root, name, "SKILL.md")
    if not path.is_file():
        raise FileNotFoundError(f"skill not found: {name}")
    text = path.read_text(encoding="utf-8", errors="replace")
    # 替换占位符
    skill_dir = str(path.parent)
    return text.replace("${SKILL_DIR}", skill_dir)


def skill_prompt_block(*, max_chars: int = 6000, max_skills: int = 12) -> str:
    """生成注入 planner/worker 的 skills 摘要块（先 markdown 注入）。"""
    skills = list_skill_summaries()
    if not skills:
        return ""
    lines = ["## 可用 Skills（按需遵循）", ""]
    used = 0
    for s in skills[:max_skills]:
        chunk = f"- **{s['name']}**：{s.get('description') or '(无描述)'}\n"
        if used + len(chunk) > max_chars:
            break
        lines.append(chunk.rstrip())
        used += len(chunk)
    # 附加前几个 skill 的正文截断，便于模型直接遵循
    remaining = max_chars - used
    for s in skills[:3]:
        if remaining < 200:
            break
        try:
            md = load_skill_markdown(s["name"])
        except OSError:
            continue
        _, body = parse_skill_md(md)
        snippet = body.strip()[: min(1200, remaining - 50)]
        block = f"\n### Skill: {s['name']}\n{snippet}\n"
        lines.append(block)
        remaining -= len(block)
    return "\n".join(lines).strip()


def inject_skills_block(prompt: str, *, agent_key: str = "", db: Database | None = None) -> str:
    block = skill_prompt_block()
    if not block:
        return prompt
    if db is not None and agent_key:
        # 异步统计由调用方 await；此处仅拼装
        pass
    return f"{prompt.rstrip()}\n\n{block}\n"


async def record_skill_stat(db: Database, skill_name: str, agent_key: str) -> None:
    await db.execute(
        """
        INSERT INTO skill_stats (skill_name, agent_key, calls, last_at)
        VALUES ($1, $2, 1, now())
        ON CONFLICT (skill_name, agent_key) DO UPDATE
        SET calls = skill_stats.calls + 1, last_at = now()
        """,
        skill_name,
        agent_key or "",
    )


def write_skill_md(
    name: str,
    *,
    description: str = "",
    license: str = "",
    compatibility: str = "",
    mcps: list[str] | None = None,
    instructions: str = "",
    overwrite: bool = False,
) -> Path:
    root = ensure_skill_root()
    skill_dir = _safe_join(root, name)
    if skill_dir.exists() and not overwrite:
        raise FileExistsError(f"skill already exists: {name}")
    skill_dir.mkdir(parents=True, exist_ok=True)
    meta: dict[str, Any] = {"name": name, "description": description}
    if license:
        meta["license"] = license
    if compatibility:
        meta["compatibility"] = compatibility
    if mcps:
        meta["mcps"] = mcps
    fm = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    body = (instructions or f"# {name}\n\n").strip() + "\n"
    text = f"---\n{fm}\n---\n\n{body}"
    path = skill_dir / "SKILL.md"
    path.write_text(text, encoding="utf-8")
    return path


def update_skill_meta(name: str, patch: dict[str, Any]) -> None:
    root = ensure_skill_root()
    path = _safe_join(root, name, "SKILL.md")
    if not path.is_file():
        raise FileNotFoundError(name)
    text = path.read_text(encoding="utf-8", errors="replace")
    meta, body = parse_skill_md(text)
    for key in ("description", "license", "compatibility", "mcps"):
        if key in patch and patch[key] is not None:
            meta[key] = patch[key]
    meta.setdefault("name", name)
    fm = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    path.write_text(f"---\n{fm}\n---\n\n{body.lstrip()}", encoding="utf-8")


def delete_skill(name: str) -> None:
    root = ensure_skill_root()
    skill_dir = _safe_join(root, name)
    if not skill_dir.is_dir():
        raise FileNotFoundError(name)
    shutil.rmtree(skill_dir)


def install_skill_zip(zip_path: Path, *, overwrite: bool = False) -> tuple[str, int]:
    """从 zip 安装 skill；要求根下有 SKILL.md 或单层目录含 SKILL.md。"""
    root = ensure_skill_root()
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
        if not names:
            raise ValueError("empty zip")
        # 找 SKILL.md
        skill_md_entries = [n for n in names if n.rstrip("/").endswith("SKILL.md")]
        if not skill_md_entries:
            raise ValueError("缺少 SKILL.md")
        entry = skill_md_entries[0]
        parts = Path(entry).parts
        if len(parts) == 1:
            # 扁平：SKILL.md 在 zip 根 → 用 frontmatter name 或 zip stem
            raw = zf.read(entry).decode("utf-8", errors="replace")
            meta, _ = parse_skill_md(raw)
            name = str(meta.get("name") or zip_path.stem)
            dest = _safe_join(root, name)
            if dest.exists() and not overwrite:
                raise FileExistsError(f"已存在: {name}")
            if dest.exists():
                shutil.rmtree(dest)
            dest.mkdir(parents=True, exist_ok=True)
            for info in zf.infolist():
                if info.is_dir():
                    continue
                target = dest / Path(info.filename).name
                target.write_bytes(zf.read(info.filename))
            return name, len(list(dest.rglob("*")))
        # 目录形式：<name>/SKILL.md
        name = parts[0]
        dest = _safe_join(root, name)
        if dest.exists() and not overwrite:
            raise FileExistsError(f"已存在: {name}")
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
        count = 0
        for info in zf.infolist():
            if info.is_dir() or not info.filename.startswith(name + "/"):
                continue
            rel = info.filename[len(name) + 1 :]
            if not rel or ".." in Path(rel).parts:
                continue
            target = _safe_join(dest, *Path(rel).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info.filename))
            count += 1
        if not (dest / "SKILL.md").is_file():
            shutil.rmtree(dest, ignore_errors=True)
            raise ValueError("缺少 SKILL.md")
        return name, count
