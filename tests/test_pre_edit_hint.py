"""T-2548: the opt-in pre-edit hint (what memory already holds about the file about to be edited).

These tests prove GATING and SILENCE: the hint says something only when a claim names the file, once per file
per session, from the warm server only, and never records what it did not show. They cannot prove the hint is
useful to an agent; that is unmeasured.

Silence tests are vacuous when the fixture could never speak, so every silence case runs on a fixture that is
shown to speak in its positive control (conftest switches remote recall off for every test: the hook tests
turn it back on against a loopback fake server and always name the URL and token explicitly).
"""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from memorymaster.core import hook_log, lifecycle, spool
from memorymaster.core.models import CitationInput
from memorymaster.core.service import MemoryService
from memorymaster.recall import context_hook, file_hint, hook_policy, remote
from memorymaster.recall.qdrant_outbox import ENV_OUTBOX_DIR

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "memorymaster" / "config_templates" / "hooks" / "memorymaster-pretooluse-recall.py"
CWD = str(REPO)
CLAIM = {"id": 11, "human_id": "mm-aaaa", "text": "hook_log.py resolves its file per call; never cache the path."}


def _payload(path="G:/repo/memorymaster/core/hook_log.py", *, session="sess-1", tool="Edit", cwd=CWD):
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    return {"tool_name": tool, "session_id": session, "cwd": cwd, "tool_input": {key: path}}


# --- gating, pure ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("text, names", [
    ("hook_log.py resolves its file per call", True),
    ("The fix lives in core/hook_log.py.", True),
    ("see core\\hook_log.py", True),  # either separator
    ("HOOK_LOG.PY is case-insensitive", True),
    ("hook_log resolves its file", False),  # the bare stem is never enough
    ("hook_log.js is another file", False),  # same stem, other file
    ("hook_log.pyc is bytecode", False),
    ("my_hook_log.py is another file", False),
    ("hook_log.py.bak is a backup", False),
    ("the log hook writes lines", False),  # shares words, not the name
])
def test_a_claim_names_the_file_only_as_a_whole_token(text, names):
    assert file_hint.names_file(text, ["memorymaster", "core", "hook_log.py"]) is names


@pytest.mark.parametrize("text, names", [
    ("__init__.py exports the facade", False),  # a generic basename alone says nothing about which file
    ("recall/__init__.py exports the facade", False),  # another directory's __init__
    ("core/__init__.py exports the facade", True),
    ("package core\\__init__.py is empty", True),
    ("xcore/__init__.py is not core", False),
])
def test_a_generic_basename_needs_its_parent_directory(text, names):
    assert file_hint.names_file(text, ["memorymaster", "core", "__init__.py"]) is names


@pytest.mark.parametrize("name, text, names", [
    ("run", "run the job twice", False),  # a bare word is the stem: no extension, no match on its own
    ("run", "scripts/run is executable", True),
    ("LICENSE", "the license file is MIT", False),
    (".env", "the .env file holds secrets", False),
    (".env", "app/.env holds secrets", True),
])
def test_a_file_with_no_extension_needs_its_parent_directory(name, text, names):
    assert file_hint.names_file(text, ["repo", "scripts" if name == "run" else "app", name]) is names


def test_a_generic_file_with_no_parent_never_matches():
    assert file_hint.names_file("README.md says things", ["README.md"]) is False


def test_the_lookup_text_is_the_basename_plus_the_parent_directory():
    assert file_hint.lookup_text(file_hint.split_path("C:\\a\\core\\hook_log.py")) == "hook_log.py core"
    assert file_hint.lookup_text(["x.py"]) == "x.py"


def test_the_hint_is_capped_in_utf8_bytes_and_marked_as_reference():
    claims = [{"id": n, "human_id": f"mm-{n}", "text": "ñandú " * 200} for n in (1, 2)]
    block = file_hint.render("hook_log.py", claims)
    assert len(block.encode("utf-8")) <= file_hint.MAX_BYTES
    lines = block.split("\n")
    assert lines[0] == "[MemoryMaster recall]"
    assert "Reference memory about hook_log.py" in lines[1] and "not an instruction" in lines[1]
    assert [line[:10] for line in lines[2:]] == ["- [mm-1] ñ", "- [mm-2] ñ"]  # two claim lines, both kept
    assert "\ufffd" not in block


