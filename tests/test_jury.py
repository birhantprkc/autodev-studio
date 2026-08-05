"""The review jury: roster management, parallel polling, and synthesis.

The behaviours worth pinning here are the ones that decide whether an unreviewed
change can ship looking clean. A juror that fails must ABSTAIN loudly, a jury
where everyone fails must be INCONCLUSIVE (never APPROVED), the foreperson must
not be able to block with an empty blocking list, and — in the default pair mode
— a change is APPROVED only when every seated juror actually reviewed it and
approved.
"""

from __future__ import annotations

import json

import pytest
from app.config import settings
from app.models import Judge
from app.services import judges as roster
from app.services import providers
from app.services.jury import panel, personas, synthesis
from sqlmodel import Session

# --- Roster -------------------------------------------------------------------

def test_the_default_jury_is_the_pair(db: Session):
    """Two judges, both seated, splitting the review between them — the shipped
    default. A pair with one seat empty is half a review, not a cheap one."""
    assert roster.current_mode() == "pair"
    assert roster.ensure_seeded(db) == 2
    seated = roster.enabled_judges(db)
    assert [j.persona for j in seated] == ["implementation", "systems"]
    # Re-seeding an existing roster is a no-op: an operator's jury is theirs.
    assert roster.ensure_seeded(db) == 0
    assert len(roster.all_judges(db)) == 2


def test_seeds_the_full_panel_when_the_operator_asks_for_it(db: Session, monkeypatch):
    monkeypatch.setattr(settings, "jury_mode", "panel")
    assert roster.ensure_seeded(db) == len(personas.for_mode("panel"))
    enabled = {j.persona for j in roster.enabled_judges(db)}
    assert enabled == {"correctness", "reliability", "security", "architecture"}
    # The cost-heavy specialists ship seated but silent.
    assert {j.persona for j in roster.all_judges(db)} - enabled == {"performance", "tests"}


def test_each_mode_keeps_its_own_roster(db: Session, monkeypatch):
    """Switching modes must not discard the seats configured for the other one —
    an operator who tries the panel and comes back should find their pair
    exactly as they left it, models and all."""
    roster.ensure_seeded(db)
    roster.update(db, roster.all_judges(db)[0].id, {"provider": "gemini", "model": "custom-1"})

    monkeypatch.setattr(settings, "jury_mode", "panel")
    roster.ensure_seeded(db)
    assert len(roster.all_judges(db)) == len(personas.for_mode("panel"))
    assert "implementation" not in {j.persona for j in roster.all_judges(db)}

    monkeypatch.setattr(settings, "jury_mode", "pair")
    pair = roster.all_judges(db)
    assert [j.persona for j in pair] == ["implementation", "systems"]
    assert pair[0].model == "custom-1"
    # And the panel's judges are never polled while the pair is in force.
    assert len(roster.enabled_judges(db)) == 2


def test_an_unknown_mode_falls_back_to_the_default_not_to_an_empty_jury(
        db: Session, monkeypatch):
    """A typo in the setting must not silently produce a roster of nobody, which
    reads downstream as 'this change was not reviewed'."""
    monkeypatch.setattr(settings, "jury_mode", "quorum")
    assert roster.current_mode() == "pair"
    roster.ensure_seeded(db)
    assert len(roster.enabled_judges(db)) == 2


def test_the_pair_briefs_cover_each_other_and_say_so():
    """The failure mode of a two-way split is a seam neither juror claims, so
    each brief has to name what the other one owns."""
    impl, systems = personas.charge("implementation"), personas.charge("systems")
    # Between them: requirements, edge cases, tests, security, patterns, scale.
    assert "PRODUCTION code" in impl and "acceptance criterion" in impl
    assert "Boundaries and empties" in impl and "regression coverage" in impl
    assert "injection" in systems and "existing patterns" in systems
    assert "n=100k" in systems
    # And each hands the other half over explicitly.
    assert "NOT your job" in impl and "security" in impl.split("NOT your job")[1]
    assert "NOT your job" in systems and "acceptance criteria" in systems.split("NOT your job")[1]


def test_judge_inherits_the_review_stage_until_overridden(db: Session):
    judge = roster.create(db, name="J", persona="security")
    settings.review_provider, settings.review_model = "groq", "llama-3.3-70b-versatile"
    assert roster.resolve(judge) == ("groq", "llama-3.3-70b-versatile")

    # A provider set without a model borrows that provider's default, rather
    # than sending the previous provider's model id to a different API.
    judge = roster.update(db, judge.id, {"provider": "gemini", "model": ""})
    provider, model = roster.resolve(judge)
    assert provider == "gemini" and model.startswith("gemini")


def test_custom_judge_requires_a_brief(db: Session):
    with pytest.raises(ValueError, match="focus"):
        roster.create(db, name="Nameless", persona="custom")
    judge = roster.create(db, name="House rules", persona="custom", focus="Check the changelog.")
    assert "Check the changelog." in personas.charge(judge.persona, judge.focus)


