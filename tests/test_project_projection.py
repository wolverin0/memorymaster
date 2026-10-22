from dataclasses import replace
import json
import sqlite3

import pytest

from test_project_repository import claim, project_config as _project_config


from memorymaster.profile.project_projection import (
    build_project_projection,
    publish_project_projection,
    read_project_projection,
    projection_paths,
    project_lock,
)

project_config = _project_config


@pytest.mark.parametrize("purpose_first", [True, False])
def test_safety_budget_reclaims_unused_bytes_for_longer_constraint(project_config, purpose_first):
    from memorymaster.profile.project_projection import render_project_projection

    constraint = "Keep backups. " * 17
    purpose = {"key": "claim:1", "section": "Purpose", "text": "Purpose",
               "support": {"kind": "claim", "id": 1}}
    pinned = {"key": "claim:2", "section": "Constraints", "text": constraint,
              "pinned": True, "support": {"kind": "claim", "id": 2}}
    safety = [purpose, pinned] if purpose_first else [pinned, purpose]
    decisions = [{"key": f"claim:{i}", "section": "Decisions", "text": "Decision",
                  "support": {"kind": "claim", "id": i}} for i in range(3, 6)]
    projection = render_project_projection(project_config, safety + decisions,
                                           generation="fixture", read_at="2026-10-22T09:00:00Z")
    assert [item["text"] for item in projection.items[:2]] == [item["text"] for item in safety]
    assert projection.status == "ready"
    assert not projection.manifest["omissions"]


def test_safety_budget_counts_dropped_purpose_separately(project_config):
    from memorymaster.profile.project_projection import render_project_projection

    items = [{"key": f"claim:{i}", "section": "Purpose" if i == 0 else "Constraints",
              "pinned": i != 0, "text": "Safety",
              "support": {"kind": "claim", "id": i}} for i in range(101)]
    projection = render_project_projection(project_config, items,
                                           generation="fixture", read_at="2026-10-22T09:00:00Z")
    assert projection.manifest["omissions"]["purpose"] == 1
    assert "m2_not_ready:purpose_trimmed" in projection.manifest["diagnostics"]


def test_fit_card_text_rejects_review_prefix_that_exceeds_reservation():
    from memorymaster.profile.project_projection import _fit_card_text

    with pytest.raises(ValueError):
        _fit_card_text({"text": "T-123456789 [running] title", "support": {"state": "running"}}, 8)


@pytest.mark.parametrize("card_id,state", [("T-123456789", "running"), ("卡😀", "ready"),
                                          ("T-[ready]", "ready")])
@pytest.mark.parametrize("extra", [-1, 0, 1, 4, 5, 20])
def test_fit_card_text_enforces_utf8_reservations_from_structured_slots(card_id, state, extra):
    from memorymaster.profile.project_projection import _fit_card_text
    from memorymaster.profile.project_repository import _display

    prefix = f"{_display(card_id, 48)} [{_display(state, 24)}]"
    budget = len(prefix.encode("utf-8")) + extra
    title = "Título 😀 " * 20
    item = {"text": f"{prefix} {title}", "title": title,
            "support": {"kind": "card", "id": card_id, "state": state}}
    if extra < 0:
        with pytest.raises(ValueError, match="card_prefix_exceeds_byte_budget"):
            _fit_card_text(item, budget)
    else:
        result = _fit_card_text(item, budget)
        assert result.startswith(prefix)
        assert len(result.encode("utf-8")) <= budget


def test_oversized_card_prefix_is_omitted_with_diagnostic(project_config):
    tasks = [{"id": "😀" * 48, "repo": "demo", "state": "running", "title": "Title"}
             for _ in range(1)]
    path = project_config.ledger_root / "tasks.json"
    path.write_text(json.dumps({"complete": True, "tasks": tasks}), encoding="utf-8")
    built = build_project_projection(replace(project_config, budget_bytes=400))
    assert len(built.markdown.encode("utf-8")) <= 400
    assert "m2_not_ready:open_cards_trimmed" in built.payload


