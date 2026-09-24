"""F-03: the compiled profile can be supported by governed claims.

Review F-03: verbatim capture has been opt-in since 2026-07-13, so
``verbatim_memories`` stopped growing on 2026-08-24 and the profile compiler
(which read only verbatim rows) froze; SessionStart kept injecting it as current
and the operational review reported PASS.  When no new verbatim input exists,
the engine now compiles from confirmed claims and Dreaming-applied claims, with
exact support manifests (claim ids stored negated) and the same independent
session gate; the operational review WARNs when the newest active-fact support
is older than 7 days.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.operations import operational_review as review
from memorymaster.profile.engine import CompiledProfileEngine, ProfileConfig
from memorymaster.profile.models import ProfileCandidate, ProfileDecision, ProfileMessage
from memorymaster.profile.repository import ProfileRepository
from memorymaster.recall.verbatim_store import ensure_verbatim_schema, store_verbatim


class _Mapper:
    model = "fixture-map"

    def __init__(self) -> None:
        self.calls: list[tuple[int, ...]] = []
        self.texts: list[str] = []

    def map(self, messages: tuple[ProfileMessage, ...]) -> tuple[ProfileCandidate, ...]:
        self.calls.append(tuple(item.message_id for item in messages))
        self.texts.extend(item.text for item in messages)
        return tuple(
            ProfileCandidate(f"c{abs(item.message_id)}", "identity_locale", "location",
                             "Argentina", "stable", (item.message_id,))
            for item in messages if "Argentina" in item.text
        )


class _Reducer:
    model = "fixture-reduce"

    def __init__(self, before_reduce=None) -> None:
        self.before_reduce = before_reduce

    def reduce(self, candidates, facts) -> tuple[ProfileDecision, ...]:
        del facts
        if self.before_reduce is not None:
            self.before_reduce()
        return (ProfileDecision(tuple(c.candidate_id for c in candidates), "add",
                                "identity_locale", "location", "Argentina", "stable",
                                confidence=0.9, rationale="independent claims"),)


def _service(tmp_path: Path) -> tuple[MemoryService, Path]:
    db = tmp_path / "profile.db"
    service = MemoryService(db, workspace_root=tmp_path)
    service.init_db()
    ensure_verbatim_schema(str(db))
    return service, db


def _claim(service: MemoryService, text: str, *, status: str = "confirmed",
           source_agent: str = "claude-session", visibility: str = "public",
           citation: CitationInput | None = None, tenant_id: str | None = None) -> int:
    claim = service.store.create_claim(
        text=text, citations=[citation or CitationInput(source="session", locator=f"s-{text[:12]}")],
        confidence=0.7, source_agent=source_agent, visibility=visibility, scope="user",
        tenant_id=tenant_id,
    )
    with service.store.connect() as conn:
        conn.execute("UPDATE claims SET status=? WHERE id=?", (status, claim.id))
        conn.commit()
    return claim.id


def _engine(db: Path, mapper, reducer, tmp_path: Path) -> CompiledProfileEngine:
    return CompiledProfileEngine(ProfileRepository(db), mapper, reducer,
                                 output_dir=tmp_path / "projection",
                                 config=ProfileConfig(max_map_calls=3))


def _run_source(db: Path) -> list[str]:
    with sqlite3.connect(db) as conn:
        return [row[0] for row in conn.execute("SELECT source FROM compiled_profile_runs ORDER BY id")]


def test_empty_verbatim_compiles_from_confirmed_and_dreaming_claims(tmp_path):
    service, db = _service(tmp_path)
    confirmed = _claim(service, "The operator is based in Argentina.",
                       citation=CitationInput(source="session", locator="C:/x/sess-1.jsonl"))
    dreamed = _claim(service, "Operator lives in Argentina, per the conversation.",
                     status="candidate", source_agent="dream-worker",
                     citation=CitationInput(source="dream-worker", locator="dream:google:abcdef123456:m7"))
    blocked = [
        _claim(service, "Argentina private note", visibility="private"),
        _claim(service, "Argentina unreviewed candidate", status="candidate"),
        _claim(service, "Argentina key AKIAIOSFODNN7EXAMPLE"),
        _claim(service, "Argentina retired claim", status="superseded"),
    ]
    mapper = _Mapper()

    result = _engine(db, mapper, _Reducer(), tmp_path).run(force=True)

    assert result["status"] == "completed", result
    assert _run_source(db) == ["claims"]
    seen = {message_id for call in mapper.calls for message_id in call}
    assert seen == {-confirmed, -dreamed}
    assert not seen & {-claim_id for claim_id in blocked}
    facts = ProfileRepository(db).active_facts()
    assert len(facts) == 1
    assert facts[0].support_ids == tuple(sorted((-confirmed, -dreamed)))
    assert facts[0].independent_sessions == 2
    manifest = json.loads((tmp_path / "projection" / "user-profile.json").read_text(encoding="utf-8"))
    assert manifest["facts"][0]["support_ids"] == sorted([-confirmed, -dreamed])

    check = review.check_compiled_profile(review.ReviewConfig(db=db))
    assert check.counts["mismatches"] == 0
    assert check.verdict is review.Verdict.PASS


def test_new_verbatim_input_takes_precedence_over_claims(tmp_path):
    service, db = _service(tmp_path)
    _claim(service, "The operator is based in Argentina.")
    first = store_verbatim(str(db), "s1", "user", "I am based in Argentina.", "project:x", "test",
                           timestamp="2026-09-01T12:00:00+00:00")
    mapper = _Mapper()

    _engine(db, mapper, _Reducer(), tmp_path).run(force=True)

    assert _run_source(db) == ["verbatim"]
    assert mapper.calls == [(first,)]


def test_claims_watermark_is_incremental_and_independent(tmp_path):
    service, db = _service(tmp_path)
    _claim(service, "The operator is based in Argentina.")
    _claim(service, "Operator lives in Argentina.")
    _engine(db, _Mapper(), _Reducer(), tmp_path).run(force=True)

    idle = _engine(db, _Mapper(), _Reducer(), tmp_path).run(force=True)
    assert idle["status"] == "no_changes"

    newer = _claim(service, "Argentina remains the operator's base.")
    mapper = _Mapper()
    _engine(db, mapper, _Reducer(), tmp_path).run(force=True)
    assert mapper.calls == [(-newer,)]
    assert _run_source(db) == ["claims", "claims"]


def _mapped(*mappers: _Mapper) -> list[int]:
    return sorted(message_id for mapper in mappers for call in mapper.calls for message_id in call)


@pytest.mark.parametrize("initial_status", ["candidate", "stale", "conflicted"])
def test_a_claim_that_becomes_eligible_after_a_higher_id_was_compiled_is_mapped(tmp_path, initial_status):
    # Verifier repro (f03_watermark.py): a MAX(id) watermark skipped a claim for
    # good once a higher-id eligible claim (a Dreaming candidate) was compiled.
    # The inflows that hit it: steward confirmation of an agent candidate, S1
    # re-confirming a stale claim, and a conflict resolved to confirmed.
    service, db = _service(tmp_path)
    late = _claim(service, "Operator is based in Argentina (session A).", status=initial_status,
                  citation=CitationInput(source="session", locator="sess-A"))
    dreamed = _claim(service, "Operator lives in Argentina, dreaming.", status="candidate",
                     source_agent="dream-worker",
                     citation=CitationInput(source="dream-worker", locator="dream:google:abc123:m1"))
    first = _Mapper()
    assert _engine(db, first, _Reducer(), tmp_path).run(force=True)["status"] == "completed"
    assert _mapped(first) == [-dreamed]

    with service.store.connect() as conn:
        conn.execute("UPDATE claims SET status='confirmed' WHERE id=?", (late,))
        conn.commit()
    second = _Mapper()
    result = _engine(db, second, _Reducer(), tmp_path).run(force=True)

    assert result["status"] == "completed", result
    assert _mapped(second) == [-late]
    assert _run_source(db) == ["claims", "claims"]


def test_a_mapped_claim_is_not_mapped_again_when_it_is_later_confirmed(tmp_path):
    service, db = _service(tmp_path)
    dreamed = _claim(service, "Operator lives in Argentina, dreaming.", status="candidate",
                     source_agent="dream-worker",
                     citation=CitationInput(source="dream-worker", locator="dream:google:abc123:m1"))
    first = _Mapper()
    _engine(db, first, _Reducer(), tmp_path).run(force=True)
    with service.store.connect() as conn:
        conn.execute("UPDATE claims SET status='confirmed' WHERE id=?", (dreamed,))
        conn.commit()

    second = _Mapper()
    result = _engine(db, second, _Reducer(), tmp_path).run(force=True)

    assert _mapped(first) == [-dreamed]
    assert result["status"] == "no_changes"
    assert second.calls == []


def test_an_unusable_eligible_claim_does_not_start_empty_runs(tmp_path):
    service, db = _service(tmp_path)
    _claim(service, "Argentina key AKIAIOSFODNN7EXAMPLE")
    mapper = _Mapper()

    result = _engine(db, mapper, _Reducer(), tmp_path).run(force=True)

    assert result["status"] == "no_changes"
    assert mapper.calls == []
    assert _run_source(db) == []


def test_a_resumed_claims_run_maps_every_pending_claim_exactly_once(tmp_path):
    service, db = _service(tmp_path)
    ids = [_claim(service, f"The operator is based in Argentina, note {n}.") for n in range(3)]
    engine = CompiledProfileEngine(ProfileRepository(db), mapper := _Mapper(), _Reducer(),
                                   output_dir=tmp_path / "projection",
                                   config=ProfileConfig(max_map_calls=1, max_messages=1))

    statuses = [engine.run(force=True)["status"] for _ in range(4)]

    assert statuses[:2] == ["mapping", "mapping"]
    assert statuses[2] == "completed"
    assert statuses[3] == "no_changes"
    assert _mapped(mapper) == sorted(-claim_id for claim_id in ids)


def test_a_claim_support_that_loses_eligibility_is_rejected(tmp_path):
    service, db = _service(tmp_path)
    kept = _claim(service, "The operator is based in Argentina.")
    retired = _claim(service, "Operator lives in Argentina.")

    def retire():
        with service.store.connect() as conn:
            conn.execute("UPDATE claims SET status='archived' WHERE id=?", (retired,))
            conn.commit()

    result = _engine(db, _Mapper(), _Reducer(before_reduce=retire), tmp_path).run(force=True)

    assert result["rejected"] == 1
    assert ProfileRepository(db).active_facts() == ()
    assert kept


def _profile_db(path: Path, supported_at: str, *, mismatch: bool = False) -> Path:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE compiled_profile_runs(status TEXT);
        INSERT INTO compiled_profile_runs VALUES ('completed');
        CREATE TABLE compiled_profile_facts(
            id INTEGER PRIMARY KEY, status TEXT, support_count INTEGER, independent_sessions INTEGER
        );
        CREATE TABLE compiled_profile_supports(fact_id INTEGER, session_id TEXT, supported_at TEXT);
    """)
    conn.execute("INSERT INTO compiled_profile_facts VALUES (1, 'active', ?, 2)", (3 if mismatch else 2,))
    conn.executemany("INSERT INTO compiled_profile_supports VALUES (1, ?, ?)",
                     [("a", "2026-01-01T00:00:00+00:00"), ("b", supported_at)])
    conn.commit()
    conn.close()
    return path