def test_builtin_charge_appends_operator_instructions():
    charge = personas.charge("security", "Also flag any new outbound network call.")
    assert "SECURITY juror" in charge
    assert "Also flag any new outbound network call." in charge


def test_move_reorders_and_renumbers_densely(db: Session):
    roster.ensure_seeded(db)
    before = [j.persona for j in roster.all_judges(db)]
    roster.move(db, roster.all_judges(db)[0].id, 1)
    after = roster.all_judges(db)
    assert [j.persona for j in after] == [before[1], before[0], *before[2:]]
    assert [j.position for j in after] == list(range(len(after)))
    # Moving the first judge up is a no-op, not an error.
    roster.move(db, after[0].id, -1)
    assert [j.persona for j in roster.all_judges(db)] == [j.persona for j in after]


def test_spread_gives_enabled_judges_distinct_providers(db: Session, monkeypatch):
    monkeypatch.setattr(settings, "jury_mode", "panel")
    monkeypatch.setattr(roster, "available_providers", lambda: ["groq", "gemini"])
    roster.ensure_seeded(db)
    enabled = roster.enabled_judges(db)
    assert [j.provider for j in enabled] == ["groq", "gemini", "groq", "gemini"]
    # Idempotent: a second spread changes nothing.
    assert roster.spread_providers(db) == 0


def test_the_pair_never_shares_one_model(db: Session, monkeypatch):
    """Two seats mean two chances to catch a defect — and one shared blind spot
    would waste both. Distinct providers matter more here than on a panel, where
    a repeated provider is diluted across several other seats."""
    monkeypatch.setattr(roster, "available_providers", lambda: ["groq", "gemini"])
    roster.ensure_seeded(db)
    providers_used = [roster.resolve(j)[0] for j in roster.enabled_judges(db)]
    assert len(set(providers_used)) == 2


def test_spread_reuses_the_review_model_on_the_review_provider(db: Session, monkeypatch):
    """'openai' is a configurable endpoint — pointed at Groq (the shipped
    default) its registry model ids don't exist, and a bad model id is a silent
    abstention. The Review stage's own pairing is known to work there."""
    roster.ensure_seeded(db)
    monkeypatch.setattr(settings, "review_provider", "openai")
    monkeypatch.setattr(settings, "review_model", "openai/gpt-oss-120b")
    monkeypatch.setattr(roster, "available_providers", lambda: ["openai", "gemini"])
    monkeypatch.setattr(providers, "fetch_models", lambda pid: [])
    roster.spread_providers(db)
    by_provider = {j.provider: j.model for j in roster.enabled_judges(db)}
    assert by_provider["openai"] == "openai/gpt-oss-120b"      # not the registry guess
    assert by_provider["gemini"] == providers.PROVIDERS["gemini"].default_model


def test_spread_drops_a_model_the_endpoint_does_not_serve(db: Session, monkeypatch):
    """A model id the endpoint rejects is an abstention at review time — a juror
    that silently isn't there. Better to inherit the Review stage than to seat a
    judge that can't speak."""
    roster.ensure_seeded(db)
    monkeypatch.setattr(settings, "review_provider", "claude-cli")
    monkeypatch.setattr(settings, "review_model", "haiku")
    monkeypatch.setattr(roster, "available_providers", lambda: ["openai"])
    # The 'openai' provider pointed at an endpoint with a different catalog.
    monkeypatch.setattr(providers, "fetch_models",
                        lambda pid: ["openai/gpt-oss-120b", "llama-3.3-70b-versatile"])
    roster.spread_providers(db)
    judge = roster.enabled_judges(db)[0]
    # Neither registry guess nor haiku (the Review stage's, on a
    # different vendor) exists here — so it lands on the curated model this
    # endpoint does serve, not on whatever happens to sort first.
    assert roster.resolve(judge) == ("openai", "openai/gpt-oss-120b")


def test_spread_avoids_an_arbitrary_model_when_nothing_curated_is_served(
        db: Session, monkeypatch):
    roster.ensure_seeded(db)
    monkeypatch.setattr(settings, "review_provider", "claude-cli")
    monkeypatch.setattr(settings, "review_model", "haiku")
    monkeypatch.setattr(roster, "available_providers", lambda: ["openai"])
    monkeypatch.setattr(providers, "fetch_models", lambda pid: ["some-local-7b", "another-local"])
    roster.spread_providers(db)
    # Last resort only: it still has to name something the endpoint serves.
    assert roster.resolve(roster.enabled_judges(db)[0])[1] == "some-local-7b"


# --- Parsing juror output ------------------------------------------------------

def test_parses_fenced_and_bare_json():
    assert panel._load_json('```json\n{"verdict": "APPROVE"}\n```')["verdict"] == "APPROVE"
    assert panel._load_json('Sure!\n{"verdict": "APPROVE"}\nHope that helps')["verdict"] == "APPROVE"
    assert panel._load_json("not json at all") == {}