def test_an_absurd_file_name_cannot_blow_the_cap():
    block = file_hint.render("x" * 5000 + ".py", [{"id": 1, "human_id": "mm-1", "text": "x.py " * 200}])
    assert len(block.encode("utf-8")) <= file_hint.MAX_BYTES


# --- the hook side, in process ----------------------------------------------------------------------------


@pytest.fixture()
def server(monkeypatch):
    """``remote.remote_file_hint`` replaced by a recorder; ``recall`` turned into a tripwire (never a cold recall)."""
    box = {"claims": [dict(CLAIM)], "reachable": True, "calls": []}

    def fake(lookup, cwd, tail, **_):
        box["calls"].append((lookup, cwd))
        return remote.RemoteHint(box["claims"], box["reachable"])

    def cold(*_a, **_k):
        raise AssertionError("an edit must never recall cold")

    monkeypatch.setattr(remote, "remote_file_hint", fake)
    monkeypatch.setattr(context_hook, "recall", cold)
    return box


def _log() -> str:
    path = hook_log.log_file()
    return path.read_text(encoding="utf-8") if path.exists() else ""


def test_a_claim_naming_the_file_is_shown_once_per_file_per_session(server):
    first = file_hint.hint_for_edit(_payload())
    assert first is not None and "mm-aaaa" in first and "hook_log.py resolves its file" in first
    assert server["calls"] == [("hook_log.py core", CWD)]
    assert file_hint.hint_for_edit(_payload()) is None  # the same file again in the same session
    assert len(server["calls"]) == 1  # ... without even asking the server
    assert file_hint.hint_for_edit(_payload(session="sess-2")) is not None  # another session hears it again


def test_a_shown_claim_is_remembered_so_the_prompt_hook_does_not_repeat_it(server):
    assert file_hint.hint_for_edit(_payload()) is not None
    assert 11 in hook_policy.seen_ids("sess-1")
    server["claims"].append({"id": 12, "human_id": "mm-bbbb", "text": "see core/hook_log.py too"})
    # another file in the same session: the claim already shown is not shown again, the new one is
    out = file_hint.hint_for_edit(_payload("G:/repo/memorymaster/other/hook_log.py"))
    assert out is not None and "mm-bbbb" in out and "mm-aaaa" not in out


def test_a_claim_the_prompt_hook_already_injected_is_not_repeated(server):
    hook_policy.remember("sess-1", [11])
    assert file_hint.hint_for_edit(_payload()) is None
    assert "reason=no-claim-names-file" in _log()


def test_claims_that_do_not_name_the_file_are_dropped_unseen(server):
    server["claims"] = [
        {"id": 21, "human_id": "mm-1", "text": "the log hook writes lines"},  # shares words
        {"id": 22, "human_id": "mm-2", "text": "hook_log.js is another file"},  # same stem
        {"id": 23, "human_id": "mm-3", "text": "hook_log.py: the only claim that names it"},
        {"id": 24, "human_id": "mm-4", "text": "a third claim about hook_log.py"},
        {"id": 25, "human_id": "mm-5", "text": "a fourth claim about hook_log.py"},
    ]
    out = file_hint.hint_for_edit(_payload())
    assert out is not None and "mm-3" in out and "mm-4" in out
    assert "mm-1" not in out and "mm-2" not in out and "mm-5" not in out  # at most two lines
    assert hook_policy.seen_ids("sess-1") == {23, 24}  # dropped and capped claims stay unseen


def test_nothing_naming_the_file_means_silence_and_no_state(server):
    server["claims"] = [{"id": 21, "human_id": "mm-1", "text": "the log hook writes lines"}]
    assert file_hint.hint_for_edit(_payload()) is None
    assert hook_policy.seen_ids("sess-1") == frozenset() and hook_policy.hinted_files("sess-1") == frozenset()
    server["claims"] = [dict(CLAIM)]  # control: the same fixture speaks when a claim names the file
    assert file_hint.hint_for_edit(_payload()) is not None