def test_review_warns_when_the_newest_support_is_older_than_seven_days(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORYMASTER_COMPILED_PROFILE", raising=False)
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    result = review.check_compiled_profile(review.ReviewConfig(db=_profile_db(tmp_path / "old.db", old)))

    assert result.verdict is review.Verdict.WARN
    assert result.counts["newest_support_age_days"] == 30
    assert "30d" in result.detail


def test_review_passes_fresh_support_and_mismatch_still_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMORYMASTER_COMPILED_PROFILE", raising=False)
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    ok = review.check_compiled_profile(review.ReviewConfig(db=_profile_db(tmp_path / "fresh.db", fresh)))
    assert ok.verdict is review.Verdict.PASS
    assert ok.counts["newest_support_age_days"] == 1

    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    bad = review.check_compiled_profile(
        review.ReviewConfig(db=_profile_db(tmp_path / "bad.db", old, mismatch=True)))
    assert bad.verdict is review.Verdict.FAIL


def test_run_source_migration_defaults_existing_runs_and_is_idempotent(tmp_path):
    import importlib

    migration = importlib.import_module("memorymaster.stores.migrations.0026_compiled_profile_run_source")
    _, db = _service(tmp_path)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO compiled_profile_runs(status, map_model, reduce_model, started_at, updated_at) "
            "VALUES ('completed', 'm', 'r', '2026-08-01', '2026-08-01')"
        )
        migration.apply_sqlite(conn)  # already applied by init_db: must be a no-op
        assert [row[0] for row in conn.execute("SELECT source FROM compiled_profile_runs")] == ["verbatim"]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO compiled_profile_runs(status, map_model, reduce_model, started_at, "
                "updated_at, source) VALUES ('completed', 'm', 'r', 'x', 'x', 'dreams')"
            )
    finally:
        conn.close()
    with pytest.raises(RuntimeError, match="SQLite-only"):
        migration.apply_postgres(object())