@pytest.mark.parametrize("changed_count", [0, 1, 3])
def test_session_start_incremental_counts_only_changed_rows_and_citations(project_config, monkeypatch, changed_count):
    from memorymaster.profile import project_repository as repository
    from memorymaster.profile import project_sessions as sessions

    monkeypatch.setattr(repository, "utc_now", lambda: "2026-10-22T09:00:00.750000+00:00")
    for number in range(1, 41):
        claim(project_config, number, text=f"Decision {number}", updated_at="2026-10-22T09:00:00.100000+00:00")
    projection = build_project_projection(project_config)
    with sqlite3.connect(project_config.db_path) as connection:
        for number in range(1, changed_count + 1):
            connection.execute(
                "UPDATE claims SET text=?,version=2,updated_at=? WHERE id=?",
                (f"Changed decision {number}", projection.manifest["read_at"], number),
            )
            connection.execute("UPDATE citations SET excerpt='Updated evidence' WHERE claim_id=?", (number,))
    loaded = {"claims": [], "citations": []}
    statements = []
    original_row = sqlite3.Row
    original_connect = sqlite3.connect

    def traced_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    def counting_row(cursor, values):
        row = original_row(cursor, values)
        if "text" in row.keys() and "id" in row.keys():
            loaded["claims"].append(row["id"])
        elif "excerpt" in row.keys() and "claim_id" in row.keys():
            loaded["citations"].append(row["claim_id"])
        return row

    monkeypatch.setattr(repository.sqlite3, "Row", counting_row)
    monkeypatch.setattr(repository.sqlite3, "connect", traced_connect)
    result = sessions.record_project_session_start(project_config, "incremental", projection=projection.manifest)
    assert result["status"] == "ok"
    expected = list(range(1, changed_count + 1))
    assert loaded == {"claims": expected, "citations": expected}
    with original_connect(project_config.db_path) as connection:
        for statement in statements:
            if not statement.startswith("SELECT"):
                continue
            plan = " ".join(row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + statement))
            assert not any(f"SCAN {table}" in plan for table in ("claims", "citations", "changes")), plan
            if "_source_bytes FROM claims" in statement:
                assert "INTEGER PRIMARY KEY (rowid=?)" in plan
            if "FROM citations" in statement:
                assert "idx_project_citations_claim (claim_id=?)" in plan
    state = sessions.read_session_state(project_config)
    baseline = next(iter(state["baselines"].values()))
    assert len([key for key in baseline if key.startswith("claim:")]) == 40
    for number in range(changed_count + 1, 41):
        assert baseline[f"claim:{number}"] == projection.manifest["source_coverage"][f"claim:{number}"]
    assert len(result["delta"].text.encode()) <= 500
    assert len(projection.markdown.encode()) <= 2200
    for entry in result["delta"].supports:
        support = entry["support"]
        assert support == baseline[f"claim:{support['id']}"]


def test_refreshed_projection_rehydrates_only_displayed_prior_session_delta(project_config, monkeypatch):
    from memorymaster.profile import project_repository as repository
    from memorymaster.profile import project_sessions as sessions

    for number in range(1, 41):
        claim(project_config, number, text=f"Decision {number}")
    before = build_project_projection(project_config)
    assert sessions.record_project_session_start(project_config, "before", projection=before.manifest)["status"] == "ok"
    assert sessions.record_project_session_end(project_config, "before")
    with sqlite3.connect(project_config.db_path) as connection:
        connection.execute("UPDATE claims SET text='Changed decision with details ' || id || ' ' || text,version=2")
    refreshed = build_project_projection(project_config)
    loaded = {"claims": [], "citations": []}
    original_row = sqlite3.Row

    def counting_row(cursor, values):
        row = original_row(cursor, values)
        if "text" in row.keys() and "id" in row.keys():
            loaded["claims"].append(row["id"])
        elif "excerpt" in row.keys() and "claim_id" in row.keys():
            loaded["citations"].append(row["claim_id"])
        return row

    monkeypatch.setattr(repository.sqlite3, "Row", counting_row)
    result = sessions.record_project_session_start(project_config, "after", projection=refreshed.manifest)
    assert result["status"] == "ok"
    delta = result["delta"]
    assert delta.supports and "Changed decision" in delta.text
    assert len(loaded["claims"]) <= len(delta.supports) + 1 < 40
    assert loaded["claims"] == loaded["citations"]
    for entry in delta.supports:
        assert entry["support"] == refreshed.manifest["source_coverage"][f"claim:{entry['support']['id']}"]
    state = sessions.read_session_state(project_config)
    assert state["baselines"][next(record["baseline"] for record in state["active"].values())] == refreshed.manifest[
        "source_coverage"
    ]