@pytest.mark.parametrize("reachable, reason", [(False, "server-unreachable"), (True, "server-busy")])
def test_an_unreachable_or_busy_server_means_silence_never_a_cold_recall(server, reachable, reason):
    server["claims"], server["reachable"] = None, reachable
    assert file_hint.hint_for_edit(_payload()) is None  # the tripwire recall would raise
    assert f"reason={reason}" in _log()
    server["claims"], server["reachable"] = [dict(CLAIM)], True  # control
    assert file_hint.hint_for_edit(_payload()) is not None


def test_a_generic_basename_needs_its_parent_in_the_claim(server):
    server["claims"] = [{"id": 31, "human_id": "mm-9", "text": "__init__.py re-exports everything"}]
    path = "G:/repo/memorymaster/core/__init__.py"
    assert file_hint.hint_for_edit(_payload(path)) is None
    server["claims"] = [{"id": 31, "human_id": "mm-9", "text": "core/__init__.py re-exports everything"}]
    assert file_hint.hint_for_edit(_payload(path)) is not None


@pytest.mark.parametrize("change", [
    {"session_id": ""}, {"session_id": None}, {"cwd": ""}, {"tool_input": {}}, {"tool_input": None},
    {"tool_input": {"file_path": 7}},
])
def test_an_unusable_payload_is_silent(server, change):
    assert file_hint.hint_for_edit({**_payload(), **change}) is None
    assert server["calls"] == []


def test_a_notebook_edit_is_hinted_by_its_notebook_path(server):
    server["claims"] = [{"id": 41, "human_id": "mm-n", "text": "report.ipynb is regenerated by the nightly job"}]
    assert file_hint.hint_for_edit(_payload("G:/repo/nb/report.ipynb", tool="NotebookEdit")) is not None


def test_the_log_has_one_line_per_decision_and_no_path_or_claim_text(server):
    file_hint.hint_for_edit(_payload())  # hit
    file_hint.hint_for_edit(_payload())  # skip already-hinted
    text = _log()
    lines = [line for line in text.splitlines() if "hook=pre-edit-hint" in line]
    assert len(lines) == 2
    assert "event=hit" in lines[0] and "tool=Edit" in lines[0] and "file=hook_log.py" in lines[0]
    assert "candidates=1" in lines[0] and "shown=1" in lines[0]
    assert "event=skip" in lines[1] and "reason=already-hinted" in lines[1]
    assert "memorymaster/core" not in text and "memorymaster\\core" not in text  # no path
    assert "resolves its file" not in text  # no claim text


def test_forgetting_the_session_lets_a_compacted_context_hear_it_again(server):
    assert file_hint.hint_for_edit(_payload()) is not None
    assert file_hint.hint_for_edit(_payload()) is None
    hook_policy.forget("sess-1")  # SessionStart after a compaction
    assert file_hint.hint_for_edit(_payload()) is not None


# --- the hook process, run exactly as Claude Code runs it ---------------------------------------------------


class _Fake(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append((body, self.headers.get("Authorization")))
        time.sleep(self.server.delay)
        data = json.dumps(self.server.answer).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_a):
        pass


@pytest.fixture()
def fake(tmp_path):
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    httpd.handle_error = lambda *_a: None  # a client that gave up on a slow answer is the point of a test
    httpd.daemon_threads, httpd.block_on_close = True, False  # teardown must not wait for a slow handler
    httpd.requests, httpd.delay, httpd.answer = [], 0.0, {"claims": [dict(CLAIM)]}
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture()
def hook(tmp_path):
    path = tmp_path / "hook.py"
    path.write_text(TEMPLATE.read_text(encoding="utf-8").replace("__MEMORYMASTER_PROJECT_ROOT__", str(REPO)),
                    encoding="utf-8")
    return path


