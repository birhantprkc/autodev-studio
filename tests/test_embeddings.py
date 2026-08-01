"""Local dense-embedding channel: availability gating, memory guards, and the
weighted RRF fusion that merges it with the graph's BM25.

These run without the [semantic] extra installed and without touching a model —
the fusion maths and the fail-open paths are what must never regress.
"""

import contextlib

import pytest
from app.config import settings
from app.services.knowledge import embed, retriever


@pytest.fixture(autouse=True)
def _restore():
    saved = (settings.local_embeddings, settings.rrf_k, settings.rrf_dense_weight,
             settings.embedding_model)
    yield
    (settings.local_embeddings, settings.rrf_k, settings.rrf_dense_weight,
     settings.embedding_model) = saved


# --- availability + fail-open -------------------------------------------------

def test_disabled_means_unavailable():
    settings.local_embeddings = False
    assert embed.enabled() is False
    assert embed.available() is False


def test_search_returns_empty_when_disabled():
    settings.local_embeddings = False
    assert embed.search("https://github.com/x/y", "anything") == []


def test_build_returns_zero_when_disabled():
    settings.local_embeddings = False
    assert embed.build("https://github.com/x/y") == 0


def test_free_mb_reports_a_number():
    mb = embed.free_mb()
    assert mb == -1 or mb > 0


def test_ram_guard_blocks_when_tight(monkeypatch):
    """Under memory pressure the channel declines rather than inviting the OOM
    killer — retrieval degrades to BM25, it does not crash."""
    monkeypatch.setattr(embed, "free_mb", lambda: 100)
    assert embed._enough_ram() is False
    monkeypatch.setattr(embed, "free_mb", lambda: 4000)
    assert embed._enough_ram() is True


def test_threads_scale_with_available_ram(monkeypatch):
    """A roomy machine is not throttled to a dev-box budget."""
    monkeypatch.setattr(embed, "free_mb", lambda: 5000)
    assert embed._threads() == 4
    monkeypatch.setattr(embed, "free_mb", lambda: 2500)
    assert embed._threads() == 2
    monkeypatch.setattr(embed, "free_mb", lambda: 1200)
    assert embed._threads() == 1


def test_doc_text_is_capped_and_prefixed():
    """The nomic task prefix is required, and the cap is what keeps the ONNX
    arena flat (measured: 2KB inputs → 1.2GB and climbing; 700B → 459MB flat)."""
    node = {"label": "Class", "name": "Table", "sig": "(rows, cols)",
            "file": "rich/table.py"}
    text = embed._doc_text(node, "x" * 5000)
    assert text.startswith("search_document: rich/table.py")
    assert "Class Table(rows, cols)" in text
    assert len(text) <= embed._DOC_CHARS


def test_point_id_is_stable_and_unique():
    a = embed._point_id("pkg.mod.Klass.method")
    assert a == embed._point_id("pkg.mod.Klass.method")
    assert a != embed._point_id("pkg.mod.Klass.other")


# --- weighted RRF fusion ------------------------------------------------------

def _hit(name, file, line=1):
    return {"name": name, "file_path": file, "start_line": line,
            "qualified_name": f"{file}::{name}", "label": "Function"}


class TestWeightedRRF:
    def test_weight_lets_dense_outrank_confident_bm25(self):
        """The measured failure mode: BM25's wrong-but-confident top hits crowd
        out dense's correct lower-ranked ones under equal weights."""
        bm25 = [_hit("wrong_a", "a.py"), _hit("wrong_b", "b.py"),
                _hit("wrong_c", "c.py"), _hit("wrong_d", "d.py")]
        dense = [_hit("wrong_a", "a.py")] * 0 + [
            _hit("x1", "x.py"), _hit("x2", "x.py"), _hit("x3", "x.py"),
            _hit("right", "target.py")]

        equal = retriever._rrf([(bm25, 1.0), (dense, 1.0)], 60)
        weighted = retriever._rrf([(bm25, 1.0), (dense, 2.0)], 60)

        def rank_of(scores, key):
            return sorted(scores, key=lambda k: -scores[k]).index(key)

        key = "target.py::right"
        assert rank_of(weighted, key) < rank_of(equal, key)

    def test_zero_weight_channel_is_ignored(self):
        bm25 = [_hit("only", "a.py")]
        scores = retriever._rrf([(bm25, 1.0), ([_hit("d", "d.py")], 0.0)], 60)
        assert "d.py::d" not in scores
        assert "a.py::only" in scores

    def test_agreement_between_channels_boosts(self):
        """A node both rankers like should beat one only a single ranker likes."""
        shared = _hit("shared", "s.py")
        bm25 = [shared, _hit("bm_only", "b.py")]
        dense = [shared, _hit("dn_only", "d.py")]
        scores = retriever._rrf([(bm25, 1.0), (dense, 2.0)], 60)
        assert scores["s.py::shared"] > scores["b.py::bm_only"]
        assert scores["s.py::shared"] > scores["d.py::dn_only"]

    def test_builtin_and_null_nodes_are_dropped(self):
        junk = [{"name": "print", "file_path": "<python-builtins>",
                 "qualified_name": "builtins.print"}]
        scores = retriever._rrf([(junk, 1.0)], 60)
        assert scores == {}


class TestDownloadProgressIsContained:
    """fastembed downloads the model with a bare tqdm and hardcodes
    show_progress=True. tqdm redraws with '\\r', which collapses to one line only
    on a TTY — under the CLI's Rich spinner every 1KB chunk became a NEW line, so
    the first run printed thousands of 'Downloading bytes: ####' rows and buried
    the interface."""

    def test_direct_stream_writes_are_captured(self):
        """The mechanism, without depending on the optional stack: anything a
        library writes straight to stdout/stderr must land in the buffer."""
        import sys

        from app.services.knowledge import embed

        with embed._quiet_progress() as buf:
            print("a stray library print")
            sys.stderr.write("\rDownloading bytes: ####  12.3MB\r")
        assert "a stray library print" in buf.getvalue()
        assert "Downloading bytes" in buf.getvalue()
        # Streams are restored even though the library wrote to them.
        assert sys.stdout is not buf and sys.stderr is not buf

    def test_a_real_tqdm_bar_never_reaches_the_terminal(self):
        """The faithful version. tqdm arrives with fastembed, so it is absent on
        a core-only install (which is what CI runs) — skip rather than pretend."""
        import sys

        tqdm = pytest.importorskip("tqdm", reason="tqdm ships with the [semantic] extra")
        from app.services.knowledge import embed

        with embed._quiet_progress() as buf:
            for _ in tqdm.tqdm(range(5), total=5):
                pass
        assert buf.getvalue(), "the bar must be captured, not merely suppressed"
        assert sys.stdout is not buf and sys.stderr is not buf

    def test_streams_are_restored_when_the_load_raises(self):
        import sys

        from app.services.knowledge import embed

        out, err = sys.stdout, sys.stderr
        with contextlib.suppress(RuntimeError), embed._quiet_progress():
            raise RuntimeError("download failed")
        assert sys.stdout is out and sys.stderr is err

    def test_logging_still_reaches_the_real_stream(self):
        """logging.StreamHandler binds sys.stderr at construction, so swapping it
        afterwards must not swallow the pipeline's own log output."""
        import logging

        from app.services.knowledge import embed

        records = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record.getMessage())

        log = logging.getLogger("embed-test")
        log.addHandler(_Capture())
        log.setLevel(logging.INFO)
        try:
            with embed._quiet_progress():
                log.info("still logged")
        finally:
            log.handlers.clear()
        assert records == ["still logged"]