def test_claim_seen_migration_is_idempotent_and_postgres_fails_closed(tmp_path):
    import importlib

    migration = importlib.import_module("memorymaster.stores.migrations.0027_compiled_profile_claim_seen")
    _, db = _service(tmp_path)
    conn = sqlite3.connect(db)
    try:
        migration.apply_sqlite(conn)  # already applied by init_db: must be a no-op
        columns = [row[1] for row in conn.execute("PRAGMA table_info(compiled_profile_claim_seen)")]
        assert columns == ["claim_id", "run_id", "seen_at"]
    finally:
        conn.close()
    with pytest.raises(RuntimeError, match="SQLite-only"):
        migration.apply_postgres(object())


# --- F-03 hardening (operator decisions of 2026-09-23) ----------------------


class _CitingMapper(_Mapper):
    """Adds one more claim id to every candidate's supports, as a mapper that
    cites a claim it was never shown would."""

    def __init__(self, extra_claim_id: int) -> None:
        super().__init__()
        self.extra = -extra_claim_id

    def map(self, messages: tuple[ProfileMessage, ...]) -> tuple[ProfileCandidate, ...]:
        self.calls.append(tuple(item.message_id for item in messages))
        return tuple(
            ProfileCandidate(f"c{abs(item.message_id)}", "identity_locale", "location",
                             "Argentina", "stable", (item.message_id, self.extra))
            for item in messages if "Argentina" in item.text
        )


