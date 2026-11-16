"""统一解析 LLM 配置，供 CrewAI / LangChain 使用。"""

from __future__ import annotations

from penr.config import get_settings


def resolve_crew_llm() -> str:
    """返回 CrewAI 可识别的 model 字符串。"""
    s = get_settings()
    provider = (s.llm_provider or "").strip().lower()
    model = (s.llm_model or "").strip()

    if not provider:
        if s.anthropic_api_key:
            provider = "anthropic"
        elif s.openai_api_key:
            provider = "openai"
        else:
            provider = "openai"

    if not model:
        model = "claude-sonnet-4-20250514" if provider == "anthropic" else "gpt-4o"

    if provider == "anthropic":
        return f"anthropic/{model}"
    if s.llm_base_url:
        # OpenAI 兼容网关
        return model
    return f"openai/{model}"


def has_llm_credentials() -> bool:
    s = get_settings()
    return bool(s.anthropic_api_key or s.openai_api_key or s.llm_base_url)