def _run(hook, payload, tmp_path, *, port, edit_flag=None, old_flag=None, extra=""):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("MEMORYMASTER_") or k in ("MEMORYMASTER_RECALL_SEEN_DIR",)}
    env.update({
        "MEMORYMASTER_HOOK_RECALL_REMOTE": "1",
        "MEMORYMASTER_SHARED_MCP_URL": f"http://127.0.0.1:{port}/mcp",
        "MEMORYMASTER_MCP_HTTP_TOKEN": "t",
        "MEMORYMASTER_HOOK_STATE_DIR": str(tmp_path / "state"),
        "MEMORYMASTER_RECALL_SEEN_DIR": str(tmp_path / "seen"),
        "HOME": str(tmp_path / "home"), "USERPROFILE": str(tmp_path / "home"),
    })
    for name, value in (("MEMORYMASTER_PRE_EDIT_HINT", edit_flag), ("MEMORYMASTER_PRETOOLUSE_RECALL", old_flag)):
        if value is not None:
            env[name] = value
    code = (
        "import runpy, sys\n"
        f"try:\n    runpy.run_path({str(hook)!r}, run_name='__main__')\n"
        "except SystemExit:\n    pass\n"
        "sys.stderr.write('COLD=' + str('memorymaster.recall.context_hook' in sys.modules) + '\\n')\n"
        + extra
    )
    started = time.monotonic()
    done = subprocess.run([sys.executable, "-c", code], input=json.dumps(payload), capture_output=True,
                          text=True, env=env, timeout=60, cwd=str(tmp_path))
    return done, time.monotonic() - started


def test_flag_off_prints_nothing_and_does_not_even_ask_the_server(hook, fake, tmp_path):
    done, _ = _run(hook, _payload(), tmp_path, port=fake.server_address[1])
    assert done.returncode == 0 and done.stdout == "" and fake.requests == []
    assert "COLD=False" in done.stderr
    # control: the same process, same payload, same server, with the flag on, speaks
    done, _ = _run(hook, _payload(), tmp_path, port=fake.server_address[1], edit_flag="1")
    assert done.stdout and len(fake.requests) == 1


def test_each_flag_governs_only_its_own_tools(hook, fake, tmp_path):
    port = fake.server_address[1]
    done, _ = _run(hook, _payload(), tmp_path, port=port, old_flag="1")  # Grep/Glob flag, edit tool
    assert done.stdout == "" and fake.requests == []
    grep = {"tool_name": "Grep", "session_id": "s", "tool_input": {"pattern": "some two words"}}
    done, _ = _run(hook, grep, tmp_path, port=port, edit_flag="1")  # edit flag, Grep
    assert done.stdout == "" and fake.requests == [] and "COLD=False" in done.stderr


@pytest.mark.parametrize("tool", file_hint.EDIT_TOOLS)
def test_an_edit_gets_one_hint_with_the_claim_and_no_prompt_or_session_on_the_wire(hook, fake, tmp_path, tool):
    done, _ = _run(hook, _payload(tool=tool), tmp_path, port=fake.server_address[1], edit_flag="1")
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse" and "mm-aaaa" in out["additionalContext"]
    assert len(out["additionalContext"].encode("utf-8")) <= file_hint.MAX_BYTES
    (body, auth), = fake.requests
    expected = {"query": "hook_log.py core", "tail": ["core", "hook_log.py"]}
    assert body == {"file_hint": expected, "hook_data": {"cwd": CWD}}  # no top-level query, no session
    assert auth == "Bearer t"
    assert "COLD=False" in done.stderr  # recall (the cold path) was never imported


def test_the_second_edit_is_silent_and_another_session_hears_it_again(hook, fake, tmp_path):
    port = fake.server_address[1]
    first, _ = _run(hook, _payload(), tmp_path, port=port, edit_flag="1")
    again, _ = _run(hook, _payload(), tmp_path, port=port, edit_flag="1")
    other, _ = _run(hook, _payload(session="sess-2"), tmp_path, port=port, edit_flag="1")
    assert first.stdout and again.stdout == "" and other.stdout
    assert len(fake.requests) == 2  # the repeat did not reach the server


