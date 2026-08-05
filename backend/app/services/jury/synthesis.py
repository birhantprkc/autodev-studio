"""Turning independent opinions into one decision.

There are two procedures here, because there are two juries:

``consensus`` — PAIR mode, the default. Two jurors, briefs that split the whole
of code review between them, and a rule instead of an arbiter: it is APPROVED
when every seated juror approves, and CHANGES REQUESTED the moment one does not.
No model is asked to weigh the two against each other. At two jurors there is
nothing to arbitrate — a third model reading two opinions can only add its own
error, and the one thing it can do that the rule cannot is overrule a juror that
was right. What the foreperson is genuinely good at (merging duplicates) is
worth much less here: two jurors who were told to look at different halves are
not supposed to find the same defect, and when they do, that agreement is signal
worth surfacing rather than volume worth compressing.

``deliberate`` — PANEL mode. Five or six narrow specialists, where concatenating
the reviews and handing them to the Dev agent would be strictly WORSE than one
reviewer: five times the volume, five times the false positives, and no way to
tell a defect two jurors found independently from one juror's pet theory. So a
foreperson does the four things a reader of five reviews would do by hand —

  merge duplicates · resolve conflicts · drop guesses · decide

It runs as an LLM call because merging semantically-equivalent findings written
in different words is a language problem. But it must not be a single point of
failure, so ``_deterministic`` reproduces the same decision procedure with crude
mechanical rules whenever the LLM call fails — deliberately more conservative,
and always labelled as such so nobody reads a fallback verdict as a real one.

Both return the same decision shape, and both hold the same line: an opinion
that never arrived is not an approval.
"""

from __future__ import annotations

import logging
import re

from ...config import settings
from .. import llm, providers
from . import panel, prompts

logger = logging.getLogger(__name__)

_BLOCKING_SEVERITIES = ("critical", "high", "medium")
_SEV_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_STOPWORD_TEXT = (
    "the a an is are be to of in on for and or not it its this that with without "
    "when if then no missing lacks lack does do doesn't should could would may "
    "can code change diff function method value error case handling")
_STOPWORDS = frozenset(_STOPWORD_TEXT.split())


_IDENT_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def _tokens(text: str) -> set[str]:
    """Content words, with identifiers broken into their parts.

    Jurors describe the same defect in different registers — one writes
    "the --dry-run flag never reaches the writer", another writes "dry_run is
    not threaded through to write_all". Treated as opaque strings those share
    nothing; split on case and punctuation they share {dry, run, write}. This
    is what lets the mechanical fallback recognise agreement at all."""
    words = _IDENT_SPLIT.split(text or "")
    return {w for w in (x.lower() for x in words if x)
            if len(w) > 2 and w not in _STOPWORDS}


def _same_finding(a: dict, b: dict) -> bool:
    """Coarse 'these two jurors mean the same defect' test for the fallback path.

    Jaccard overlap on content words, with a same-file bonus. This is a blunt
    instrument — the LLM foreperson is far better at it — but it is enough to
    stop the fallback from reporting one defect four times."""
    ta, tb = _tokens(a.get("title", "")), _tokens(b.get("title", ""))
    if not ta or not tb:
        return False
    overlap = len(ta & tb) / len(ta | tb)
    if overlap >= 0.5:
        return True
    fa = (a.get("location") or "").split(":")[0].strip()
    fb = (b.get("location") or "").split(":")[0].strip()
    return bool(fa) and fa == fb and overlap >= 0.3


def _cluster(opinions: list[panel.Opinion]) -> list[dict]:
    """Group the jurors' findings so one defect two of them saw is one entry.

    Shared by both mechanical procedures (the foreperson's fallback and the
    pair's consensus). Cross-juror agreement is the single strongest signal an
    ensemble produces, and it only exists once the duplicates are recognised."""
    clusters: list[dict] = []
    for op in opinions:
        if not op.usable:
            continue
        for f in op.findings:
            for c in clusters:
                if _same_finding(c, f):
                    c["raised_by"].append(op.name)
                    c["confidence"] = max(c["confidence"], f["confidence"])
                    # Two jurors on one defect: keep the graver read of it.
                    if _SEV_RANK[f["severity"]] < _SEV_RANK[c["severity"]]:
                        c["severity"] = f["severity"]
                    break
            else:
                clusters.append({**f, "raised_by": [op.name]})
    return clusters