def _tenant_engine(db: Path, mapper, tmp_path: Path, tenant_id: str | None) -> CompiledProfileEngine:
    return CompiledProfileEngine(ProfileRepository(db, tenant_id=tenant_id), mapper, _Reducer(),
                                 output_dir=tmp_path / "projection",
                                 config=ProfileConfig(max_map_calls=3))


def test_claims_of_another_tenant_are_never_mapped(tmp_path):
    service, db = _service(tmp_path)
    own = [_claim(service, f"Note {n}: the operator is based in Argentina.", tenant_id="acme") for n in range(2)]
    _claim(service, "Operator lives in Argentina, other tenant.", tenant_id="globex")
    _claim(service, "Operator lives in Argentina, legacy row.")
    mapper = _Mapper()

    result = _tenant_engine(db, mapper, tmp_path, "acme").run(force=True)

    assert result["status"] == "completed", result
    assert _mapped(mapper) == sorted(-claim_id for claim_id in own)
    assert ProfileRepository(db).active_facts()[0].support_ids == tuple(sorted(-c for c in own))


def test_an_unset_tenant_reads_only_legacy_rows(tmp_path):
    service, db = _service(tmp_path)
    legacy = [_claim(service, f"Note {n}: the operator is based in Argentina.") for n in range(2)]
    _claim(service, "Operator lives in Argentina, tenant row.", tenant_id="acme")
    mapper = _Mapper()

    _tenant_engine(db, mapper, tmp_path, None).run(force=True)

    assert _mapped(mapper) == sorted(-claim_id for claim_id in legacy)