def test_an_unreachable_server_is_silent_and_fast(hook, tmp_path):
    started = time.monotonic()
    done, took = _run(hook, _payload(), tmp_path, port=1, edit_flag="1")
    assert done.returncode == 0 and done.stdout == "" and "COLD=False" in done.stderr
    # a refused loopback connect can take longer than the timeout on Windows: either reason is silence
    log = (tmp_path / "state" / "hook.log").read_text(encoding="utf-8")
    assert "reason=server-unreachable" in log or "reason=server-busy" in log
    assert time.monotonic() - started < 20  # generous: interpreter start on a loaded runner


def test_a_slow_server_is_given_up_on_inside_the_registered_timeout(hook, fake, tmp_path):
    fake.delay = 10.0
    done, took = _run(hook, _payload(), tmp_path, port=fake.server_address[1], edit_flag="1")
    assert done.returncode == 0 and done.stdout == "" and "COLD=False" in done.stderr
    assert "reason=server-busy" in (tmp_path / "state" / "hook.log").read_text(encoding="utf-8")
    assert took < 8.0  # it gave up at the hint timeout (1.5 s) rather than waiting for the 10 s answer


def test_an_older_server_without_the_field_is_silent(hook, fake, tmp_path):
    fake.answer = {"ctx": ""}  # what a server that does not know file_hint answers
    done, _ = _run(hook, _payload(), tmp_path, port=fake.server_address[1], edit_flag="1")
    assert done.returncode == 0 and done.stdout == ""
    assert len(fake.requests) == 1  # control: it did ask


def test_a_crash_inside_the_hint_is_silent_and_logs_only_the_exception_type(hook, fake, tmp_path):
    secret_path = "G:/private-area/core/hook_log.py"
    boom = ("import memorymaster.recall.file_hint as f\n"
            "def _x(*a, **k):\n    raise RuntimeError('G:/private-area/core/hook_log.py exploded')\n"
            "f.hint_for_edit = _x\n")
    # the patch must land before the template imports the name, so it runs in a sitecustomize-style prelude
    prelude = tmp_path / "prelude.py"
    prelude.write_text(boom, encoding="utf-8")
    src = hook.read_text(encoding="utf-8")
    hook.write_text(f"import sys; sys.path.insert(0, {str(REPO)!r}); exec(open({str(prelude)!r}).read())\n" + src,
                    encoding="utf-8")
    done, _ = _run(hook, _payload(secret_path), tmp_path, port=fake.server_address[1], edit_flag="1")
    assert done.returncode == 0 and done.stdout == ""
    log = (tmp_path / "state" / "hook.log").read_text(encoding="utf-8")
    assert "event=error" in log and "error=RuntimeError" in log and "private-area" not in log


# --- the server side: candidates, recording nothing ------------------------------------------------------------


@pytest.fixture()
def db(tmp_path: Path, monkeypatch) -> str:
    monkeypatch.setenv(ENV_OUTBOX_DIR, str(tmp_path / "outbox"))
    monkeypatch.setenv("MEMORYMASTER_SPOOL_DIR", str(tmp_path / "spool"))
    for name in ("QDRANT_URL", "MEMORYMASTER_SCOPE_DEFAULT", "MEMORYMASTER_JEV_MODE"):
        monkeypatch.delenv(name, raising=False)
    svc = MemoryService(tmp_path / "hint.db", workspace_root=tmp_path)
    svc.init_db()

    def add(text, scope, confirm=True):
        claim = svc.store.create_claim(text=text, citations=[CitationInput(source="test://hint")], scope=scope)
        if confirm:
            lifecycle.transition_claim(svc.store, claim.id, "confirmed", reason="fixture", event_type="validator")
        return claim.id

    ids = {
        "mine": add("hook_log.py resolves its file per call; never cache the path", "project:pubgclone"),
        "other": add("hook_log.py in the other project writes to a socket", "project:elsewhere"),
        "candidate": add("hook_log.py candidate note nobody validated", "project:pubgclone", confirm=False),
    }
    (tmp_path / "pubgclone").mkdir()
    return str(svc.store.db_path), ids