def test_parses_double_encoded_json():
    """A juror that wraps its whole answer in an extra layer of string escaping
    (returns a JSON string CONTAINING JSON, e.g. \\n / \\" left escaped, rather
    than a bare object) must still be read — this is what a live gemini-flash
    juror actually returned and silently abstained on before this fix."""
    inner = {"verdict": "APPROVE", "summary": "looks fine"}
    double_encoded = json.dumps(json.dumps(inner, indent=2))
    assert panel._load_json(double_encoded)["verdict"] == "APPROVE"


def test_parses_escaped_json_that_was_never_quoted():
    r"""The nastier cousin of the case above: the juror emits the *escaped* form
    of the object but omits the surrounding quotes, so `{\n \"verdict\": …}`
    arrives with every inner quote and newline backslashed and nothing for those
    escapes to belong to. No direct parse can read it. This is what a live
    gemini-flash juror returned on the rich run — it had voted APPROVE, and the
    panel recorded an ABSTAIN and a coverage gap purely on formatting."""
    inner = {"verdict": "APPROVE", "summary": "Reviewed the ANSI SGR logic.", "findings": []}
    escaped_but_unquoted = json.dumps(json.dumps(inner))[1:-1]

    parsed = panel._load_json(escaped_but_unquoted)
    assert parsed["verdict"] == "APPROVE"
    assert parsed["summary"] == "Reviewed the ANSI SGR logic."


def test_unescaping_is_a_last_resort_not_a_first_guess():
    """A well-formed object whose *content* contains escapes must be read as
    itself, not mangled by the unescape path bolted on for broken jurors."""
    payload = json.dumps({"verdict": "REQUEST_CHANGES",
                          "summary": 'the test asserts \\n and a literal " quote'})
    assert panel._load_json(payload)["summary"] == 'the test asserts \\n and a literal " quote'


def test_normalizes_verdict_synonyms_and_severities():
    verdict, _, findings = panel._normalize({
        "verdict": "changes requested",
        "findings": [{"title": "boom", "severity": "BLOCKER", "confidence": "0.8"}],
    })
    assert verdict == "REQUEST_CHANGES"
    assert findings[0]["severity"] == "medium"     # unknown severity, not dropped
    assert findings[0]["confidence"] == 0.8


def test_infers_a_verdict_when_the_juror_omits_one():
    verdict, _, _ = panel._normalize({"findings": [{"title": "x", "severity": "critical"}]})
    assert verdict == "REQUEST_CHANGES"
    verdict, _, _ = panel._normalize({"summary": "looks fine"})
    assert verdict == "APPROVE"
    assert panel._normalize({})[0] == "ABSTAIN"


def test_confidence_is_clamped_and_defaults_to_neutral():
    assert panel._clamp_confidence(5) == 1.0
    assert panel._clamp_confidence(-2) == 0.0
    assert panel._clamp_confidence("very sure") == 0.5


def test_ungrounded_findings_are_dropped():
    assert panel._normalize_finding({"severity": "high"}) is None
    assert panel._normalize_finding("a string") is None
    assert panel._normalize_finding({"issue": "real"})["title"] == "real"


# --- The case file the judges receive -------------------------------------------

def test_judges_see_the_original_request_above_the_criteria():
    """A panel holding only the acceptance criteria can check the change against
    the PM's restatement, but cannot notice the restatement itself missed the
    point — which is exactly how a delivery that fixed nothing got approved."""
    from app.services.jury import prompts

    user = prompts.judge_user(
        "CHARGE", "scope-3", "Fix Windows capture", ["all lines render"],
        "diff --git a/tests/x.py b/tests/x.py",
        request="I captured the output of a program run on Windows and passed it to the "
                "library; the earlier lines came out blank.",
        description="The capture path drops early lines.",
        localization="The plan this change was supposed to implement:\n"
                     "  rich/console.py::capture")

    # The user's own words appear, and appear BEFORE the derived criteria.
    assert "program run on Windows" in user
    assert user.index("program run on Windows") < user.index("all lines render")
    # The PM never read the code, so its notes are framed as commentary.
    assert "never read the code" in user
    assert "outranks every restatement" in user
    # The plan (verified against the graph) is what the diff is judged against.
    assert "rich/console.py::capture" in user


def test_the_diff_budget_follows_what_the_seats_can_actually_take():
    """A juror judging half a diff makes confident claims about code that was
    never on screen. The cap existed for a free tier's 22K request limit, so it
    should bind only where that limit binds."""
    from app.services.jury import prompts

    # Groq's free tier: the historical floor, unchanged.
    assert prompts.diff_budget(["groq"]) == prompts.DIFF_CHARS
    # A roomy seat gets a far bigger diff…
    assert prompts.diff_budget(["gemini"]) > 50_000
    assert prompts.diff_budget(["anthropic"]) > 200_000
    # …but the shared case file is sized to the TIGHTEST seat, or the other one
    # would be middle-trimmed blindly at the transport.
    assert prompts.diff_budget(["gemini", "groq"]) == prompts.DIFF_CHARS
    # Unknown/unresolved seats fall back rather than overshooting.
    assert prompts.diff_budget([]) == prompts.DIFF_CHARS


