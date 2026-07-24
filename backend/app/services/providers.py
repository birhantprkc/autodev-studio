"""Provider registry — the single source of truth for which LLM providers exist,
where they live, which key they use, and which models they offer.

Each pipeline stage (knowledge, pm, dev, qa, review) stores a
``<stage>_provider`` + ``<stage>_model`` in config; routing resolves the endpoint
from the provider id here instead of guessing from the model name. A provider has
one of three "kinds":

  * ``openai``     — an OpenAI-compatible HTTP endpoint (groq, openai, gemini, xai,
                     custom). Served by ``openai_agent.chat``/``code``.
  * ``anthropic``  — Anthropic's native Messages API. Served by ``anthropic_api.chat``.
                     Used for the pure-chat stages (knowledge, pm, qa, review).
  * ``claude-cli`` — the Claude Code CLI (``claude_agent.run_claude``). The strongest
                     coding path; offered for the dev/review stages only.

Anthropic *coding* (dev/review) intentionally runs through the CLI, so those two
stages expose ``claude-cli`` rather than the ``anthropic`` API provider.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import settings

STAGES: tuple[str, ...] = ("knowledge", "pm", "dev", "qa", "review")


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    kind: str                       # "openai" | "anthropic" | "claude-cli"
    key_field: str                  # settings attr holding the API key
    base_url: str = ""              # static base URL (openai-kind, when no field)
    base_url_field: str = ""        # settings attr holding the base URL (overrides base_url)
    models: tuple[str, ...] = ()    # catalog offered in the UI (free text still allowed)
    default_model: str = ""         # used by the "apply to all stages" preset
    note: str = ""                  # short UI hint


# The registry. Order here is the UI order.
PROVIDERS: dict[str, Provider] = {
    "groq": Provider(
        "groq", "Groq (free tier)", "openai", "groq_api_key",
        base_url="https://api.groq.com/openai/v1",
        models=(
            "openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant", "qwen/qwen3-32b", "moonshotai/kimi-k2-instruct",
        ),
        default_model="openai/gpt-oss-120b",
        note="Fast, free-tier OpenAI-compatible endpoint. Tight per-minute token budgets.",
    ),
    "openai": Provider(
        "openai", "OpenAI (or primary endpoint)", "openai", "openai_api_key",
        base_url_field="openai_base_url",
        models=("gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "o4-mini"),
        default_model="gpt-4o",
        note="The legacy 'primary' endpoint — its base URL is configurable (points at "
             "Groq out of the box). Set it to https://api.openai.com/v1 for real OpenAI.",
    ),
    "gemini": Provider(
        "gemini", "Google Gemini", "openai", "gemini_api_key",
        base_url_field="gemini_base_url",
        models=(
            "gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-3.1-flash-lite",
            "gemini-flash-lite-latest",
        ),
        default_model="gemini-3.1-flash-lite",
        note="Google AI Studio via its OpenAI-compatible endpoint. Large per-minute budgets.",
    ),
    "xai": Provider(
        "xai", "xAI (Grok)", "openai", "xai_api_key",
        base_url="https://api.x.ai/v1",
        models=("grok-4", "grok-4-fast", "grok-3", "grok-3-mini"),
        default_model="grok-4-fast",
        note="xAI Grok, OpenAI-compatible. Model ids change over time — free text is allowed.",
    ),
    "anthropic": Provider(
        "anthropic", "Anthropic (API)", "anthropic", "anthropic_api_key",
        models=("claude-opus-4-8", "claude-sonnet-5", "claude-haiku-4-5"),
        default_model="claude-sonnet-5",
        note="Anthropic's native Messages API (used for the chat stages). For coding, pick "
             "'Claude Code (CLI)' on the Dev/Review stages instead.",
    ),
    "claude-cli": Provider(
        "claude-cli", "Claude Code (agentic CLI)", "claude-cli", "anthropic_api_key",
        models=("sonnet", "opus", "haiku"),
        default_model="sonnet",
        note="The Claude Code CLI — real agentic file editing. The strongest coding path; "
             "Dev/Review only. Uses the host's Claude login when no API key is set.",
    ),
    "custom": Provider(
        "custom", "Custom (OpenAI-compatible)", "openai", "custom_api_key",
        base_url_field="custom_base_url",
        models=(),
        default_model="",
        note="Any OpenAI-compatible endpoint (OpenRouter, Together, DeepSeek, Ollama, …). "
             "Set the base URL + key below and type the model id.",
    ),
}

# Which providers each stage may select. Dev/Review get the CLI (+ dev gets 'auto');
# the pure-chat stages get the API providers (no CLI).
_CHAT_PROVIDERS = ["groq", "openai", "gemini", "xai", "anthropic", "custom"]
_CODE_PROVIDERS = ["claude-cli", "groq", "openai", "gemini", "xai", "custom"]


def provider_ids() -> list[str]:
    return list(PROVIDERS.keys())


def stage_provider_ids(stage: str) -> list[str]:
    if stage == "dev":
        return ["auto"] + _CODE_PROVIDERS
    if stage == "review":
        return list(_CODE_PROVIDERS)
    # Chat stages (knowledge/pm/qa) may also run through the Claude CLI (pure-chat
    # invocation via claude_agent.chat) — the only Claude path when no
    # ANTHROPIC_API_KEY is set.
    return list(_CHAT_PROVIDERS) + ["claude-cli"]


def kind(provider_id: str) -> str:
    p = PROVIDERS.get(provider_id)
    return p.kind if p else "openai"


def is_cli(provider_id: str) -> bool:
    # 'auto' and 'anthropic' both resolve to the Claude CLI on the coding stages.
    return provider_id in ("claude-cli", "auto", "anthropic")


def endpoint(provider_id: str) -> tuple[str, str]:
    """(base_url, api_key) for an OpenAI-compatible provider, read live from settings."""
    p = PROVIDERS.get(provider_id)
    if p is None:
        return "", ""
    base = getattr(settings, p.base_url_field, "") if p.base_url_field else p.base_url
    key = getattr(settings, p.key_field, "") if p.key_field else ""
    return base or p.base_url, key


def has_key(provider_id: str) -> bool:
    p = PROVIDERS.get(provider_id)
    if p is None or not p.key_field:
        return False
    return bool(getattr(settings, p.key_field, ""))


def label(provider_id: str, model: str = "") -> str:
    """Human label for run rows, e.g. 'xai grok-4' or 'claude-cli sonnet'."""
    name = provider_id or "?"
    return f"{name} {model}".strip()


def stage_provider(stage: str) -> str:
    return getattr(settings, f"{stage}_provider", "openai")


def stage_model(stage: str) -> str:
    return getattr(settings, f"{stage}_model", "")


# Recommended per-stage model when the operator applies a whole-provider preset.
# For anthropic, the chat stages use the API and dev/review flip to the CLI.
_ANTHROPIC_PRESET = {
    "knowledge": ("anthropic", "claude-haiku-4-5"),
    "pm": ("anthropic", "claude-opus-4-8"),
    "dev": ("claude-cli", "sonnet"),
    "qa": ("anthropic", "claude-sonnet-5"),
    "review": ("claude-cli", "opus"),
}


def preset_values(provider_id: str) -> dict[str, str]:
    """All ``<stage>_provider`` / ``<stage>_model`` values for 'apply this provider to
    every stage'. Raises ValueError for an unknown provider."""
    if provider_id == "anthropic":
        out: dict[str, str] = {}
        for stage, (pid, model) in _ANTHROPIC_PRESET.items():
            out[f"{stage}_provider"] = pid
            out[f"{stage}_model"] = model
        return out
    p = PROVIDERS.get(provider_id)
    if p is None or provider_id == "claude-cli":
        # claude-cli can't drive the chat stages (knowledge/pm/qa), so it isn't a
        # valid whole-system preset; use 'anthropic' for an all-Claude setup.
        raise ValueError(f"'{provider_id}' is not a valid apply-to-all provider")
    out = {}
    for stage in STAGES:
        out[f"{stage}_provider"] = provider_id
        out[f"{stage}_model"] = p.default_model
    return out


def preset_provider_ids() -> list[str]:
    """Providers offerable in the 'apply to all stages' dropdown."""
    return ["anthropic", "groq", "openai", "gemini", "xai", "custom"]