def _count(db_path: str, claim_id: int) -> int:
    svc = MemoryService(db_path, workspace_root=Path(db_path).parent, read_only=True)
    return svc.store.get_claim(claim_id, include_citations=False).access_count


def test_lookup_returns_authorized_candidates_of_the_session_project_only(db, tmp_path):
    path, ids = db
    found = file_hint.lookup("hook_log.py core", db_path=path, hook_data={"cwd": str(tmp_path / "pubgclone")})
    assert [c["id"] for c in found] == [ids["mine"]]  # not the other project, not the unvalidated candidate
    assert found[0]["human_id"] and "resolves its file" in found[0]["text"]
    assert context_hook._current_scope() == "project:memorymaster"  # the request scope did not leak out
    assert file_hint.lookup("hook_log.py core", db_path=path, hook_data={}) == []  # no cwd, no default scope
    assert file_hint.lookup("  ", db_path=path, hook_data={"cwd": str(tmp_path / "pubgclone")}) == []


def test_lookup_records_nothing_but_the_prompt_path_does(db, tmp_path):
    path, ids = db
    cwd = str(tmp_path / "pubgclone")
    file_hint.lookup("hook_log.py core", db_path=path, hook_data={"cwd": cwd})
    assert spool.claim_files(path) == [] and _count(path, ids["mine"]) == 0
    seen_dir = Path(os.environ["MEMORYMASTER_RECALL_SEEN_DIR"])  # the directory the prompt hook writes
    assert list(seen_dir.iterdir()) == [] and hook_policy.seen_ids("s") == frozenset()
    assert "event=receipt" not in _log()
    # control: the prompt path with the same DB does leave a trace, so the silence above means something
    context_hook.recall("what is known about hook_log.py core", db_path=path, skip_qdrant=True,
                        hook_data={"cwd": cwd, "session_id": "s"})
    assert spool.claim_files(path) != [] or _count(path, ids["mine"]) > 0
    assert ids["mine"] in hook_policy.seen_ids("s") and list(seen_dir.iterdir()) != []


def test_the_claim_naming_the_file_survives_a_crowd_of_lookalikes(db, tmp_path):
    """Thirty in-scope claims sharing the query's words outrank the one claim that names the file (it is last in
    the lexical pool): the pool is searched deeper than the cap and gated on the name before the cap applies."""
    path, ids = db
    svc = MemoryService(path, workspace_root=tmp_path)
    for n in range(30):
        claim = svc.store.create_claim(
            text=f"core core core hook log core hook log {n} hook_log.js hook_log.pyc core py logging core",
            citations=[CitationInput(source=f"test://hint/{n}")],  # distinct sources: recall caps rows per source
            scope="project:pubgclone")
        lifecycle.transition_claim(svc.store, claim.id, "confirmed", reason="fixture", event_type="validator")
    only = svc.store.create_claim(text="hook_log.py must never cache its path", scope="project:pubgclone",
                                  citations=[CitationInput(source="test://hint")])
    lifecycle.transition_claim(svc.store, only.id, "confirmed", reason="fixture", event_type="validator")
    cwd = {"cwd": str(tmp_path / "pubgclone")}
    found = file_hint.lookup("hook_log.py core", db_path=path, hook_data=cwd, tail=["core", "hook_log.py"])
    assert {c["id"] for c in found} == {only.id, ids["mine"]}  # only the claims that name the file, none of the crowd


# --- the shared server's route ------------------------------------------------------------------------------


def _client(monkeypatch, db_target="test.db"):
    starlette_testclient = pytest.importorskip("starlette.testclient")
    from memorymaster.surfaces import mcp_http

    monkeypatch.setenv("MEMORYMASTER_MCP_AUTH_MODE", "local-trusted")
    return starlette_testclient.TestClient(mcp_http.create_http_app(token="secret", db_target=db_target, workspace="."))