def test_a_claim_of_another_tenant_never_becomes_a_support(tmp_path):
    service, db = _service(tmp_path)
    for n in range(2):
        _claim(service, f"Note {n}: the operator is based in Argentina.", tenant_id="acme")
    foreign = _claim(service, "Operator lives in Argentina, other tenant.", tenant_id="globex")

    result = _tenant_engine(db, _CitingMapper(foreign), tmp_path, "acme").run(force=True)

    assert result["status"] == "completed", result
    assert result["rejected"] == 1
    assert ProfileRepository(db, tenant_id="acme").active_facts() == ()


def _set_text(service: MemoryService, claim_id: int, text: str) -> None:
    # Legacy rows predate the claim-memory policy, so bare private IPs and
    # emails are stored as-is; write the text the way such a row holds it.
    with service.store.connect() as conn:
        conn.execute("UPDATE claims SET text=? WHERE id=?", (text, claim_id))
        conn.commit()


def test_claim_text_reaches_the_mapper_with_private_ip_and_email_redacted(tmp_path):
    service, db = _service(tmp_path)
    claim_id = _claim(service, "The operator is based in Argentina.")
    _set_text(service, claim_id, "The operator is based in Argentina, keeps the NAS at "
                                 "192.168.1.42 and reads ops.team@example.com daily.")
    mapper = _Mapper()

    _engine(db, mapper, _Reducer(), tmp_path).run(force=True)

    assert mapper.calls == [(-claim_id,)]
    [sent] = mapper.texts
    assert "192.168.1.42" not in sent and "ops.team@example.com" not in sent
    assert "[REDACTED:private_ipv4]" in sent and "[REDACTED:email]" in sent
    assert "The operator is based in Argentina" in sent


def test_claim_egress_redacts_topology_and_blocks_a_surviving_credential():
    import base64

    from memorymaster.profile.egress import prepare_claim_egress

    clean = prepare_claim_egress(
        "Operator notes live in /c/Users/bob/notes and C:/Users/bob/x; relay 10.0.0.7 in prose."
    )
    assert not clean.blocked
    assert "bob" not in clean.text and "10.0.0.7" not in clean.text

    encoded = base64.b64encode(b"aws key AKIAIOSFODNN7EXAMPLE").decode()
    assert prepare_claim_egress(f"Operator deploy note {encoded}").blocked


@pytest.mark.parametrize("path", [
    "/mnt/c/Users/bob/Desktop/proj",  # WSL mount of the Windows profile
    "/users/bob/notes",               # lowercase macOS form
    "~bob/notes",                     # tilde-user home
])
def test_claim_egress_redacts_wsl_lowercase_and_tilde_home_paths(path):
    from memorymaster.profile.egress import prepare_claim_egress

    egress = prepare_claim_egress(f"Operator in Argentina keeps repos at {path} for now.")

    assert not egress.blocked
    assert "bob" not in egress.text, egress.text
    assert "home_path" in egress.findings
    # Prose tildes and the bare current-user home stay readable.
    assert prepare_claim_egress("It takes ~5 minutes; notes live in ~/notes.").findings == ()


# Credentials the canonical redactor misses but dream_bridge refuses to export
# (verifier F-03.2): the map provider must never receive them either.
_EXPORT_REFUSED = {
    "db_pass_env": "The operator is based in Argentina; exports DB_PASS=hunter2 for the NAS backup.",
    "supabase_key": "Operator lives in Argentina and deploys with sbp_0123456789abcdefghijklmnopqrstuvwxyz0123.",
    "sendgrid_key": "Operator in Argentina sends mail with SG.abcdefghijklmnopqrstuv_0123456789.",
    "twilio_sid": "Operator in Argentina texts through AC" + "0123456789abcdef0123456789abcdef" + ".",
    "webhook_token": "Operator in Argentina alerts via a webhook with token abcdefghijklmnopqrstuvwxyz0123.",
    "ssh_shape": "Operator in Argentina runs ssh -p 2222 deploy@buildhost nightly.",
    "public_ip_port": "Operator in Argentina serves the status page at 203.0.113.9:8080.",
    "redacted_payload": "[REDACTED_CLAIM_TEXT]",
}


