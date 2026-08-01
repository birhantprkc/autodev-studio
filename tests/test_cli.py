"""The terminal client: dispatch, selection state, and the rendering contract.

These cover the seams where the CLI could quietly diverge from the rest of the
system — command lookup, which repo/scope a command acts on, and the style
resolution that Textual gets wrong silently rather than loudly.
"""

from __future__ import annotations

import pytest
from app.cli import commands, theme
from app.cli.context import Context
from app.cli.panels.settings import _visible
from app.core import CoreError
from app.models import Repo, ScopeSession
from sqlmodel import Session


@pytest.fixture()
def repos(db: Session) -> tuple[Repo, Repo]:
    first = Repo(name="rich", org="Textualize", git_url="https://github.com/Textualize/rich",
                 key_prefix="R", default_branch="main")
    second = Repo(name="click", org="pallets", git_url="https://github.com/pallets/click",
                  key_prefix="C", default_branch="main")
    db.add(first)
    db.add(second)
    db.commit()
    db.refresh(first)
    db.refresh(second)
    return first, second


# ── command dispatch ─────────────────────────────────────────────────────────
class TestRegistry:
    def test_every_command_is_reachable_by_its_own_name(self):
        for name in commands.ORDER:
            assert commands.resolve(name) is not None, name

    def test_resolves_without_the_leading_slash(self):
        """Subcommands dispatch through the same registry as slash commands."""
        assert commands.resolve("board") is commands.resolve("/board")

    def test_resolves_an_unambiguous_prefix(self):
        assert commands.resolve("/appr").name == "/approve"

    def test_refuses_an_ambiguous_prefix(self):
        # /model and /models both exist; guessing between them would silently
        # repoint a pipeline stage when the user meant to look at one.
        assert commands.resolve("/mode") is None

    def test_aliases_reach_the_same_command(self):
        assert commands.resolve("/q") is commands.resolve("/quit")
        assert commands.resolve("/b") is commands.resolve("/board")

    def test_unknown_command_resolves_to_nothing(self):
        assert commands.resolve("/definitely-not-a-command") is None

    def test_help_text_exists_for_every_command(self):
        """The registry is what /help and tab-completion render from."""
        for name in commands.ORDER:
            assert commands.REGISTRY[name].help.strip(), f"{name} has no help"


# ── selection state ──────────────────────────────────────────────────────────
class TestContext:
    def test_switching_repo_clears_the_scope(self, db: Session, repos):
        """A scope belongs to one repo; carrying it across would have the PM
        agent answering about the wrong codebase."""
        first, second = repos
        scope = ScopeSession(repo_id=first.id, kind="pm", title="add a flag")
        db.add(scope)
        db.commit()
        db.refresh(scope)

        ctx = Context()
        ctx.select_scope(scope)
        assert ctx.repo_id == first.id and ctx.session_id == scope.id

        ctx.select_repo(second)
        assert ctx.repo_id == second.id
        assert ctx.session_id is None

    def test_reselecting_the_same_repo_keeps_the_scope(self, db: Session, repos):
        first, _ = repos
        scope = ScopeSession(repo_id=first.id, kind="pm", title="add a flag")
        db.add(scope)
        db.commit()
        db.refresh(scope)

        ctx = Context()
        ctx.select_scope(scope)
        ctx.select_repo(first)
        assert ctx.session_id == scope.id

    def test_ensure_repo_auto_selects_when_only_one_exists(self, db: Session):
        repo = Repo(name="rich", org="Textualize", git_url="https://x/rich", key_prefix="R")
        db.add(repo)
        db.commit()

        ctx = Context()
        assert ctx.ensure_repo(db).name == "rich"

    def test_ensure_repo_asks_rather_than_guessing_between_several(self, db: Session, repos):
        ctx = Context()
        with pytest.raises(CoreError) as excinfo:
            ctx.ensure_repo(db)
        assert excinfo.value.status == 409
        # The message has to name the alternatives, or it isn't actionable.
        assert "rich" in str(excinfo.value)

    def test_ensure_repo_explains_the_empty_case_differently(self, db: Session):
        """'Nothing indexed yet' and 'several, pick one' need different advice."""
        ctx = Context()
        with pytest.raises(CoreError) as excinfo:
            ctx.ensure_repo(db)
        assert "/kb add" in str(excinfo.value)