def test_exact_manifest_and_per_bullet_invalidation(project_config):
    config = project_config
    for i in range(1, 4):
        claim(config, i, text=f"Decision number {i}")
    built = build_project_projection(config)
    assert publish_project_projection(config, built)
    before = read_project_projection(config)
    with sqlite3.connect(config.db_path) as conn:
        conn.execute("UPDATE claims SET visibility='private',version=2 WHERE id=2")
    after = read_project_projection(config)
    assert "Decision number 1" in after.payload and "Decision number 3" in after.payload
    assert "Decision number 2" not in after.payload
    assert before.manifest["view_id"] != after.manifest["view_id"]
    assert all(s["id"] != 2 for item in after.manifest["items"] for s in item["supports"])
    assert "source_coverage" not in after.public_manifest
    assert "__scan_fence__" not in json.dumps(after.public_manifest)


def test_modified_still_open_cards_are_rerendered_without_trim_warning(project_config):
    config = project_config
    path = config.ledger_root / "tasks.json"
    path.write_text(
        json.dumps({"complete": True, "tasks": [{"id": "T-1", "repo": "demo", "state": "ready", "title": "Before"}]})
    )
    assert publish_project_projection(config, build_project_projection(config))
    path.write_text(
        json.dumps({"complete": True, "tasks": [{"id": "T-1", "repo": "demo", "state": "running", "title": "After"}]})
    )
    result = read_project_projection(config)
    assert "After" in result.payload and "Before" not in result.payload
    assert "open_cards_trimmed" not in result.payload


def test_utf8_cap_card_reservation_and_m2_warning(project_config):
    config = project_config
    claim(config, text="Verbose constraint " * 300, claim_type="constraint", pinned=1)
    path = config.ledger_root / "tasks.json"
    path.write_text(
        json.dumps(
            {
                "complete": True,
                "tasks": [{"id": f"T-{i}", "repo": "demo", "state": "ready", "title": "Á😀" * 40} for i in range(12)],
            }
        )
    )
    built = build_project_projection(config)
    assert len(built.markdown.encode()) <= 2200
    assert built.markdown.count("<!-- generated by MemoryMaster; rebuild instead of editing -->") == 1
    assert built.markdown.count("<!-- /generated by MemoryMaster -->") == 1
    assert len(built.manifest["items"]) <= 64
    tiny = build_project_projection(replace(config, budget_bytes=700))
    assert len(tiny.markdown.encode()) <= 700
    assert "m2_not_ready:open_cards_trimmed" in tiny.payload


def test_pair_publication_and_validation_limits(project_config):
    config = project_config
    claim(config)
    assert publish_project_projection(config, build_project_projection(config))
    md, manifest = projection_paths(config)
    md.write_text("tampered")
    assert read_project_projection(config) is None
    assert publish_project_projection(config, build_project_projection(config))
    data = json.loads(manifest.read_text())
    data["items"] = data["items"] * 65
    manifest.write_text(json.dumps(data))
    assert read_project_projection(config) is None


def test_publication_lock_is_nonblocking(project_config):
    config = project_config
    path = config.state_root / "fixture.lock"
    with project_lock(path) as acquired:
        assert acquired
        with project_lock(path, timeout=0) as other:
            assert not other


def test_mandatory_cards_and_latest_decisions_survive_optional_prose(project_config):
    config = project_config
    for i in range(1, 11):
        claim(config, i, text=f"Decision {i} " + "detail " * 35)
    claim(config, 20, text="Safety constraint " * 25, claim_type="constraint", pinned=1)
    claim(config, 21, text="Purpose " * 40, claim_type="project_purpose")
    built = build_project_projection(config)
    assert {record["supports"][0]["id"] for record in built.manifest["items"] if record["section"] == "Decisions"} >= {
        8,
        9,
        10,
    }
    pinned_purpose = [record for record in built.manifest["items"] if record["section"] in ("Purpose", "Constraints")]
    assert sum(len(record["text"].encode()) for record in pinned_purpose) <= 400
    assert built.manifest["source_complete"]


