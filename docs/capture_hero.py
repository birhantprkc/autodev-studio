#!/usr/bin/env python3
"""Render the README's hero image from the CLI's own renderers.

Not a mockup: this drives ``cli.render`` and ``cli.live.RunView`` — the exact
code paths a real session goes through — and exports what Rich draws to SVG. So
the picture cannot drift from the product; if a renderer changes, re-run this and
the hero changes with it.

    python docs/capture_hero.py

Writes docs/screenshots/cli-hero.svg.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.cli import live, render, theme  # noqa: E402
from app.models import Repo, ScopeSession  # noqa: E402
from rich.console import Group  # noqa: E402
from rich.padding import Padding  # noqa: E402
from rich.terminal_theme import TerminalTheme  # noqa: E402
from rich.text import Text  # noqa: E402

WIDTH = 112
OUT = ROOT / "docs" / "screenshots" / "cli-hero.svg"


def _hex(colour: str) -> tuple[int, int, int]:
    c = colour.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


# The CLI itself never sets a background — a terminal already has one, and half
# of them are light (see cli/theme.py). An SVG has no terminal to inherit from,
# so the export supplies one here, built from the same palette constants so the
# picture and the product cannot drift apart.
BACKGROUND = "#16161C"
FOREGROUND = "#E4E4E9"
SVG_THEME = TerminalTheme(
    _hex(BACKGROUND), _hex(FOREGROUND),
    [_hex(c) for c in (BACKGROUND, theme.RED, theme.GREEN, theme.AMBER,
                       theme.BLUE, theme.VIOLET, theme.OAK, FOREGROUND)],
    [_hex(c) for c in (theme.INK, theme.RED, theme.GREEN, theme.GOLD,
                       theme.BLUE, theme.VIOLET, theme.OAK, "#FFFFFF")],
)

# A representative session against a real open-source repo. The numbers are the
# ones from benchmarks/kb-vs-claude-code.md (rich, task C — the cross-cutting
# bug), so the picture shows a run that actually happened.
REPO = Repo(name="rich", org="Textualize", git_url="https://github.com/Textualize/rich",
            key_prefix="R", default_branch="master")
SCOPE = ScopeSession(title="Fix cell-width measurement for combining marks")

STAGES = {
    "knowledge_provider": "claude-cli", "knowledge_model": "haiku",
    "pm_provider": "claude-cli", "pm_model": "haiku",
    "planner_provider": "gemini", "planner_model": "gemini-3.5-flash-lite",
    "dev_provider": "claude-cli", "dev_model": "sonnet",
    "qa_provider": "groq", "qa_model": "openai/gpt-oss-120b",
    "review_provider": "groq", "review_model": "llama-3.3-70b-versatile",
}

# (stage key, model, seconds, tokens_in, tokens_out, cost) — keys are live.STAGE_ORDER's
TIMELINE = [
    ("plan", "gemini-3.5-flash-lite", 11.4, 16_029, 1_097, 0.0510),
    ("dev", "claude-cli sonnet", 96.2, 261_949, 3_498, 0.1890),
    ("qa", "groq openai/gpt-oss-120b", 22.7, 1_551, 357, 0.0074),
]
REVIEW_RUNNING_FOR = 8.3

# Kept inside WIDTH: the tail renders one line per entry with a gutter, so a
# line that wraps loses its gutter and the block stops reading as a log.
LOG_TAIL = [
    ("info", "graph: 4 call sites of cell_len() verified — cells.py:38, text.py:441"),
    ("info", "planner: 3 steps, 4 files pinned, blast radius 6 symbols"),
    ("info", "dev: edited rich/cells.py  +18 −0   (syntax gate: ast OK)"),
    ("info", "qa: 214 passed, 0 failed  (baseline: 214 passed) — PASS"),
    ("info", "jury: 4 judges seated, running in parallel across 3 providers"),
]


def _finished_stage(view: live.RunView, run_id: int, key: str, model: str,
                    seconds: float, tin: int, tout: int, cost: float, *, at: float) -> None:
    """Push a stage through the real event intake, then backdate its clock.

    ``RunView`` measures elapsed time off ``time.monotonic()``, which a static
    capture has no way to drive through the event API — so the events are real
    and only the two timestamps are placed on the monotonic clock afterwards.
    """
    view.apply("run.started", {"agent": key, "run_id": run_id})
    view.apply("run.model", {"run_id": run_id, "model": model})
    view.apply("run.finished", {"run_id": run_id, "tokens_in": tin,
                                "tokens_out": tout, "cost": cost})
    stage = view.stages[key]
    stage.started = at
    stage.finished = at + seconds


def build(g: theme.Glyphs) -> Group:
    blocks: list = [
        render.banner(WIDTH, g, unicode=True),
        render.status_line(repo=REPO, scope=SCOPE, stages=STAGES, demo=True,
                           cost=0.2474, g=g),
        Padding(Text("  • describe a change in plain English, or /help for commands",
                     style=theme.s("muted")), (1, 0, 0, 2)),
    ]

    # The conversation: a request, the PM agent's clarifying turn, then /run.
    blocks.append(render.user_echo(
        "wide emoji and combining accents make table borders misalign", g))
    blocks.append(render.agent_message(
        "PM", theme.s("pm"),
        "Two questions before I lock this. (1) Should the fix cover every "
        "grapheme cluster, or only the combining-mark case you hit? (2) The "
        "measurement is cached — is a cache-invalidation change in scope, or "
        "should I keep this to the measurement itself?", g))
    blocks.append(render.user_echo("all grapheme clusters, and yes fix the cache", g))
    blocks.append(render.agent_message(
        "PM", theme.s("pm"),
        "Scope locked. Drafted R-118 with 4 acceptance criteria.", g))
    blocks.append(Padding(Text("› /approve all && /run", style=theme.s("you")), (1, 0, 0, 2)))

    # The pipeline, rendered by the same RunView the live view uses.
    view = live.RunView(g, unicode=True)
    total = sum(row[2] for row in TIMELINE) + REVIEW_RUNNING_FOR
    now = time.monotonic()
    view.started = now - total

    at = view.started
    for i, (key, model, seconds, tin, tout, cost) in enumerate(TIMELINE, start=1):
        _finished_stage(view, i, key, model, seconds, tin, tout, cost, at=at)
        at += seconds

    # Review is mid-flight — the jury's judges run in parallel, so this is the
    # frame worth showing.
    view.apply("run.started", {"agent": "review", "run_id": 99})
    view.apply("run.model", {"run_id": 99, "model": "4 judges in parallel"})
    view.stages["review"].started = now - REVIEW_RUNNING_FOR

    for severity, line in LOG_TAIL:
        view.apply("run.log", {"severity": severity, "message": line})
    blocks.append(view.renderable())

    return Group(*blocks)


def _make_self_contained(svg: str) -> str:
    """Drop Rich's CDN webfont ``url()`` sources.

    An image in a README should not phone home, and GitHub blocks the request
    anyway. The fallback costs nothing structurally: Rich pins every text run
    with ``textLength``, so whatever monospace the viewer has lands on the same
    grid and the columns stay aligned. (Inlining a font was tried — it adds
    ~450 KB and changes nothing, because the block wordmark's hairline seams come
    from that same ``textLength`` stretching, not from the font choice. They
    disappear at the size a README actually displays this at.)
    """
    kept = [line for line in svg.splitlines() if "cdnjs.cloudflare.com" not in line]
    # `src: local("…"),` is left with a dangling comma once the urls are gone.
    return "\n".join(line.replace('),', ');') if line.strip().startswith("src: local(")
                     else line for line in kept)


def main() -> int:
    console = theme.console(width=WIDTH, record=True)
    console.print(build(theme.Glyphs(True)))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(_make_self_contained(
        console.export_svg(title="codejury", theme=SVG_THEME)), encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}  ({OUT.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