def test_a_cut_diff_says_so_instead_of_looking_like_the_whole_change():
    """The one thing worse than a clipped diff is a clipped diff a juror reads as
    complete — it reports the tail as missing code and blocks a correct change."""
    from app.services.jury import prompts

    diff = "\n".join(f"+    line {i}" for i in range(4000))
    case = prompts.judge_case("T", "t", ["c"], diff, diff_chars=1000)
    assert "DISPLAY CUT, not the end of the code" in case
    assert "more characters of the diff omitted" in case


def test_a_whole_diff_is_sent_whole_when_it_fits():
    from app.services.jury import prompts

    diff = "\n".join(f"+    line {i}" for i in range(200))
    case = prompts.judge_case("T", "t", ["c"], diff, diff_chars=prompts.DIFF_CHARS)
    assert "DISPLAY CUT" not in case
    assert "line 199" in case


def test_judges_are_offered_repository_lookups_before_they_decide():
    """A finding about code outside the diff is a guess unless it was checked,
    and a wrong blocking finding costs a full paid Dev+QA+Review round."""
    from app.services.jury import prompts

    user = prompts.judge_user("CHARGE", "T-1", "t", ["c1"], "diff")
    assert "<<<CALLERS SymbolName>>>" in user
    # Offered immediately before the output contract, so it is in view exactly
    # when the juror decides whether it can support its finding.
    assert user.index("<<<CALLERS") < user.index('"verdict"')


def test_the_lookup_offer_disappears_when_reachback_is_disabled(monkeypatch):
    from app.config import settings
    from app.services.jury import prompts

    monkeypatch.setattr(settings, "jury_tool_calls", 0)
    assert "<<<CALLERS" not in prompts.judge_user("CHARGE", "T-1", "t", ["c1"], "diff")


def test_case_file_degrades_cleanly_without_the_new_context():
    """Older callers (and the single-reviewer path) pass no request/description."""
    from app.services.jury import prompts

    user = prompts.judge_user("CHARGE", "T-1", "t", ["c1"], "diff")
    assert "c1" in user and "CHARGE" in user
    assert "What the user actually asked for" not in user


def test_correctness_charge_rejects_a_tests_only_bug_fix():
    """The observed failure: Dev added four tests, changed no production code,
    and every judge approved. The charge now names that case explicitly."""
    charge = personas.charge("correctness")
    assert "PRODUCTION code" in charge
    assert "would they FAIL against the code as it was before" in charge


def test_rewritten_test_assertions_raise_a_deterministic_alert():
    """The observed failure: Dev introduced an off-by-one, then edited the
    existing assertion to match it, and a green suite hid the whole thing."""
    diff = (
        "diff --git a/rich/_windows_renderer.py b/rich/_windows_renderer.py\n"
        "-    term.move_cursor_to(WindowsCoordinates(row=y - 1, col=x - 1))\n"
        "+    term.move_cursor_to(WindowsCoordinates(row=y, col=x))\n"
        "diff --git a/tests/test_windows_renderer.py b/tests/test_windows_renderer.py\n"
        "-        WindowsCoordinates(row=29, col=19)\n"
        "+        WindowsCoordinates(row=30, col=20)\n"
    )
    alert = panel.tampering_brief(diff)
    # The assertion that burned us was a continuation line with no "assert"
    # keyword on it, so keyword matching alone must not be what triggers this.
    assert "DELETES OR REWRITES 1 line(s) of EXISTING test code" in alert
    assert "row=29" in alert
    assert "Do not assume (a)" in alert


def test_adding_new_tests_is_not_flagged_as_tampering():
    """New assertions are the wanted case — flagging them would train the panel
    to ignore the alert."""
    diff = ("diff --git a/tests/test_x.py b/tests/test_x.py\n"
            "+    assert render(x) == 'expected'\n"
            "+    assert other(y) == 2\n")
    assert panel.tampering_brief(diff) == ""
    # Production-code deletions are ordinary refactoring, not tampering.
    assert panel.tampering_brief("diff --git a/app/x.py b/app/x.py\n-    assert cfg\n") == ""


# --- Polling ------------------------------------------------------------------

def _judge(name="J", persona="correctness"):
    return Judge(id=1, name=name, persona=persona, provider="groq", model="m")


def test_a_failing_juror_abstains_rather_than_approving(monkeypatch):
    monkeypatch.setattr(panel, "_call", lambda *a, **k: {
        "text": "", "error": "429 rate limited", "tokens_in": 0, "tokens_out": 0, "cost": 0})
    op = panel.poll_judge(_judge(), {"task_key": "T", "title": "t", "criteria": [], "diff": "d"})
    assert op.error and not op.usable and op.verdict == "ABSTAIN"