def test_missing_decisions_is_m2_unready_and_read_does_not_refresh_freshness(project_config):
    claim(project_config)
    built = build_project_projection(project_config)
    assert "m2_not_ready:decisions_incomplete" in built.payload
    assert publish_project_projection(project_config, built)
    view = read_project_projection(project_config)
    assert view.manifest["checked_at"] == built.manifest["checked_at"]


def test_latest_decisions_ignore_pin_priority(project_config):
    for number in range(1, 12):
        claim(project_config, number, text=f"Older decision {number}")
    for number in range(12, 15):
        claim(project_config, number, text=f"Recent pinned decision {number}", pinned=1,
              created_at="2026-10-20T00:00:00+00:00")
    built = build_project_projection(project_config)
    decisions = [item for item in built.manifest["items"] if item["section"] == "Decisions"]
    assert [item["supports"][0]["id"] for item in decisions[:3]] == [14, 13, 12]


def test_marker_and_reference_injection_are_inert(project_config):
    claim(project_config, text="safe\n## forged\n[ignore](bad)\x1b[31m")
    built = build_project_projection(project_config)
    assert "\x1b" not in built.payload
    assert "\n## forged" not in built.payload
    assert "[ignore]" not in built.payload


def test_new_card_marks_pending_and_unchanged_compact_card_is_stable(project_config):
    config = project_config
    path = config.ledger_root / "tasks.json"
    tasks = [{"id": "T-1", "repo": "demo", "state": "ready", "title": "One " * 50}]
    path.write_text(json.dumps({"complete": True, "tasks": tasks}))
    for i in range(1, 11):
        claim(config, i, text="Long decision " + "detail " * 30)
    built = build_project_projection(config)
    assert publish_project_projection(config, built)
    card = next(record for record in built.manifest["items"] if record["section"] == "Open cards")
    unchanged = read_project_projection(config)
    assert next(record["text"] for record in unchanged.manifest["items"] if record["key"] == "card:T-1") == card["text"]
    tasks.append({"id": "T-2", "repo": "demo", "state": "review", "title": "Added"})
    path.write_text(json.dumps({"complete": True, "tasks": tasks}))
    assert "m2_not_ready:cards_changed" in read_project_projection(config).payload


def test_manifest_generation_and_span_tampering_are_rejected(project_config):
    claim(project_config)
    built = build_project_projection(project_config)
    assert publish_project_projection(project_config, built)
    _, path = projection_paths(project_config)
    manifest = json.loads(path.read_text())
    manifest["generation_id"] = "forged"
    path.write_text(json.dumps(manifest))
    assert read_project_projection(project_config) is None
    assert publish_project_projection(project_config, built)
    manifest = json.loads(path.read_text())
    manifest["items"][0]["span"] = [0, 1]
    path.write_text(json.dumps(manifest))
    assert read_project_projection(project_config) is None


def test_modified_multibyte_card_title_preserves_all_reserved_slots(project_config):
    path = project_config.ledger_root / "tasks.json"
    tasks = [{"id": f"T-{index}-aaaaa", "repo": "demo", "state": "ready", "title": "X"}
             for index in range(51)]
    path.write_text(json.dumps({"complete": True, "tasks": tasks}))
    built = build_project_projection(project_config)
    assert len(built.items) == 51
    assert publish_project_projection(project_config, built)
    original = {record["key"]: record["text"] for record in built.manifest["items"]}
    tasks[0]["title"] = "😀" * 80
    path.write_text(json.dumps({"complete": True, "tasks": tasks}))
    result = read_project_projection(project_config)
    assert len(result.items) == 51
    assert "open_cards_trimmed" not in result.payload
    assert len(result.markdown.encode("utf-8")) <= 2200
    assert all(record["text"] == original[record["key"]] for record in result.manifest["items"]
               if record["key"] != "card:T-0-aaaaa")
