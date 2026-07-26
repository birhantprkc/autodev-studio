"""The verdict-parsing helpers that drive the revise loop.

These decide whether the pipeline loops back to Dev, so their exact semantics
matter: CONCERNS must not trigger a revision, an errored-with-no-text agent call
is INCONCLUSIVE (not a pass), and a lost Claude CLI must be detected so the
pipeline can fall back.
"""

from app.services import orchestrator as orch


class TestQaFailed:
    def test_explicit_fail_triggers_revision(self):
        assert orch._qa_failed("VERDICT: FAIL — tests do not pass")

    def test_pass_does_not(self):
        assert not orch._qa_failed("VERDICT: PASS")

    def test_concerns_does_not_trigger(self):
        # CONCERNS is deliberately weaker than FAIL — it must not loop.
        assert not orch._qa_failed("VERDICT: CONCERNS — minor style nits")

    def test_fail_must_be_near_the_verdict(self):
        # The word "fail" elsewhere in prose must not be read as a FAIL verdict.
        assert not orch._qa_failed(
            "The feature prevents the login from failing. VERDICT: PASS"
        )

    def test_empty(self):
        assert not orch._qa_failed("")
        assert not orch._qa_failed(None)


class TestReviewChangesRequested:
    def test_detected_case_insensitive(self):
        assert orch._review_changes_requested("changes requested: fix the null check")
        assert orch._review_changes_requested("CHANGES REQUESTED")

    def test_approved_is_not_a_change_request(self):
        assert not orch._review_changes_requested("APPROVED — ships as-is")

    def test_empty(self):
        assert not orch._review_changes_requested("")


class TestInconclusive:
    def test_error_with_no_text_is_inconclusive(self):
        assert orch._inconclusive({"error": "timeout", "text": ""})

    def test_error_but_has_verdict_is_not_inconclusive(self):
        assert not orch._inconclusive({"error": "timeout", "text": "VERDICT: PASS"})

    def test_clean_result_is_not_inconclusive(self):
        assert not orch._inconclusive({"text": "VERDICT: APPROVED"})


class TestBackendUnavailable:
    def test_missing_backend_is_unavailable(self, monkeypatch):
        # Backend can't run on this machine → unavailable regardless of error text.
        monkeypatch.setattr(orch.agent_backends, "is_available", lambda _b: False)
        assert orch._backend_unavailable("claude-code", "anything")

    def test_auth_error_with_backend_present_is_unavailable(self, monkeypatch):
        monkeypatch.setattr(orch.agent_backends, "is_available", lambda _b: True)
        assert orch._backend_unavailable("claude-code", "Invalid API key — please run login")

    def test_ordinary_error_with_backend_present_is_not_unavailable(self, monkeypatch):
        # Backend runs and the error is a real code problem → keep the strong model.
        monkeypatch.setattr(orch.agent_backends, "is_available", lambda _b: True)
        assert not orch._backend_unavailable("claude-code", "syntax error in generated patch")


class TestIsTransient:
    def test_rate_limit_and_overload_are_transient(self):
        assert orch._is_transient("HTTP 429 rate limit exceeded")
        assert orch._is_transient("Error 529: overloaded")

    def test_code_error_is_not_transient(self):
        assert not orch._is_transient("patch does not apply")
