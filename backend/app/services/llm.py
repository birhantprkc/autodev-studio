"""Unified chat entry point for the pipeline stages.

Dispatches a (provider, model) chat call to the right transport:
  * ``anthropic`` provider  → the native Anthropic Messages API (anthropic_api.chat)
  * everything else (OpenAI-compatible: groq/openai/gemini/xai/custom) → openai_agent.chat

Callers pass their stage's ``settings.<stage>_provider`` + ``settings.<stage>_model``.
Both transports return the same dict: {text, tokens_in, tokens_out, cost, model, error}.
"""

from __future__ import annotations

from . import anthropic_api, claude_agent, openai_agent, providers


def chat(system: str, user: str = "", *, provider: str, model: str, timeout: int = 180,
         json_mode: bool = False, messages: list[dict] | None = None) -> dict:
    if providers.kind(provider) == "claude-cli":
        return claude_agent.chat(system, user, model=model, timeout=timeout,
                                 json_mode=json_mode, messages=messages)
    if providers.kind(provider) == "anthropic":
        return anthropic_api.chat(system, user, model=model, timeout=timeout,
                                  json_mode=json_mode, messages=messages)
    return openai_agent.chat(system, user, provider=provider, model=model, timeout=timeout,
                             json_mode=json_mode, messages=messages)