def _entry(cluster: dict, *, seated: int = 0) -> dict:
    """One cluster as a decision entry, with how much of the jury stood behind it.

    ``seated`` is how many jurors actually reviewed: with two of them, "both of
    them" is unanimity, and calling that "majority" (as a fixed threshold of 3
    would) understates the strongest evidence a pair can produce."""
    who = sorted(set(cluster["raised_by"]))
    n = len(who)
    bar = seated if seated else 3
    return {
        "title": cluster["title"], "location": cluster["location"],
        "why_it_matters": cluster["why_it_matters"], "severity": cluster["severity"],
        "confidence": cluster["confidence"], "suggestion": cluster["suggestion"],
        "raised_by": who,
        "agreement": "unanimous" if n >= bar else ("majority" if n >= 2 else "single"),
    }


def _deterministic(opinions: list[panel.Opinion], min_confidence: float) -> dict:
    """Mechanical synthesis, used when the foreperson model can't be reached.

    Deliberately stricter about what blocks than the LLM path: only critical and
    high findings that clear the confidence floor. Nothing here can judge whether
    a medium finding is a real defect or a preference, and wrongly blocking costs
    a full paid revision round — so mediums are reported, not enforced."""
    blocking, observations, dismissed = [], [], []
    for c in _cluster(opinions):
        entry = _entry(c)
        if c["confidence"] < min_confidence:
            dismissed.append({**entry, "reason": f"confidence {c['confidence']:.2f} is below "
                                                 f"the panel's floor of {min_confidence:.2f}"})
        elif c["severity"] in ("critical", "high"):
            blocking.append(entry)
        else:
            observations.append(entry)
    blocking.sort(key=lambda e: (e["severity"] != "critical", -e["confidence"]))
    return {
        "verdict": "CHANGES REQUESTED" if blocking else "APPROVED",
        "rationale": (
            "Synthesized WITHOUT the foreperson model (it could not be reached) — merged "
            "mechanically by title overlap and filtered by confidence. Only critical/high "
            "findings were allowed to block; medium findings are listed as observations "
            "and were NOT enforced, so this verdict is more permissive than a full "
            "foreperson review."),
        "blocking": blocking, "observations": observations, "dismissed": dismissed,
        "synthesis": "deterministic",
    }


def _unreviewed(abstained: list[panel.Opinion]) -> dict:
    """The decision when nobody's opinion can be trusted. Shared by both
    procedures, because both have exactly one way to get this wrong."""
    reasons = "; ".join(f"{o.name}: {o.error or 'no opinion'}" for o in abstained) or "no judges"
    return {
        "verdict": "INCONCLUSIVE",
        "rationale": f"No juror returned a usable opinion ({reasons[:400]}). "
                     "This change has NOT been code-reviewed.",
        "blocking": [], "observations": [], "dismissed": [],
        "synthesis": "none", "tokens_in": 0, "tokens_out": 0, "cost": 0.0,
    }


def _dissent_entry(op: panel.Opinion, min_confidence: float) -> dict:
    """A blocking entry for a juror that voted REQUEST_CHANGES without leaving a
    finding the bar accepts.

    Its vote still stands — under a unanimity rule a dissent IS the verdict — but
    Dev cannot act on "no". Sending it back with an empty blocking list burns a
    paid round and returns a byte-identical diff, so the juror's own words become
    the entry. Marked at the floor, and at medium: this is a juror that could not
    substantiate itself, not a confirmed defect.
    """
    summary = (op.summary or "").strip()
    head = summary.split(". ")[0][:300] if summary else ""
    return {
        "title": head or f"{op.name} withheld approval",
        "location": "", "severity": "medium", "confidence": min_confidence,
        "why_it_matters": summary[:1200] or (
            "This juror voted to request changes but did not record a finding that "
            "clears the panel's bar. Its objection is unexplained — treat it as a "
            "question to answer, not a defect to patch blindly."),
        "suggestion": "", "raised_by": [op.name], "agreement": "single",
    }