AUTH = {"Authorization": "Bearer secret"}


def test_the_route_serves_candidates_for_a_file_hint_and_never_runs_the_prompt_path(monkeypatch):
    got = {}
    monkeypatch.setattr(file_hint, "lookup", lambda q, **kw: got.update(q=q, **kw) or [dict(CLAIM)])
    monkeypatch.setattr(context_hook, "recall", lambda *a, **k: pytest.fail("the prompt path must not run"))
    client = _client(monkeypatch)
    hint = {"query": "hook_log.py core", "tail": ["core", "hook_log.py"]}
    body = {"file_hint": hint, "hook_data": {"cwd": "G:/repo"}}
    assert client.post("/hook/recall", json=body).status_code == 401
    response = client.post("/hook/recall", json=body, headers=AUTH)
    assert response.status_code == 200 and response.json() == {"claims": [CLAIM]}
    assert got == {"q": "hook_log.py core", "db_path": "test.db", "hook_data": {"cwd": "G:/repo"},
                   "tail": ["core", "hook_log.py"]}
    assert client.post("/hook/recall", json={"file_hint": [1]}, headers=AUTH).status_code == 400


def test_a_trivial_looking_lookup_is_not_treated_as_a_trivial_prompt(monkeypatch):
    monkeypatch.setattr(file_hint, "lookup", lambda q, **kw: [dict(CLAIM)])
    response = _client(monkeypatch).post("/hook/recall", json={"file_hint": {"query": "ok.py"}, "hook_data": {"cwd": "x"}},
                                         headers=AUTH)
    assert response.json() == {"claims": [CLAIM]}


def test_the_route_stays_silent_for_a_switched_off_lane(monkeypatch, tmp_path):
    config = tmp_path / "recall-hook.json"
    config.write_text(json.dumps({"disabled_cwds": ["whatsappbot-final"]}), encoding="utf-8")
    monkeypatch.setenv("MEMORYMASTER_RECALL_HOOK_CONFIG", str(config))
    monkeypatch.setattr(file_hint, "lookup", lambda q, **kw: [dict(CLAIM)])
    client = _client(monkeypatch)
    off = client.post("/hook/recall", json={"file_hint": {"query": "a.py x"}, "hook_data": {"cwd": "G:/x/whatsappbot-final"}},
                      headers=AUTH)
    assert off.json() == {"claims": []}


def test_end_to_end_the_route_finds_the_claim_in_a_real_database(monkeypatch, db, tmp_path):
    path, ids = db
    client = _client(monkeypatch, db_target=path)
    response = client.post("/hook/recall", json={"file_hint": {"query": "hook_log.py core"},
                                                 "hook_data": {"cwd": str(tmp_path / "pubgclone")}}, headers=AUTH)
    assert [c["id"] for c in response.json()["claims"]] == [ids["mine"]]
    assert spool.claim_files(path) == []


def test_the_client_never_sends_the_lookup_as_a_query_or_the_session():
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"claims": []}'

    def opener(request, timeout):
        seen.update(body=json.loads(request.data), timeout=timeout)
        return _Resp()

    env = {"MEMORYMASTER_MCP_HTTP_TOKEN": "t", "MEMORYMASTER_SHARED_MCP_URL": "http://127.0.0.1:1/mcp"}
    answer = remote.remote_file_hint("a.py core", "G:/repo", ["core", "a.py"], environ=env, opener=opener)
    assert answer == remote.RemoteHint([], True)
    assert seen["body"] == {"file_hint": {"query": "a.py core", "tail": ["core", "a.py"]},
                            "hook_data": {"cwd": "G:/repo"}}
    assert seen["timeout"] == remote.FILE_HINT_TIMEOUT_SECONDS < 4.0
    off = {"MEMORYMASTER_HOOK_RECALL_REMOTE": "0"}  # not environ={}: on Windows the token falls back to the registry
    assert remote.remote_file_hint("a", "b", ["b"], environ=off, opener=opener) == remote.RemoteHint(None, False)
