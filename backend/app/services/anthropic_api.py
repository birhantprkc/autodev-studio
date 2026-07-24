"""Anthropic native Messages API adapter for the pure-chat pipeline stages
(knowledge, pm, qa, review) when their provider is set to ``anthropic``.

Mirrors ``openai_agent.chat``'s call shape and return dict so ``llm.chat`` can
dispatch to either transport transparently. Anthropic *coding* (dev/review) runs
through the Claude Code CLI instead — see services/claude_agent.py.
"""

from __future__ import annotations

from ..config import settings

# List prices ($ per 1M tokens: input, output) for the cost meter. Unknown models
# fall back to Sonnet-tier pricing.
_PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_DEFAULT_PRICE = (3.0, 15.0)

_JSON_NUDGE = (
    "\n\nRespond with a single valid JSON object and NOTHING else — no prose, no "
    "markdown fences."
)


def chat(system: str, user: str = "", *, model: str | None = None, timeout: int = 180,
         json_mode: bool = False, messages: list[dict] | None = None) -> dict:
    """One Anthropic Messages API completion. Returns
    {text, tokens_in, tokens_out, cost, model, error} — same shape as openai_agent.chat.
    `messages` (multi-turn) overrides `user`. The Anthropic SDK is imported lazily so
    the module loads even if the SDK isn't installed and no Anthropic stage is used."""
    m = model or "claude-sonnet-5"
    if not settings.anthropic_api_key:
        return {"text": "", "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "model": m,
                "error": "ANTHROPIC_API_KEY not configured — skipping " + m}
    try:
        import anthropic  # lazy: only needed when an Anthropic stage runs
    except ImportError:
        return {"text": "", "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "model": m,
                "error": "anthropic SDK not installed (pip install anthropic)"}

    sys_prompt = system + (_JSON_NUDGE if json_mode else "")
    msgs = messages if messages is not None else [{"role": "user", "content": user}]
    try:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=float(timeout))
        resp = client.messages.create(
            model=m, max_tokens=8000, system=sys_prompt, messages=msgs,
        )
    except anthropic.APIStatusError as exc:  # 4xx/5xx with a response
        return {"text": "", "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "model": m,
                "error": f"Anthropic {exc.status_code}: {str(exc)[:200]}"}
    except Exception as exc:  # noqa: BLE001 — connection/timeout/etc.
        return {"text": "", "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "model": m,
                "error": str(exc)}

    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    usage = resp.usage
    tin = (getattr(usage, "input_tokens", 0) or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0)
    tout = getattr(usage, "output_tokens", 0) or 0
    pin, pout = _PRICES.get(m, _DEFAULT_PRICE)
    cost = round(tin / 1_000_000 * pin + tout / 1_000_000 * pout, 4)
    return {"text": text, "tokens_in": tin, "tokens_out": tout, "cost": cost,
            "model": m, "error": None}