def consensus(opinions: list[panel.Opinion], min_confidence: float | None = None) -> dict:
    """PAIR mode's decision rule: unanimity, no foreperson.

    APPROVED requires that every seated juror reviewed AND approved. Anything
    else goes back to Dev with the reasons attached. There are three ways to not
    be approved, and they are deliberately different outcomes:

    * A juror voted REQUEST_CHANGES → CHANGES REQUESTED. One dissent is enough;
      the jurors were given DIFFERENT halves of the review, so the other one
      approving is not a second opinion on this finding — it is silence about a
      subject that was never its own. Majority voting across a split brief would
      mean 'the security half can be outvoted by the half that never looked'.
    * Every juror that spoke approved, but one abstained → INCONCLUSIVE. With two
      seats an abstention is half the review missing, and a half-reviewed change
      that ships looking approved is the exact failure this subsystem exists to
      prevent. It is NOT downgraded to an approval by the surviving juror.
    * Nobody spoke → INCONCLUSIVE.

    No LLM call, so no usage: the verdict costs nothing and cannot fail.
    """
    if min_confidence is None:
        min_confidence = float(settings.jury_min_confidence)
    usable = [o for o in opinions if o.usable]
    abstained = [o for o in opinions if not o.usable]
    if not usable:
        return _unreviewed(abstained)

    dissenters = [o for o in usable if o.verdict == "REQUEST_CHANGES"]
    dissenting_names = {o.name for o in dissenters}
    seated = len(usable)

    blocking, observations, dismissed = [], [], []
    for c in _cluster(usable):
        entry = _entry(c, seated=seated)
        raised_by_dissenter = bool(dissenting_names & set(entry["raised_by"]))
        if c["confidence"] < min_confidence:
            dismissed.append({**entry, "reason": f"confidence {c['confidence']:.2f} is below "
                                                 f"the jury's floor of {min_confidence:.2f}"})
        elif c["severity"] == "low" or not raised_by_dissenter:
            # A finding from a juror that still voted APPROVE is that juror
            # telling us it is not worth a round — take it at its word and
            # report it rather than enforcing it.
            observations.append(entry)
        else:
            blocking.append(entry)
    blocking.sort(key=lambda e: (_SEV_RANK.get(e["severity"], 3), -e["confidence"]))

    # A dissenter whose every finding was filtered out still gets to block — but
    # it must arrive with something Dev can read.
    for op in dissenters:
        if not any(op.name in e["raised_by"] for e in blocking):
            blocking.append(_dissent_entry(op, min_confidence))

    names = ", ".join(sorted(o.name for o in usable))
    if dissenters:
        who = ", ".join(sorted(dissenting_names))
        verdict = "CHANGES REQUESTED"
        rationale = (
            f"{who} withheld approval, so this change is not approved: the jury decides by "
            f"unanimity and every seated juror must approve. The jurors review different "
            f"halves of the change, so a dissent is not outvoted by the others' approval — "
            f"nobody else examined what {who} examined. {len(blocking)} finding(s) go back "
            f"to Dev.")
    elif abstained:
        verdict = "INCONCLUSIVE"
        missing = ", ".join(f"{o.name} ({o.error or 'no opinion'})"[:160] for o in abstained)
        rationale = (
            f"{names} approved, but {missing} never returned an opinion — so that half of "
            f"the review DID NOT HAPPEN. A jury of this size has no spare seat to cover a "
            f"missing one, and an unreviewed half is not an approval. Re-run the review.")
    else:
        verdict = "APPROVED"
        rationale = (
            f"Unanimous: all {seated} juror(s) ({names}) approved after reviewing "
            f"complementary halves of the change"
            + (f". {len(observations)} non-blocking observation(s) recorded." if observations
               else " with nothing blocking."))

    return {
        "verdict": verdict, "rationale": rationale,
        "blocking": blocking, "observations": observations, "dismissed": dismissed,
        "synthesis": "consensus",
        "votes": {o.name: o.verdict for o in opinions},
        "tokens_in": 0, "tokens_out": 0, "cost": 0.0,
    }