@pytest.mark.parametrize("name", sorted(_EXPORT_REFUSED))
def test_claim_egress_blocks_what_dream_bridge_refuses_to_export(name):
    from memorymaster.bridges.dream_bridge import _is_sensitive
    from memorymaster.profile.egress import prepare_claim_egress

    text = _EXPORT_REFUSED[name]
    assert _is_sensitive(text), "fixture must be a text the export filter refuses"

    egress = prepare_claim_egress(text)

    assert egress.blocked, egress
    assert "export_refused" in egress.findings


def test_claims_dream_bridge_refuses_to_export_never_reach_the_mapper(tmp_path):
    service, db = _service(tmp_path)
    refused = []
    for name in ("db_pass_env", "supabase_key", "sendgrid_key"):
        claim_id = _claim(service, f"Operator note {name} in Argentina.")
        _set_text(service, claim_id, _EXPORT_REFUSED[name])
        refused.append(claim_id)
    mapper = _Mapper()

    result = _engine(db, mapper, _Reducer(), tmp_path).run(force=True)

    assert mapper.texts == []
    assert mapper.calls == []
    assert result["status"] == "no_changes", result
    assert ProfileRepository(db).pending_claim_bounds() is None


def test_claims_without_lineage_share_one_session_and_cannot_pass_the_gate(tmp_path):
    service, db = _service(tmp_path)
    unlinked = CitationInput(source="mcp", locator="ingest_claim")
    first = _claim(service, "The operator is based in Argentina.", citation=unlinked)
    second = _claim(service, "Operator lives in Argentina.", citation=unlinked)

    batch = ProfileRepository(db).message_batch(after_id=0, through_id=second, max_messages=10,
                                                max_chars=10_000, source="claims")
    result = _engine(db, _Mapper(), _Reducer(), tmp_path).run(force=True)

    assert {item.session_id for item in batch.messages} == {"claim:unlinked"}
    assert {item.message_id for item in batch.messages} == {-first, -second}
    assert result["rejected"] == 1
    assert ProfileRepository(db).active_facts() == ()


def test_the_map_prompt_labels_claims_as_third_party_memory_assertions():
    from types import SimpleNamespace

    from memorymaster.profile.providers import ProfileMapper

    class _Client:
        def __init__(self) -> None:
            self.prompts: list[str] = []

        def complete(self, prompt: str):
            self.prompts.append(prompt)
            return SimpleNamespace(text='{"candidates": []}')

    client = _Client()
    ProfileMapper(client=client).map((
        ProfileMessage(-7, "claim:unlinked", "user", "Operator lives in Argentina.", ""),
        ProfileMessage(3, "s1", "user", "I live in Argentina.", "Where are you based?"),
    ))

    [prompt] = client.prompts
    instructions, messages = prompt.split("MESSAGES:\n", 1)
    claim_row, user_row = json.loads(messages)
    assert claim_row == {"message_id": -7, "scope": "user", "memory_claim": "Operator lives in Argentina."}
    assert user_row == {"message_id": 3, "scope": "user", "user_text": "I live in Argentina.",
                        "assistant_context_only": "Where are you based?"}
    assert "memory_claim" in instructions
    assert "third-party memory assertion" in instructions
    assert "not the operator's own words" in instructions


_INELIGIBLE = {
    "archived": "UPDATE claims SET status='archived' WHERE id=?",
    "superseded": "UPDATE claims SET status='superseded' WHERE id=?",
    "stale": "UPDATE claims SET status='stale' WHERE id=?",
    "conflicted": "UPDATE claims SET status='conflicted' WHERE id=?",
    "other_tenant": "UPDATE claims SET tenant_id='globex' WHERE id=?",
    "non_public": "UPDATE claims SET visibility='private' WHERE id=?",
}


def _make_ineligible(service: MemoryService, claim_id: int, change: str) -> None:
    with service.store.connect() as conn:
        conn.execute(_INELIGIBLE[change], (claim_id,))
        conn.commit()


