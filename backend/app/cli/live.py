"""Watching a pipeline run.

A scope run is minutes of work by five agents across several models. The web UI
polls a log table for it; here we subscribe to ``services.events`` and render
the run as it happens.

Three things shape this module:

**The pipeline is not ours to interrupt.** It runs on its own thread with a
working git clone, a branch, and possibly a paid model call in flight. So Ctrl-C
*detaches the view*, it does not kill the run — the alternative is a half-applied
commit and a wedged working copy. The run is rejoinable, because everything it
does is also in the database.

**Events arrive on the pipeline's thread.** The subscriber does nothing but put
them on a queue; all rendering happens on the main thread, inside ``Live``.
Rendering from the worker thread would interleave with the prompt and tear.

**The interesting part is the shape of the run, not the log.** So the timeline
of stages is the primary object and the log is a tail beneath it — a full log
scroll tells you nothing about whether QA has started.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.padding import Padding
from rich.table import Table
from rich.text import Text

from ..services import events
from . import theme

# Stage order as the orchestrator drives it. `plan` and `dev` can recur across
# revision rounds; the timeline collapses repeats into one row with a counter
# rather than growing without bound.
STAGE_ORDER = ["plan", "dev", "qa", "review", "pr"]
STAGE_LABEL = {
    "plan": "Planner",
    "dev": "Dev",
    "qa": "QA",
    "review": "Review",
    "pr": "Pull request",
    "pm": "PM",
}
STAGE_STYLE = {
    "plan": "planner", "dev": "dev", "qa": "qa", "review": "jury", "pr": "info", "pm": "pm",
}

_SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
_SPINNER_ASCII = ["-", "\\", "|", "/"]

# How much log to keep on screen. Enough to see what an agent is doing, little
# enough that the timeline stays visible above it on an 80x24 terminal.
TAIL_LINES = 8


class _Stage:
    __slots__ = ("key", "runs", "state", "model", "started", "finished", "cost", "error", "tokens")

    def __init__(self, key: str) -> None:
        self.key = key
        self.runs = 0
        self.state = "pending"      # pending | running | done | failed
        self.model = ""
        self.started: float | None = None
        self.finished: float | None = None
        self.cost = 0.0
        self.tokens = 0
        self.error: str | None = None


class RunView:
    """Accumulates events into something renderable."""

    def __init__(self, g: theme.Glyphs, unicode: bool) -> None:
        self.g = g
        self.frames = _SPINNER if unicode else _SPINNER_ASCII
        self.stages: dict[str, _Stage] = {}
        self.order: list[str] = []
        self.run_stage: dict[int, str] = {}   # run_id → stage key
        self.tail: list[tuple[str, str]] = []  # (severity, message)
        self.cost = 0.0
        self.tokens = 0
        self.started = time.monotonic()
        self.tick = 0

    # ── event intake ─────────────────────────────────────────────────────────
    def apply(self, kind: str, payload: dict[str, Any]) -> None:
        if kind == "run.started":
            key = payload["agent"]
            stage = self.stages.get(key)
            if stage is None:
                stage = self.stages[key] = _Stage(key)
                self.order.append(key)
            stage.runs += 1
            stage.state = "running"
            stage.started = time.monotonic()
            stage.finished = None
            self.run_stage[payload["run_id"]] = key

        elif kind == "run.model":
            stage = self._stage_for(payload.get("run_id"))
            if stage is not None:
                stage.model = payload.get("model") or ""

        elif kind == "run.log":
            message = (payload.get("message") or "").strip()
            if message:
                # Multi-line agent output (a full QA verdict, a review) would
                # blow the tail out in one event; keep the first lines so the
                # tail stays a tail.
                for line in message.splitlines()[:4]:
                    if line.strip():
                        self.tail.append((payload.get("severity") or "info", line.strip()))
                del self.tail[:-TAIL_LINES]

        elif kind == "run.finished":
            stage = self._stage_for(payload.get("run_id"))
            if stage is not None:
                stage.state = "failed" if payload.get("error") else "done"
                stage.finished = time.monotonic()
                stage.error = payload.get("error")
                if not payload.get("usage_unknown"):
                    stage.cost += payload.get("cost") or 0.0
                    self.cost += payload.get("cost") or 0.0
                stage.tokens += (payload.get("tokens_in") or 0) + (payload.get("tokens_out") or 0)
                self.tokens += (payload.get("tokens_in") or 0) + (payload.get("tokens_out") or 0)

    def _stage_for(self, run_id: int | None) -> _Stage | None:
        key = self.run_stage.get(run_id) if run_id is not None else None
        return self.stages.get(key) if key else None

    # ── rendering ────────────────────────────────────────────────────────────
    def _mark(self, stage: _Stage) -> Text:
        if stage.state == "running":
            return Text(self.frames[self.tick % len(self.frames)], style="running")
        if stage.state == "done":
            return Text(self.g.ok, style="ok")
        if stage.state == "failed":
            return Text(self.g.fail, style="err")
        return Text(self.g.pending, style="pending")

    def _elapsed(self, stage: _Stage) -> str:
        if stage.started is None:
            return ""
        end = stage.finished if stage.finished is not None else time.monotonic()
        seconds = end - stage.started
        return f"{seconds:5.1f}s"

    def renderable(self) -> RenderableType:
        self.tick += 1

        timeline = Table(box=None, pad_edge=False, show_header=False, padding=(0, 2, 0, 0))
        timeline.add_column(width=1)
        timeline.add_column(style="heading", no_wrap=True, width=12)
        timeline.add_column(overflow="ellipsis")        # model
        timeline.add_column(justify="right", style="muted", no_wrap=True)   # elapsed

        # Show every stage the pipeline knows about, so the ones still to come
        # read as "not started" rather than being invisible.
        keys = [k for k in STAGE_ORDER if k in self.stages]
        keys += [k for k in self.order if k not in STAGE_ORDER]
        for k in STAGE_ORDER:
            if k not in self.stages:
                keys.append(k)
        seen: set[str] = set()
        for key in keys:
            if key in seen:
                continue
            seen.add(key)
            stage = self.stages.get(key) or _Stage(key)
            label = Text(STAGE_LABEL.get(key, key.title()),
                         style=STAGE_STYLE.get(key, "muted") if stage.state != "pending" else "muted")
            model = Text(stage.model or "", style="muted")
            if stage.runs > 1:
                model += Text(f"  (round {stage.runs})", style="brand.dim")
            if stage.error:
                model = Text(stage.error[:60], style="err")
            timeline.add_row(self._mark(stage), label, model, self._elapsed(stage))

        body: list[RenderableType] = [timeline]

        if self.tail:
            body.append(Text(""))
            for severity, line in self.tail[-TAIL_LINES:]:
                style = {"error": "err", "warn": "warn"}.get(severity, "muted")
                body.append(Text(f"  {self.g.vbar} ", style="rule") +
                            Text(line[:140], style=style))

        total = time.monotonic() - self.started
        footer = Text(f"{total:.0f}s elapsed", style="muted")
        if self.tokens:
            footer += Text(f"   {self.g.bullet}   {self.tokens:,} tokens", style="muted")
        if self.cost:
            footer += Text(f"   {self.g.bullet}   ${self.cost:.4f}", style="muted")
        footer += Text(f"   {self.g.bullet}   Ctrl-C detaches (the run keeps going)", style="rule")
        body.append(Text(""))
        body.append(footer)

        return Padding(Group(*body), (1, 0, 1, 2))


def watch(console: Console, g: theme.Glyphs, unicode: bool, work: threading.Thread,
          *, refresh_hz: int = 12) -> tuple[RunView, bool]:
    """Render ``work`` until it finishes or the user detaches.

    Returns the accumulated view and whether the run actually completed —
    a detached run is still going, and the caller must not report it as done.
    """
    inbox: queue.Queue[tuple[str, dict]] = queue.Queue()
    view = RunView(g, unicode)

    # Runs on the pipeline thread: enqueue only, never render.
    def _on_event(kind: str, payload: dict) -> None:
        inbox.put((kind, payload))

    detached = False
    with events.listener(_on_event):
        work.start()
        try:
            with Live(view.renderable(), console=console, refresh_per_second=refresh_hz,
                      transient=False) as live:
                while work.is_alive() or not inbox.empty():
                    try:
                        kind, payload = inbox.get(timeout=1 / refresh_hz)
                        view.apply(kind, payload)
                    except queue.Empty:
                        pass
                    live.update(view.renderable())
                live.update(view.renderable())
        except KeyboardInterrupt:
            detached = True

    return view, not detached