def _coerce_entries(raw, *, keep_reason: bool = False) -> list[dict]:
    out = []
    for item in (raw or []):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        entry = {
            "title": title[:300],
            "location": str(item.get("location") or "").strip()[:200],
            "why_it_matters": str(item.get("why_it_matters") or item.get("why") or "").strip()[:1200],
            "severity": str(item.get("severity") or "medium").strip().lower(),
            "confidence": panel._clamp_confidence(item.get("confidence")),
            "suggestion": str(item.get("suggestion") or "").strip()[:1200],
            "raised_by": [str(r)[:80] for r in (item.get("raised_by") or []) if str(r).strip()],
            "agreement": str(item.get("agreement") or "single").strip().lower(),
        }
        if entry["severity"] not in ("critical", "high", "medium", "low"):
            entry["severity"] = "medium"
        if keep_reason:
            entry["reason"] = str(item.get("reason") or "").strip()[:600]
        out.append(entry)
    return out


def deliberate(opinions: list[panel.Opinion], task_key: str, title: str,
               criteria: list[str], on_event=None, request: str = "") -> dict:
    """Combine the jurors' opinions into one verdict.

    Returns the structured decision; ``tokens_in``/``tokens_out``/``cost`` on it
    are the foreperson's own usage (zero on the fallback path)."""
    usable = [o for o in opinions if o.usable]
    abstained = [o for o in opinions if not o.usable]
    min_conf = float(settings.jury_min_confidence)

    if not usable:
        # Every juror failed. This is NOT an approval — the change is unreviewed,
        # and the pipeline has shipped an unreviewed delivery looking clean before.
        return _unreviewed(abstained)

    provider = settings.jury_synthesis_provider or settings.review_provider
    model = settings.jury_synthesis_model or settings.review_model
    user = prompts.foreperson_user(
        task_key, title, criteria, panel.render_opinions(usable), min_conf,
        abstained=", ".join(f"{o.name} ({o.persona})" for o in abstained),
        request=request)

    payload, usage = {}, {"tokens_in": 0, "tokens_out": 0, "cost": 0.0}
    if providers.can_chat(provider):
        try:
            res = llm.chat(prompts.FOREPERSON_SYSTEM, user, provider=provider, model=model,
                           json_mode=True)
            usage = {"tokens_in": res.get("tokens_in") or 0,
                     "tokens_out": res.get("tokens_out") or 0, "cost": res.get("cost") or 0.0}
            payload = panel._load_json(res.get("text") or "")
            if not payload and on_event:
                on_event("warn", "Foreperson returned unparseable output — falling back to "
                                 "mechanical synthesis")
        except Exception as exc:  # noqa: BLE001
            if on_event:
                on_event("warn", f"Foreperson ({provider} {model}) failed: {exc} — falling back "
                                 "to mechanical synthesis")
    elif on_event:
        on_event("warn", f"Foreperson provider '{provider}' is not configured — falling back "
                         "to mechanical synthesis")

    if not payload:
        out = _deterministic(usable, min_conf)
        out.update(usage)
        return out

    blocking = [e for e in _coerce_entries(payload.get("blocking"))
                if e["confidence"] >= min_conf and e["severity"] in _BLOCKING_SEVERITIES]
    verdict = str(payload.get("verdict") or "").strip().upper()
    if verdict not in ("APPROVED", "CHANGES REQUESTED"):
        verdict = "CHANGES REQUESTED" if blocking else "APPROVED"
    # The contract ties the verdict to the blocking list; a model that says
    # CHANGES REQUESTED with nothing blocking would send Dev back with nothing
    # to fix, which burns a round and produces an identical diff.
    if verdict == "CHANGES REQUESTED" and not blocking:
        verdict = "APPROVED"
    out = {
        "verdict": verdict,
        "rationale": str(payload.get("rationale") or "").strip()[:2000],
        "blocking": blocking,
        "observations": _coerce_entries(payload.get("observations")),
        "dismissed": _coerce_entries(payload.get("dismissed"), keep_reason=True),
        "synthesis": "foreperson",
        "foreperson": providers.label(provider, model),
    }
    out.update(usage)
    return out