def _fact_supports(db: Path) -> list[tuple[str, int | None]]:
    with sqlite3.connect(db) as conn:
        return conn.execute(
            """SELECT f.status, s.verbatim_id FROM compiled_profile_facts f
               LEFT JOIN compiled_profile_supports s ON s.fact_id = f.id
               ORDER BY f.id, s.verbatim_id"""
        ).fetchall()


@pytest.mark.parametrize("change", sorted(_INELIGIBLE))
def test_a_fact_whose_claim_support_lost_eligibility_is_retired(tmp_path, change):
    service, db = _service(tmp_path)
    kept = _claim(service, "The operator is based in Argentina.")
    lost = _claim(service, "Operator lives in Argentina.")
    assert _engine(db, _Mapper(), _Reducer(), tmp_path).run(force=True)["status"] == "completed"
    assert len(ProfileRepository(db).active_facts()) == 1
    _make_ineligible(service, lost, change)

    result = _engine(db, _Mapper(), _Reducer(), tmp_path).run(force=True)

    assert result["status"] == "no_changes", result
    assert ProfileRepository(db).active_facts() == ()
    assert _fact_supports(db) == [("expired", -kept)]
    manifest = json.loads((tmp_path / "projection" / "user-profile.json").read_text(encoding="utf-8"))
    assert manifest["facts"] == []
    assert review.check_compiled_profile(review.ReviewConfig(db=db)).counts["mismatches"] == 0


@pytest.mark.parametrize("mode", ["redact", "erase"])
def test_a_fact_whose_claim_support_was_redacted_or_erased_is_retired(tmp_path, mode):
    # redact_claim_payload keeps status and visibility; only the text changes.
    service, db = _service(tmp_path)
    kept = _claim(service, "The operator is based in Argentina.")
    lost = _claim(service, "Operator lives in Argentina.")
    _engine(db, _Mapper(), _Reducer(), tmp_path).run(force=True)
    service.redact_claim_payload(lost, mode=mode, reason="operator request")
    mapper = _Mapper()

    _engine(db, mapper, _Reducer(), tmp_path).run(force=True)

    assert ProfileRepository(db).active_facts() == ()
    assert _fact_supports(db) == [("expired", -kept)]
    assert mapper.texts == []
    assert ProfileRepository(db).pending_claim_bounds() is None


def test_retraction_runs_even_when_the_next_compile_is_not_due(tmp_path):
    # A forgotten claim must stop being injected now, not one cadence later.
    service, db = _service(tmp_path)
    kept = _claim(service, "The operator is based in Argentina.")
    lost = _claim(service, "Operator lives in Argentina.")
    _engine(db, _Mapper(), _Reducer(), tmp_path).run(force=True)
    _make_ineligible(service, lost, "archived")
    mapper = _Mapper()

    result = _engine(db, mapper, _Reducer(), tmp_path).run()

    assert result["status"] == "not_due", result
    assert mapper.calls == []
    assert ProfileRepository(db).active_facts() == ()
    assert _fact_supports(db) == [("expired", -kept)]
    manifest = json.loads((tmp_path / "projection" / "user-profile.json").read_text(encoding="utf-8"))
    assert manifest["facts"] == []


def test_a_fact_that_keeps_enough_sessions_stays_active_on_its_remaining_support(tmp_path):
    service, db = _service(tmp_path)
    ids = [_claim(service, f"Note {n}: the operator is based in Argentina.") for n in range(3)]
    _engine(db, _Mapper(), _Reducer(), tmp_path).run(force=True)
    _make_ineligible(service, ids[0], "archived")

    _engine(db, _Mapper(), _Reducer(), tmp_path).run(force=True)

    [fact] = ProfileRepository(db).active_facts()
    assert fact.support_ids == tuple(sorted(-claim_id for claim_id in ids[1:]))
    assert (fact.support_count, fact.independent_sessions) == (2, 2)
    assert review.check_compiled_profile(review.ReviewConfig(db=db)).counts["mismatches"] == 0


