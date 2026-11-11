"""Provider egress gate (S-2/S-3/S-4): what a provider request carries after local literal redaction.

Every fixture is synthetic. The stub providers record the exact payload they were handed.
"""

from __future__ import annotations

import base64
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.core.provider_egress import WITHHELD
from memorymaster.core.service import MemoryService
from memorymaster.dreaming.ledger import DreamLedger
from memorymaster.dreaming.models import CaptureEnvelope, DreamMessage, ExtractionResult, ProviderUsage
from memorymaster.dreaming.worker import DreamConfig, DreamWorker
from test_dreaming_worker import _Consolidator

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
BS = chr(92)

_PW = "Tr0ub4dor77" + "Zx93Qv"
B64 = base64.b64encode(("password=" + _PW).encode()).decode()
IP = "10.177.30.40"
WIN_PATH = "C:" + BS + "Users" + BS + "jdoe" + BS + "notes" + BS + "plan.txt"
MAIL = "maria.gonzalez77" + "@" + "clientecorp.com.ar"
TOKEN = "sk-" + "ant-api03-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"
BLOCKED = "DB_PASS=" + "hunter2hunter2"  # credential-grade, not substitutable in place
BENIGN = "The service retries three times before it gives up."
NEEDLES = (B64, IP, WIN_PATH, MAIL, TOKEN, _PW, "jdoe")


def _assert_clean(payload: str) -> None:
    for needle in NEEDLES:
        assert needle not in payload, needle


# ---- S-3: dreaming ---------------------------------------------------------------------------


class _Recorder:
    model = "stub"
    provider = "stub"

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    def extract(self, messages, *, scope, capture_hash):
        self.calls.append([dict(m) for m in messages])
        return ExtractionResult((), ProviderUsage("stub", "stub", 10, 1, 1, 1, True))


def _queue(ledger: DreamLedger, texts: list[str], *, session_hash: str = "s1") -> int:
    # Stored with the OLD literal sanitizer: the ledger keeps whatever is in these texts.
    stamp = (NOW - timedelta(hours=1)).isoformat()
    messages = tuple(
        DreamMessage(f"m{i}", "user" if i % 2 == 0 else "assistant", text, stamp) for i, text in enumerate(texts)
    )
    return ledger.enqueue(
        CaptureEnvelope(
            provider="codex",
            session_hash=session_hash,
            scope="project:test",
            captured_at=NOW.isoformat(),
            last_activity_at=stamp,
            messages=messages,
            cursor_start=0,
            cursor_end=100,
            content_hash="h-" + session_hash,
        )
    )


def _run(tmp_path: Path, texts: list[str]):
    service = MemoryService(tmp_path / "claims.db", workspace_root=tmp_path)
    service.init_db()
    ledger = DreamLedger(tmp_path / "capture.db")
    capture_id = _queue(ledger, texts)
    extractor = _Recorder()
    summary = DreamWorker(ledger, service, extractor, _Consolidator(), config=DreamConfig(), now=lambda: NOW).run(
        apply_candidates=False
    )
    return summary, extractor, ledger, capture_id


def test_dreaming_payload_redacts_encoded_secrets_ips_paths_and_emails(tmp_path):
    texts = [
        "host is " + IP + " and the file is " + WIN_PATH,
        "the client is " + MAIL + " and the token is " + TOKEN,
        BENIGN,
    ]
    summary, extractor, ledger, capture_id = _run(tmp_path, texts)
    assert summary["errors"] == 0
    sent = json.dumps(extractor.calls, ensure_ascii=False)
    _assert_clean(sent)
    assert "[REDACTED:private_ipv4]" in sent  # redacted, not blocked: queued captures with an IP keep flowing
    assert BENIGN in sent
    assert ledger.get_capture(capture_id)["state"] != "retryable"


def test_dreaming_blocked_message_is_withheld_individually_and_the_capture_completes(tmp_path):
    summary, extractor, ledger, capture_id = _run(tmp_path, ["note: " + BLOCKED + " ok", BENIGN])
    assert summary["errors"] == 0
    assert summary["extracted"] == 1
    assert summary["egress_withheld"] == 1
    assert len(extractor.calls) == 1
    texts = {m["message_id"]: m["text"] for m in extractor.calls[0]}
    assert texts["m0"] == WITHHELD
    assert texts["m1"] == BENIGN
    assert "hunter2" not in json.dumps(extractor.calls)
    assert ledger.get_capture(capture_id)["state"] not in {"retryable", "quarantined"}


def test_dreaming_capture_with_only_blocked_messages_completes_without_a_provider_call(tmp_path):
    summary, extractor, ledger, capture_id = _run(tmp_path, [BLOCKED, "also " + BLOCKED])
    assert summary["errors"] == 0
    assert extractor.calls == []
    assert summary["egress_withheld"] == 2
    row = ledger.get_capture(capture_id)
    assert row["state"] not in {"retryable", "quarantined"}


# ---- S-4: PreCompact residual ----------------------------------------------------------------