# --- Rendering ----------------------------------------------------------------
# review_summary is prose consumed by the board, the PR body and the knowledge
# write-back, all of which predate the jury. So the panel's decision is rendered
# down to text that ends in the same VERDICT line the single reviewer produced.

def _entry_lines(e: dict, *, show_reason: bool = False) -> str:
    who = ", ".join(e.get("raised_by") or []) or "panel"
    agree = e.get("agreement") or "single"
    head = f"- **{e['title']}**"
    if e.get("location"):
        head += f" — `{e['location']}`"
    meta = f"  _{e.get('severity', 'medium')} · confidence {e.get('confidence', 0):.0%} · " \
           f"raised by {who} ({agree})_"
    lines = [head, meta]
    if e.get("why_it_matters"):
        lines.append(f"  Why it matters: {e['why_it_matters']}")
    if e.get("suggestion"):
        lines.append(f"  Suggested fix: {e['suggestion']}")
    if show_reason and e.get("reason"):
        lines.append(f"  Dismissed because: {e['reason']}")
    return "\n".join(lines)


def render(decision: dict, opinions: list[panel.Opinion]) -> str:
    """The jury's decision as the prose review_summary the rest of the app reads."""
    if decision["verdict"] == "INCONCLUSIVE":
        return f"VERDICT: INCONCLUSIVE — {decision['rationale']}"

    by_consensus = decision.get("synthesis") == "consensus"
    seated = ", ".join(f"{o.name} ({o.provider} {o.model})" for o in opinions) or "none"
    parts = [f"## Jury review — {len(opinions)} judge(s)", "",
             f"{'Jury' if by_consensus else 'Panel'}: {seated}"]
    if by_consensus:
        # The rule is part of the verdict's meaning: a reader has to know that
        # nothing arbitrated these votes, and that one "no" was enough.
        votes = decision.get("votes") or {}
        parts.append("Decided by UNANIMITY — no foreperson. "
                     + ("; ".join(f"{n}: {v.lower().replace('_', ' ')}"
                                 for n, v in votes.items()) if votes else ""))
    abstained = [o for o in opinions if not o.usable]
    if abstained:
        parts.append("**Abstained (these perspectives were NOT reviewed):** "
                     + ", ".join(f"{o.name} — {o.error or 'no opinion'}" for o in abstained))
    if decision.get("synthesis") == "deterministic":
        parts.append("**Synthesis: mechanical fallback** — the foreperson model could not be "
                     "reached; see the rationale below.")
    heading = "The jury's reasoning." if by_consensus else "Foreperson's rationale."
    parts += ["", f"**{heading}** {decision.get('rationale', '')}"]

    if decision["blocking"]:
        parts += ["", f"### Blocking ({len(decision['blocking'])})",
                  "\n".join(_entry_lines(e) for e in decision["blocking"])]
    if decision["observations"]:
        parts += ["", f"### Observations — not blocking ({len(decision['observations'])})",
                  "\n".join(_entry_lines(e) for e in decision["observations"])]
    if decision["dismissed"]:
        dropped_by = "below the confidence floor" if by_consensus else "by the foreperson"
        parts += ["", f"### Dismissed {dropped_by} ({len(decision['dismissed'])})",
                  "\n".join(_entry_lines(e, show_reason=True) for e in decision["dismissed"])]

    # Per-juror summaries last: the decision is what matters, the reasoning is
    # reference material for whoever wants to audit the panel.
    op_lines = [f"- **{o.name}** ({o.verdict.lower().replace('_', ' ')}, "
                f"{len(o.findings)} finding(s)): {o.summary or '—'}"
                for o in opinions if o.usable]
    if op_lines:
        parts += ["", "### Individual opinions", "\n".join(op_lines)]

    parts += ["", f"VERDICT: {decision['verdict']}"]
    return "\n".join(parts)