def test_a_retracted_claim_that_becomes_eligible_again_is_mapped_again(tmp_path):
    service, db = _service(tmp_path)
    _claim(service, "The operator is based in Argentina.")
    lost = _claim(service, "Operator lives in Argentina.")
    _engine(db, _Mapper(), _Reducer(), tmp_path).run(force=True)
    _make_ineligible(service, lost, "stale")
    _engine(db, _Mapper(), _Reducer(), tmp_path).run(force=True)
    with service.store.connect() as conn:
        conn.execute("UPDATE claims SET status='confirmed' WHERE id=?", (lost,))
        conn.commit()
    mapper = _Mapper()

    _engine(db, mapper, _Reducer(), tmp_path).run(force=True)

    assert _mapped(mapper) == [-lost]


def test_review_compares_the_real_support_age_with_the_limit(tmp_path, monkeypatch):
    # 7.9 days truncated to 7 passed a 7-day limit.
    monkeypatch.delenv("MEMORYMASTER_COMPILED_PROFILE", raising=False)
    aged = (datetime.now(timezone.utc) - timedelta(days=7.9)).isoformat()

    result = review.check_compiled_profile(review.ReviewConfig(db=_profile_db(tmp_path / "aged.db", aged)))

    assert result.verdict is review.Verdict.WARN
    assert result.counts["newest_support_age_days"] == 7.9
    assert "7.9d" in result.detail


def test_review_reports_an_age_just_over_the_limit_without_rounding_it_away(tmp_path, monkeypatch):
    # 7.04 days rounds to 7.0; "7d old (max 7d)" would contradict the WARN.
    monkeypatch.delenv("MEMORYMASTER_COMPILED_PROFILE", raising=False)
    aged = (datetime.now(timezone.utc) - timedelta(days=7.04)).isoformat()

    result = review.check_compiled_profile(review.ReviewConfig(db=_profile_db(tmp_path / "edge.db", aged)))

    assert result.verdict is review.Verdict.WARN
    assert "more than 7d old (max 7d)" in result.detail


def test_review_counts_a_support_stamped_slightly_ahead_as_fresh(tmp_path, monkeypatch):
    # Clock skew: a support two hours in the future is not of unknown age.
    monkeypatch.delenv("MEMORYMASTER_COMPILED_PROFILE", raising=False)
    ahead = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()

    result = review.check_compiled_profile(review.ReviewConfig(db=_profile_db(tmp_path / "ahead.db", ahead)))

    assert result.verdict is review.Verdict.PASS, result.detail
    assert result.counts["newest_support_age_days"] == 0
    assert "unknown" not in result.detail


def test_a_claim_edited_in_place_no_longer_supports_the_fact(tmp_path):
    # Ruling R4: the support manifest pins the claim text by message_hash. A claim
    # edited in place keeps its id, status and visibility, so eligibility alone
    # would keep a fact supported by text the claim no longer says.
    service, db = _service(tmp_path)
    kept = _claim(service, "The operator is based in Argentina.")
    edited = _claim(service, "Operator lives in Argentina.")
    _engine(db, _Mapper(), _Reducer(), tmp_path).run(force=True)
    assert len(ProfileRepository(db).active_facts()) == 1
    _set_text(service, edited, "Operator moved to Uruguay last year.")
    mapper = _Mapper()

    _engine(db, mapper, _Reducer(), tmp_path).run(force=True)

    assert ProfileRepository(db).active_facts() == ()
    assert _fact_supports(db) == [("expired", -kept)]
    # The edited claim left the seen set, so its new text is mapped afresh.
    assert _mapped(mapper) == [-edited]
    assert review.check_compiled_profile(review.ReviewConfig(db=db)).counts["mismatches"] == 0


def test_an_unedited_claim_support_survives_retraction(tmp_path):
    service, db = _service(tmp_path)
    ids = [_claim(service, f"Note {n}: the operator is based in Argentina.") for n in range(2)]
    _engine(db, _Mapper(), _Reducer(), tmp_path).run(force=True)

    stats = ProfileRepository(db).retract_ineligible_claim_supports(
        now=datetime.now(timezone.utc), min_sessions=2)

    assert stats == {"supports_removed": 0, "facts_retired": 0}
    [fact] = ProfileRepository(db).active_facts()
    assert fact.support_ids == tuple(sorted(-claim_id for claim_id in ids))