def _residual(excerpt: str, stats: dict | None = None):
    from memorymaster.capture import precompact_residual as pr

    seen: list[tuple[str, str]] = []

    def llm(prompt: str, text: str) -> str:
        seen.append((prompt, text))
        return json.dumps({"decisions": [], "rejected": [], "assumptions": [], "cards": [], "next_step": ""})

    pr.extract_residual(excerpt, llm=llm, config={"provider": "openai", "model": "", "forced": False}, stats=stats)
    return seen


def test_precompact_excerpt_redacts_encoded_secrets_ips_paths_and_emails():
    excerpt = "\n".join(
        [
            "ASSISTANT: host " + IP + " file " + WIN_PATH,
            "USER: mail " + MAIL + " token " + TOKEN,
            "ASSISTANT: " + BENIGN,
        ]
    )
    seen = _residual(excerpt)
    assert len(seen) == 1
    _assert_clean(seen[0][1])
    assert BENIGN in seen[0][1]
    assert "[REDACTED:private_ipv4]" in seen[0][1]


def test_precompact_blocked_line_is_withheld_and_the_rest_still_goes_out():
    stats: dict = {}
    seen = _residual("USER: " + BLOCKED + "\nASSISTANT: " + BENIGN, stats)
    assert len(seen) == 1
    assert "hunter2" not in seen[0][1]
    assert BENIGN in seen[0][1]
    assert stats["egress_withheld"] == 1


# ---- S-2: session-end distillation (both paths) ----------------------------------------------