def test_a_juror_that_raises_abstains_instead_of_killing_the_panel(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("provider exploded")
    monkeypatch.setattr(panel, "_call", boom)
    op = panel.poll_judge(_judge(), {"task_key": "T", "title": "t", "criteria": [], "diff": "d"})
    assert not op.usable and "provider exploded" in op.error


def test_unparseable_output_abstains_and_keeps_the_text_for_diagnosis(monkeypatch):
    monkeypatch.setattr(panel, "_call", lambda *a, **k: {
        "text": "I think it's fine honestly", "error": None,
        "tokens_in": 10, "tokens_out": 5, "cost": 0.01})
    op = panel.poll_judge(_judge(), {"task_key": "T", "title": "t", "criteria": [], "diff": "d"})
    assert not op.usable
    assert "I think it's fine honestly" in op.error
    assert op.tokens_in == 10        # the call was still paid for; bill it


def test_agent_cli_judge_gets_a_callable_event_sink(monkeypatch):
    """The agent-backend adapters call on_event unconditionally. Passing None
    took the juror down with a TypeError, which looks exactly like an
    abstention — a whole perspective lost, quietly."""
    seen = {}

    def fake_run(backend, workdir, prompt, on_event, model=None):
        on_event("info", "adapters do this")          # must not raise
        seen["backend"], seen["model"] = backend, model
        return {"text": '{"verdict": "APPROVE", "findings": []}', "error": None,
                "tokens_in": 5, "tokens_out": 5, "cost": 0.01}

    monkeypatch.setattr(panel.agent_backends, "run", fake_run)
    judge = Judge(id=1, name="Arch", persona="architecture", provider="claude-cli", model="haiku")
    op = panel.poll_judge(judge, {"task_key": "T", "title": "t", "criteria": [], "diff": "d"},
                          workdir="/tmp/repo")
    assert op.usable and op.verdict == "APPROVE"
    assert seen == {"backend": "claude-code", "model": "haiku"}


def test_empanel_polls_every_judge(monkeypatch):
    monkeypatch.setattr(panel, "_call", lambda *a, **k: {
        "text": '{"verdict": "APPROVE", "summary": "ok", "findings": []}',
        "error": None, "tokens_in": 1, "tokens_out": 1, "cost": 0.0})
    rows = [Judge(id=i, name=f"J{i}", persona="correctness") for i in range(1, 5)]
    events = []
    ops = panel.empanel(rows, {"task_key": "T", "title": "t", "criteria": [], "diff": "d"},
                        on_event=lambda lvl, msg: events.append((lvl, msg)))
    assert len(ops) == 4 and all(o.usable for o in ops)
    assert any("Empanelling 4 judge" in m for _, m in events)


def test_a_failed_juror_on_the_pair_is_retried_once(monkeypatch):
    """Under unanimity a lost seat is an INCONCLUSIVE delivery, not a smaller
    jury — and the usual cause is a per-minute rate limit that has cleared by
    the time we ask again. The first attempt is still billed."""
    calls = []

    def _flaky(system, user, provider, model, workdir="", cache_prefix=""):
        calls.append(model)
        if len(calls) == 1:
            return {"text": "", "error": "429 rate limited", "tokens_in": 7,
                    "tokens_out": 0, "cost": 0.02}
        return {"text": '{"verdict": "APPROVE", "summary": "ok", "findings": []}',
                "error": None, "tokens_in": 3, "tokens_out": 1, "cost": 0.01}

    monkeypatch.setattr(panel, "_call", _flaky)
    monkeypatch.setattr(panel.time, "sleep", lambda _s: None)
    ops = panel.empanel([Judge(id=1, name="Systems", persona="systems", mode="pair")],
                        {"task_key": "T", "title": "t", "criteria": [], "diff": "d"})
    assert len(calls) == 2
    assert ops[0].usable and ops[0].verdict == "APPROVE"
    assert ops[0].tokens_in == 10 and ops[0].cost == pytest.approx(0.03)


def test_a_full_panel_does_not_stampede_a_rate_limited_provider(monkeypatch):
    """There the foreperson is told about the gap and the other seats still
    cover most of the review, so retrying every failure would turn a provider
    outage into several times the traffic for no verdict change."""
    calls = []
    monkeypatch.setattr(panel, "_call", lambda *a, **k: calls.append(1) or {
        "text": "", "error": "429", "tokens_in": 0, "tokens_out": 0, "cost": 0})
    rows = [Judge(id=i, name=f"J{i}", persona="correctness", mode="panel") for i in range(1, 5)]
    ops = panel.empanel(rows, {"task_key": "T", "title": "t", "criteria": [], "diff": "d"})
    assert len(calls) == 4
    assert all(not o.usable for o in ops)


def test_every_juror_gets_a_byte_identical_case_file(monkeypatch):
    """The panel's prompt-cache saving depends on this and nothing else: if the
    shared prefix differs by one character between jurors, every provider that
    discounts a repeated prefix charges all of them full price."""
    seen: list[str] = []

    def _spy(system, user, provider, model, workdir="", cache_prefix=""):
        seen.append(cache_prefix)
        return {"text": '{"verdict": "APPROVE", "summary": "ok", "findings": []}',
                "error": None, "tokens_in": 1, "tokens_out": 1, "cost": 0.0}

    monkeypatch.setattr(panel, "_call", _spy)
    rows = [Judge(id=i, name=f"J{i}", persona=p) for i, p in enumerate(
        ("correctness", "reliability", "security", "architecture"), start=1)]
    panel.empanel(rows, {"task_key": "T", "title": "t", "criteria": ["c"],
                         "diff": "d" * 200, "context": "ctx"})
    assert len(seen) == 4
    assert len(set(seen)) == 1, "jurors were sent different case files"
    assert seen[0], "the case file must not be empty, or there is nothing to cache"


def test_the_juror_charge_comes_after_the_shared_case_not_before(monkeypatch):
    """Ordering is the optimisation. The per-juror charge must live in the tail
    so it cannot break the shared prefix."""
    captured: dict[str, str] = {}

    def _spy(system, user, provider, model, workdir="", cache_prefix=""):
        captured["user"] = user
        captured["prefix"] = cache_prefix
        return {"text": '{"verdict": "APPROVE", "summary": "s", "findings": []}',
                "error": None, "tokens_in": 1, "tokens_out": 1, "cost": 0.0}

    monkeypatch.setattr(panel, "_call", _spy)
    panel.poll_judge(Judge(id=1, name="J", persona="security"),
                     {"task_key": "T", "title": "t", "criteria": [], "diff": "d"})
    assert "SECURITY juror" in captured["user"]
    assert "SECURITY juror" not in captured["prefix"]
    assert "CASE T" in captured["prefix"]


# --- Synthesis ----------------------------------------------------------------

def _op(name, verdict="REQUEST_CHANGES", findings=(), error=""):
    return panel.Opinion(judge_id=None, name=name, persona="correctness", provider="groq",
                         model="m", verdict=verdict, summary="s",
                         findings=[panel._normalize_finding(f) for f in findings], error=error)


def test_a_panel_that_all_failed_is_inconclusive_not_approved():
    decision = synthesis.deliberate(
        [_op("A", error="timeout"), _op("B", error="429")], "T", "t", [])
    assert decision["verdict"] == "INCONCLUSIVE"
    assert "NOT been code-reviewed" in decision["rationale"]


def test_deterministic_synthesis_merges_agreeing_jurors(monkeypatch):
    monkeypatch.setattr(settings, "jury_min_confidence", 0.5)
    f = {"title": "auth check missing on delete route", "severity": "high", "confidence": 0.9,
         "location": "app/routes.py:40"}
    g = {"title": "delete route missing auth check", "severity": "critical", "confidence": 0.7,
         "location": "app/routes.py:41"}
    decision = synthesis._deterministic([_op("A", findings=[f]), _op("B", findings=[g])], 0.5)
    assert len(decision["blocking"]) == 1
    entry = decision["blocking"][0]
    assert entry["raised_by"] == ["A", "B"] and entry["agreement"] == "majority"
    assert entry["severity"] == "critical"       # the graver read wins
    assert decision["verdict"] == "CHANGES REQUESTED"


def test_tokenizer_splits_identifiers_so_phrasings_can_agree():
    """Jurors write the same defect in different registers — one says
    "the --dry-run flag", another says "dry_run". As opaque strings those share
    nothing, so the fallback would report one defect twice."""
    assert synthesis._tokens("dry_run write_all") >= {"dry", "run", "write", "all"}
    assert synthesis._tokens("parseDryRunFlag") >= {"parse", "dry", "run", "flag"}
    assert synthesis._tokens("the a of in") == set()      # stopwords carry no signal


def test_deterministic_synthesis_dismisses_low_confidence_guesses():
    f = {"title": "might leak memory somewhere", "severity": "high", "confidence": 0.2}
    decision = synthesis._deterministic([_op("A", findings=[f])], 0.5)
    assert not decision["blocking"]
    assert decision["dismissed"][0]["reason"].startswith("confidence 0.20")
    assert decision["verdict"] == "APPROVED"


def test_deterministic_synthesis_does_not_block_on_medium_findings():
    f = {"title": "naming could be clearer", "severity": "medium", "confidence": 0.9}
    decision = synthesis._deterministic([_op("A", findings=[f])], 0.5)
    assert decision["verdict"] == "APPROVED"
    assert decision["observations"] and not decision["blocking"]
    assert decision["synthesis"] == "deterministic"


def test_foreperson_cannot_request_changes_with_nothing_blocking(monkeypatch):
    """A verdict with no blocking findings sends Dev back with nothing to fix —
    it burns a paid round and returns a byte-identical diff."""
    monkeypatch.setattr(synthesis.providers, "can_chat", lambda p: True)
    monkeypatch.setattr(synthesis.llm, "chat", lambda *a, **k: {
        "text": '{"verdict": "CHANGES REQUESTED", "rationale": "vibes", '
                '"blocking": [], "observations": [], "dismissed": []}',
        "tokens_in": 1, "tokens_out": 1, "cost": 0.0})
    decision = synthesis.deliberate([_op("A", verdict="APPROVE")], "T", "t", [])
    assert decision["verdict"] == "APPROVED"
    assert decision["synthesis"] == "foreperson"


def test_foreperson_blocking_respects_the_confidence_floor(monkeypatch):
    monkeypatch.setattr(settings, "jury_min_confidence", 0.6)
    monkeypatch.setattr(synthesis.providers, "can_chat", lambda p: True)
    monkeypatch.setattr(synthesis.llm, "chat", lambda *a, **k: {
        "text": '{"verdict": "CHANGES REQUESTED", "rationale": "r", "blocking": ['
                '{"title": "solid", "severity": "high", "confidence": 0.9},'
                '{"title": "hunch", "severity": "high", "confidence": 0.3}], '
                '"observations": [], "dismissed": []}',
        "tokens_in": 1, "tokens_out": 1, "cost": 0.0})
    decision = synthesis.deliberate([_op("A")], "T", "t", [])
    assert [b["title"] for b in decision["blocking"]] == ["solid"]
    assert decision["verdict"] == "CHANGES REQUESTED"


def test_falls_back_to_mechanical_synthesis_when_the_foreperson_fails(monkeypatch):
    monkeypatch.setattr(synthesis.providers, "can_chat", lambda p: True)

    def boom(*a, **k):
        raise RuntimeError("no route to host")
    monkeypatch.setattr(synthesis.llm, "chat", boom)
    events = []
    decision = synthesis.deliberate([_op("A", verdict="APPROVE")], "T", "t", [],
                                    on_event=lambda lvl, m: events.append(m))
    assert decision["synthesis"] == "deterministic"
    assert any("mechanical synthesis" in m for m in events)


def test_render_ends_in_a_verdict_line_the_pipeline_can_read():
    ops = [_op("A", verdict="APPROVE"), _op("B", error="timeout")]
    decision = {"verdict": "APPROVED", "rationale": "fine", "blocking": [],
                "observations": [], "dismissed": [], "synthesis": "foreperson"}
    text = synthesis.render(decision, ops)
    assert text.strip().endswith("VERDICT: APPROVED")
    assert "CHANGES REQUESTED" not in text
    # An abstention is called out, not quietly folded into the approval.
    assert "Abstained" in text and "B — timeout" in text


# --- Consensus: the pair's decision rule ---------------------------------------

def test_the_pair_approves_only_when_both_approve():
    decision = synthesis.consensus([_op("Implementation", verdict="APPROVE"),
                                    _op("Systems", verdict="APPROVE")])
    assert decision["verdict"] == "APPROVED"
    assert decision["synthesis"] == "consensus"
    assert not decision["blocking"]
    # No foreperson ran, so the verdict costs nothing and cannot fail.
    assert decision["cost"] == 0.0 and decision["tokens_in"] == 0


def test_one_dissent_is_enough_to_send_it_back():
    """The jurors hold complementary briefs, so the approving juror is not a
    second opinion on the dissenter's finding — it is silence about a subject it
    was never asked to look at. Majority voting across a split brief would let
    the half that never looked outvote the half that did."""
    f = {"title": "auth check missing on the delete route", "severity": "high",
         "confidence": 0.9, "location": "app/routes.py:40"}
    decision = synthesis.consensus([_op("Implementation", verdict="APPROVE"),
                                    _op("Systems", verdict="REQUEST_CHANGES", findings=[f])])
    assert decision["verdict"] == "CHANGES REQUESTED"
    assert [b["title"] for b in decision["blocking"]] == [f["title"]]
    assert decision["blocking"][0]["raised_by"] == ["Systems"]
    assert "Systems" in decision["rationale"]


def test_an_abstention_is_never_an_approval():
    """Half the review did not happen. With no spare seat to cover it, the
    surviving juror's approval cannot stand in for the missing one — this is the
    exact path by which an unreviewed delivery once shipped looking clean."""
    decision = synthesis.consensus([_op("Implementation", verdict="APPROVE"),
                                    _op("Systems", error="429 rate limited")])
    assert decision["verdict"] == "INCONCLUSIVE"
    assert "DID NOT HAPPEN" in decision["rationale"]


def test_a_dissenter_with_no_gradeable_finding_still_blocks_with_an_explanation():
    """Its vote stands — but Dev cannot act on "no". An empty blocking list burns
    a paid round and returns a byte-identical diff."""
    hunch = {"title": "might be racy", "severity": "high", "confidence": 0.1}
    decision = synthesis.consensus(
        [_op("Implementation", verdict="APPROVE"),
         _op("Systems", verdict="REQUEST_CHANGES", findings=[hunch])], min_confidence=0.5)
    assert decision["verdict"] == "CHANGES REQUESTED"
    assert len(decision["blocking"]) == 1
    assert decision["blocking"][0]["raised_by"] == ["Systems"]
    # The unsupported hunch itself is dismissed, not promoted into the block.
    assert [d["title"] for d in decision["dismissed"]] == ["might be racy"]


def test_a_finding_from_an_approving_juror_is_reported_not_enforced():
    """That juror weighed it and still approved; overriding it would block on
    something nobody asked to block on, at the cost of a full paid round."""
    nit = {"title": "name could be clearer", "severity": "medium", "confidence": 0.9}
    decision = synthesis.consensus([_op("Implementation", verdict="APPROVE", findings=[nit]),
                                    _op("Systems", verdict="APPROVE")])
    assert decision["verdict"] == "APPROVED"
    assert [o["title"] for o in decision["observations"]] == ["name could be clearer"]


def test_both_jurors_on_one_defect_reads_as_unanimous_not_majority():
    """Two of two IS everyone. A fixed 'unanimous means three' threshold would
    understate the strongest signal a pair can produce."""
    a = {"title": "dry_run never reaches write_all", "severity": "high", "confidence": 0.8,
         "location": "app/writer.py:40"}
    b = {"title": "the --dry-run flag is not threaded through to write_all",
         "severity": "critical", "confidence": 0.7, "location": "app/writer.py:41"}
    decision = synthesis.consensus([_op("Implementation", verdict="REQUEST_CHANGES", findings=[a]),
                                    _op("Systems", verdict="REQUEST_CHANGES", findings=[b])])
    assert len(decision["blocking"]) == 1
    assert decision["blocking"][0]["agreement"] == "unanimous"
    assert decision["blocking"][0]["severity"] == "critical"   # the graver read wins


def test_consensus_renders_the_rule_and_a_verdict_line_the_pipeline_can_read():
    ops = [_op("Implementation", verdict="APPROVE"), _op("Systems", verdict="APPROVE")]
    text = synthesis.render(synthesis.consensus(ops), ops)
    assert text.strip().endswith("VERDICT: APPROVED")
    assert "UNANIMITY" in text and "no foreperson" in text.lower()


def test_the_pair_never_calls_a_foreperson(db: Session, monkeypatch):
    """End to end through the public entry point: the default review path must
    not make a synthesis call at all."""
    from app.services import jury

    roster.ensure_seeded(db)          # the shipped pair, through the real roster
    monkeypatch.setattr(panel, "_call", lambda *a, **k: {
        "text": '{"verdict": "APPROVE", "summary": "ok", "findings": []}',
        "error": None, "tokens_in": 1, "tokens_out": 1, "cost": 0.0})

    def no_foreperson(*a, **k):
        raise AssertionError("the pair must not call a synthesis model")
    monkeypatch.setattr(synthesis.llm, "chat", no_foreperson)

    res = jury.review("T", "t", ["c"], "diff")
    assert res["decision"]["verdict"] == "APPROVED"
    assert res["decision"]["synthesis"] == "consensus"
    assert [j["persona"] for j in res["decision"]["jurors"]] == ["implementation", "systems"]
    # The review stage's own run carries no synthesis usage to bill.
    assert res["cost"] == 0.0


def test_the_pair_juror_is_told_there_is_no_foreperson_behind_it(monkeypatch):
    """Its calibration IS the jury's calibration: nothing downstream will catch
    an overreach, and nothing else is looking at its half."""
    captured: dict[str, str] = {}

    def _spy(system, user, provider, model, workdir="", cache_prefix=""):
        captured["system"] = system
        return {"text": '{"verdict": "APPROVE", "summary": "s", "findings": []}',
                "error": None, "tokens_in": 1, "tokens_out": 1, "cost": 0.0}

    monkeypatch.setattr(panel, "_call", _spy)
    panel.poll_judge(Judge(id=1, name="Systems", persona="systems", mode="pair"),
                     {"task_key": "T", "title": "t", "criteria": [], "diff": "d"})
    assert "NO FOREPERSON" in captured["system"]
    assert "unanimity is required" in captured["system"]


# --- Panel entry point / API ---------------------------------------------------

def test_an_empty_panel_is_inconclusive_not_approved(db: Session, monkeypatch):
    from app.services import jury

    monkeypatch.setattr(roster, "ensure_seeded", lambda _db: 0)
    monkeypatch.setattr(roster, "enabled_judges", lambda _db: [])
    res = jury.review("T", "t", [], "diff")
    assert res["decision"]["verdict"] == "INCONCLUSIVE"
    assert "NOT been code-reviewed" in res["text"]