# ── rendering contract ───────────────────────────────────────────────────────
class TestTheme:
    def test_palette_names_resolve_to_literal_styles(self):
        """Textual renders widgets with its own machinery and never consults a
        Rich theme, so a bare palette name there is not an error — it just comes
        out uncoloured. Everything shared between the two must resolve first."""
        assert theme.s("ok") == theme.STYLES["ok"]
        assert theme.s("sev.critical").startswith("bold ")
        assert theme.s("ok") != "ok"

    def test_unknown_style_passes_through_untouched(self):
        # "bold" and "" are real Rich styles that aren't in our palette.
        assert theme.s("bold") == "bold"
        assert theme.s("") == ""

    def test_every_palette_entry_is_a_parseable_rich_style(self):
        from rich.style import Style

        for _name, value in theme.STYLES.items():
            Style.parse(value)      # raises if the palette holds a typo

    def test_glyphs_have_ascii_fallbacks(self):
        """--ascii has to change every glyph, not most of them."""
        unicode_glyphs = theme.Glyphs(True)
        ascii_glyphs = theme.Glyphs(False)
        for slot in theme.Glyphs.__slots__ if hasattr(theme.Glyphs, "__slots__") else vars(ascii_glyphs):
            value = getattr(ascii_glyphs, slot)
            assert value.isascii(), f"{slot} is not ASCII in fallback mode: {value!r}"
            assert value != getattr(unicode_glyphs, slot) or value.isascii()

    def test_wordmark_degrades_on_a_narrow_terminal(self):
        wide = theme.wordmark(100, unicode=True)
        narrow = theme.wordmark(40, unicode=True)
        assert len(wide) > 1                       # the block wordmark
        assert len(narrow) == 1                    # the lockup
        assert all(len(line) <= 72 for line in wide)


# ── settings form spec ───────────────────────────────────────────────────────
class TestShowIf:
    def test_field_with_no_condition_is_always_visible(self):
        assert _visible({"show_if": ""}, {})

    def test_condition_matches_the_driver_value(self):
        field = {"show_if": "rag_embeddings=api"}
        assert _visible(field, {"rag_embeddings": "api"})
        assert not _visible(field, {"rag_embeddings": "local"})

    def test_condition_accepts_alternatives(self):
        field = {"show_if": "engine=api|local"}
        assert _visible(field, {"engine": "local"})
        assert not _visible(field, {"engine": "tfidf"})

    def test_booleans_are_compared_as_words(self):
        """The spec writes bools as true/false; the values are real bools."""
        field = {"show_if": "jury_enabled=true"}
        assert _visible(field, {"jury_enabled": True})
        assert not _visible(field, {"jury_enabled": False})


# ── the settings panel's edit detection ──────────────────────────────────────
class TestBlankSentinel:
    """`Select.BLANK` is not part of Select's API in the installed Textual.

    The name still resolves — up the MRO to ``Widget.BLANK``, an unrelated flag
    whose value is ``False`` — so using it as "no selection" constructs a Select
    with a literal ``False`` and blows up in ``_validate_value`` the first time
    a field has no configured value. It fails at mount, not at import, so only
    an assertion like this catches it before a user does.
    """

    def test_blank_is_not_the_no_selection_sentinel(self):
        from textual.widgets import Select

        assert Select.BLANK is not Select.NULL
        assert Select.BLANK is False        # noqa: E712 — that is the whole point

    def test_no_panel_uses_blank_as_a_sentinel(self):
        """Comments may name it (they explain the trap); code may not use it."""
        import pathlib

        panels = pathlib.Path(__file__).resolve().parents[1] / "backend/app/cli/panels"
        offenders = []
        for path in panels.glob("*.py"):
            for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                code = line.split("#", 1)[0]
                if "Select.BLANK" in code:
                    offenders.append(f"{path.name}:{lineno}")
        assert not offenders, f"Select.BLANK used as a sentinel at: {offenders}"