def _script_module():
    spec = importlib.util.spec_from_file_location(
        "agent_session_end_ingest_t", ROOT / "scripts" / "agent_session_end_ingest.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _packaged_module():
    import memorymaster.surfaces.session_end_ingest as mod

    return mod


def _transcript(tmp_path: Path, assistant_texts: list[str]) -> Path:
    lines = [{"type": "user", "message": {"role": "user", "content": "please note this down for later"}}]
    lines += [
        {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": t}]}}
        for t in assistant_texts
    ]
    path = tmp_path / "t.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


@pytest.mark.parametrize("loader", [_script_module, _packaged_module], ids=["script", "package"])
def test_session_end_payload_is_gated_and_a_blocked_message_does_not_take_the_batch_down(tmp_path, monkeypatch, loader):
    sent: list[str] = []
    monkeypatch.setattr("memorymaster.core.llm_provider.call_llm", lambda prompt, text: sent.append(text) or "[]")
    mod = loader()
    transcript = _transcript(
        tmp_path,
        [
            "Session notes: host " + IP + " in " + WIN_PATH + " for " + MAIL + " " + TOKEN,
            "Session notes: " + B64 + " and " + BLOCKED + " must never leave this machine, really",
            "Session notes: " + BENIGN + " Nothing else to add here.",
        ],
    )
    db = tmp_path / "unused.db"
    db.touch()  # the package path checks existence before ingesting its (empty) answer
    mod.run(str(db), str(transcript), source_agent="t-agent", cwd=str(tmp_path))
    assert len(sent) == 1
    _assert_clean(sent[0])
    assert "hunter2" not in sent[0]
    assert BENIGN in sent[0]
    assert "[REDACTED:private_ipv4]" in sent[0]


def test_a_base64_wrapped_credential_never_reaches_the_provider(tmp_path):
    summary, extractor, _ledger, _cid = _run(tmp_path, ["remember the config " + B64 + " for the service", BENIGN])
    assert summary["errors"] == 0
    assert B64 not in json.dumps(extractor.calls)
    assert summary["egress_withheld"] == 1
    seen = _residual("USER: use " + B64 + " please\nASSISTANT: " + BENIGN)
    assert B64 not in seen[0][1] and BENIGN in seen[0][1]


def _stored(text: str) -> str:
    from memorymaster.core.provider_egress import export_messages

    return export_messages([{"message_id": "m", "text": text}])[0][0]["text"]


def test_stored_capture_markers_do_not_block_but_foreign_spliced_and_credentials_do():
    assert (
        _stored("host [REDACTED:private_ipv4] answered, " + BENIGN)
        == "host [REDACTED:private_ipv4] answered, " + BENIGN
    )
    assert (
        _stored("the key is [REDACTED:secret], (see [REDACTED:email]).")
        == "the key is [REDACTED:secret], (see [REDACTED:email])."
    )
    assert _stored("[REDACTED:private_ipv4] and " + BLOCKED) == WITHHELD
    assert _stored("a [redacted] value and a [REDACTED: broken marker") == WITHHELD
    # a marker spliced into a token leaves its head and tail visible: it must block
    assert _stored("sk-ant-api03-A1b2C3d4[REDACTED:x]E5f6G7h8I9j0K1l2M3n4O5p6") == WITHHELD
    assert _stored("AKIA[REDACTED:x]IOSFODNN7EXAMPLE") == WITHHELD
    # the helper's own placeholder shape in the input is data, never rewritten
    assert _stored("x [MARK0x] y [REDACTED:ab]") == "x [MARK0x] y [REDACTED:ab]"


def test_raw_transcript_text_keeps_the_original_redacted_refusal():
    from memorymaster.core.provider_egress import export_text

    assert export_text("host [REDACTED:private_ipv4] answered") is None
    assert export_text("sk-ant-api03-A1b2C3d4[REDACTED:x]E5f6G7h8I9j0K1l2M3n4O5p6") is None
    assert export_text("AKIA[REDACTED:x]IOSFODNN7EXAMPLE") is None
    assert export_text(BENIGN) == BENIGN


def test_a_huge_unbroken_token_is_replaced_fast_and_a_long_prose_message_still_goes_out():
    import time

    from memorymaster.core.provider_egress import LONG_TOKEN, export_messages, export_text

    blob = "A1" * 10_000
    started = time.monotonic()
    assert export_text(blob) == LONG_TOKEN
    prose = "The service retries three times. " * 2_000  # ~66k chars
    safe, withheld = export_messages([{"message_id": "a", "text": blob}, {"message_id": "b", "text": prose}])
    assert time.monotonic() - started < 3
    assert withheld == 0 and safe[0]["text"] == LONG_TOKEN and safe[1]["text"] == prose


@pytest.mark.parametrize("loader", [_script_module, _packaged_module], ids=["script", "package"])
def test_session_end_huge_blob_message_is_withheld_quickly_and_the_rest_goes_out(tmp_path, monkeypatch, loader):
    import time

    sent: list[str] = []
    monkeypatch.setattr("memorymaster.core.llm_provider.call_llm", lambda prompt, text: sent.append(text) or "[]")
    mod = loader()
    transcript = _transcript(
        tmp_path, ["Session notes: " + "Zq" * 10_000, "Session notes: " + BENIGN + " Nothing else."]
    )
    db = tmp_path / "unused.db"
    db.touch()
    started = time.monotonic()
    mod.run(str(db), str(transcript), source_agent="t-agent", cwd=str(tmp_path))
    assert time.monotonic() - started < 5
    assert len(sent) == 1 and BENIGN in sent[0] and "ZqZq" not in sent[0] and "long_token" in sent[0]


def test_a_gate_exception_withholds_that_message_only(tmp_path, monkeypatch):
    import memorymaster.core.provider_egress as gate

    real = gate.prepare_transcript_egress

    def flaky(text, **kwargs):
        if "poison" in text:
            raise ValueError("synthetic gate fault")
        return real(text, **kwargs)

    monkeypatch.setattr(gate, "prepare_transcript_egress", flaky)
    summary, extractor, ledger, capture_id = _run(tmp_path, ["poison pill text", BENIGN])
    assert summary["errors"] == 0 and summary["egress_withheld"] == 1
    texts = {m["message_id"]: m["text"] for m in extractor.calls[0]}
    assert texts == {"m0": gate.GATE_ERROR, "m1": BENIGN}
    assert ledger.get_capture(capture_id)["state"] not in {"retryable", "quarantined"}
    assert gate.export_text("poison") is not None  # raw path uses another entry point
    monkeypatch.setattr(gate, "prepare_claim_egress", lambda text: (_ for _ in ()).throw(ValueError("x")))
    assert gate.export_text("anything") is None  # a fault withholds, never raises


def test_precompact_excerpt_with_only_withheld_lines_makes_no_provider_call():
    stats: dict = {}
    assert _residual("USER: " + BLOCKED + "\nASSISTANT: " + BLOCKED, stats) == []
    assert stats["egress_withheld"] == 2


def test_a_failed_extraction_does_not_count_withheld_messages(tmp_path):
    class Broken(_Recorder):
        def extract(self, messages, *, scope, capture_hash):
            raise RuntimeError("provider unavailable")

    service = MemoryService(tmp_path / "claims.db", workspace_root=tmp_path)
    service.init_db()
    ledger = DreamLedger(tmp_path / "capture.db")
    _queue(ledger, [BLOCKED, BENIGN])
    summary = DreamWorker(ledger, service, Broken(), _Consolidator(), config=DreamConfig(), now=lambda: NOW).run(
        apply_candidates=False
    )
    assert summary["errors"] == 1 and summary["egress_withheld"] == 0


def test_a_quote_spanning_a_redacted_ip_binds_to_the_text_the_provider_saw(tmp_path):
    from memorymaster.dreaming.models import DreamCandidate
    from memorymaster.dreaming.source_review import bind_source

    ledger = DreamLedger(tmp_path / "capture.db")
    cid = _queue(ledger, ["the primary database lives at " + IP + " in the lab", BENIGN])
    row = ledger.get_capture(cid)

    def candidate(quote: str) -> DreamCandidate:
        return DreamCandidate(
            "c1", "The primary database lives in the lab.", "fact", "db", "lives_in", "lab", "project", "m0", quote, 0.8
        )

    spanning = bind_source(candidate("database lives at [REDACTED:private_ipv4] in"), row)
    assert spanning.source_context["exact_quote"] is True
    assert bind_source(candidate("database lives at " + IP + " in"), row).source_context["exact_quote"] is True
    assert bind_source(candidate("never said this sentence anywhere"), row).source_context["exact_quote"] is False
    assert row["messages"][0]["text"].count(IP) == 1  # what is persisted is untouched