class TestRecordIgnoresConstructionEchoes:
    """Mounting a widget with a value makes it post its own Changed message.

    Textual delivers that on a later pump tick, so a "are we still mounting?"
    flag can't catch it. If those echoes count as edits, two things break: every
    field looks edited (Ctrl-S then writes back all 59 settings nobody touched),
    and each provider field's echo re-renders the group, which rebuilds all six
    provider fields, which echo again — an unbounded loop that pegs a core.
    """

    @staticmethod
    def _panel(values: dict):
        from app.cli.panels.settings import SettingsPanel

        panel = SettingsPanel.__new__(SettingsPanel)   # no App bootstrap needed
        panel.values = dict(values)
        panel.pending = {}
        panel.group_index = 0
        panel._rerender_calls = 0
        panel._rerender = lambda: setattr(          # type: ignore[method-assign]
            panel, "_rerender_calls", panel._rerender_calls + 1)
        return panel

    def test_echo_of_the_constructed_value_is_not_an_edit(self):
        panel = self._panel({"dev_provider": "claude-cli"})
        panel._record("f-dev_provider", "claude-cli")
        assert panel.pending == {}
        assert panel._rerender_calls == 0, "an echo triggered a re-render — this is the loop"

    def test_numeric_echo_is_not_an_edit(self):
        """Input reports a str; the setting is stored as a float. Comparing them
        raw reads every numeric field as edited the instant its group is drawn."""
        panel = self._panel({"claude_max_budget_usd": 2.0})
        panel._record("f-claude_max_budget_usd", "2.0")
        assert panel.pending == {}

    def test_unset_field_echoing_empty_is_not_an_edit(self):
        panel = self._panel({"jury_synthesis_model": None})
        panel._record("f-jury_synthesis_model", "")
        assert panel.pending == {}

    def test_a_real_change_is_still_recorded(self):
        panel = self._panel({"dev_provider": "claude-cli"})
        panel._record("f-dev_provider", "anthropic")
        assert panel.pending == {"dev_provider": "anthropic"}
        assert panel._rerender_calls == 1, "a provider change must refresh its model list"

    def test_repeating_a_real_change_does_not_re_render(self):
        panel = self._panel({"dev_provider": "claude-cli"})
        panel._record("f-dev_provider", "anthropic")
        panel._record("f-dev_provider", "anthropic")
        assert panel._rerender_calls == 1


class TestModelCommandValidation:
    """`/model` joins everything after the provider into the model id, because
    real ids contain slashes ('openai/gpt-oss-120b'). That made a trailing typo
    invisible: `/model planner claude-cli sonnet /models` stored the model as
    "sonnet /models" and the only symptom was the Planner failing mid-run."""

    def test_a_trailing_slash_command_is_rejected(self, db: Session, repos):
        from app.cli.repl import Shell

        shell = Shell(Context(), console=theme.console())
        with pytest.raises(CoreError) as exc:
            commands.resolve("/model").run(shell, "planner claude-cli sonnet /models")
        assert "doesn't look like a model id" in str(exc.value)
        assert "/model planner claude-cli sonnet" in str(exc.value)

    def test_a_slashed_model_id_still_works(self, db: Session, repos):
        """Provider-prefixed ids are the normal case and must not be blocked."""
        from app.cli.repl import Shell

        shell = Shell(Context(), console=theme.console())
        commands.resolve("/model").run(shell, "qa groq openai/gpt-oss-120b")
        from app.config import settings as cfg
        assert cfg.qa_model == "openai/gpt-oss-120b"
