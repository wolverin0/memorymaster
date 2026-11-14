"""T-2548: the recall-block reuse metric answers "were the injected words used?" with controls, and prints counts only."""
from __future__ import annotations

import json
from pathlib import Path

from memorymaster.recall import residual, transcript_fallback
from scripts import recall_block_reuse as rb

SECRET_PROMPT = "please rotate sk-live-SECRET-9f3a for tenant bluegrass"
WORDS = ["quasar", "nebula", "gryphon", "mosaic", "obelisk", "tundra", "vortex", "zephyr", "cobalt", "lantern"]


def _line(rec: dict) -> str:
    return json.dumps(rec, separators=(",", ":"))


def recall_text(*claims: tuple[str, str], excerpts: tuple[str, ...] = ()) -> str:
    lines = ["[MemoryMaster recall]", "# Memory Context", "When a memory below informs your reply, cite its [mm-id].", ""]
    lines += [f"- [{cid}] {text}" for cid, text in claims]
    if excerpts:
        lines += [*transcript_fallback.HEADER, transcript_fallback.OPEN_TAG]
        lines += [f"- [2026-11-01 proj session=abc lines 1-2] {e}" for e in excerpts]
        lines.append(transcript_fallback.CLOSE_TAG)
    return "\n".join(lines)


def residual_text(*items: str) -> str:
    return "\n".join([residual.HEADER, "Open decisions:", *[f"- {i}" for i in items]])


def attachment(content: str, uid: str, *, event: str = "UserPromptSubmit", sidechain: bool = False,
               cwd: str = "C:\\Users\\someone\\work\\demo") -> dict:
    return {"parentUuid": None, "isSidechain": sidechain, "type": "attachment", "uuid": uid, "cwd": cwd,
            "timestamp": "2026-11-10T10:00:00.000Z",
            "attachment": {"type": "hook_additional_context", "hookEvent": event, "hookName": event,
                           "content": [content], "toolUseID": "t1"}}


def turn(prompt: str, answer: str, blocks: tuple[str, ...] = (), *, n: int, sidechain: bool = False,
         tool_noise: str = "") -> list[dict]:
    cwd = "C:\\Users\\someone\\work\\demo"
    recs = [{"parentUuid": None, "isSidechain": sidechain, "type": "user", "uuid": f"u{n}", "cwd": cwd,
             "timestamp": "2026-11-10T10:00:00.000Z", "message": {"role": "user", "content": prompt}}]
    recs += [attachment(b, f"a{n}-{i}", sidechain=sidechain) for i, b in enumerate(blocks)]
    if tool_noise:  # a tool call and its result: neither is answer text
        recs.append({"parentUuid": None, "isSidechain": sidechain, "type": "assistant", "uuid": f"t{n}", "cwd": cwd,
                     "message": {"role": "assistant", "content": [
                         {"type": "tool_use", "id": "x", "name": "Bash", "input": {"command": tool_noise}}]}})
        recs.append({"parentUuid": None, "isSidechain": sidechain, "type": "user", "uuid": f"r{n}", "cwd": cwd,
                     "message": {"role": "user", "content": [
                         {"type": "tool_result", "tool_use_id": "x", "content": tool_noise}]}})
    recs.append({"parentUuid": None, "isSidechain": sidechain, "type": "assistant", "uuid": f"s{n}", "cwd": cwd,
                 "message": {"role": "assistant", "content": [
                     {"type": "thinking", "thinking": "hidden"}, {"type": "text", "text": answer}]}})
    return recs


def write_session(root: Path, name: str, records: list[dict], *, junk: bool = False) -> Path:
    folder = root / "proj"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.jsonl"
    lines = [_line(r) for r in records]
    if junk:
        lines.insert(1, "{not json at all")
        lines.insert(2, '{"parentUuid":null,"isSidechain":false,"type":"user","message":')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def run(root: Path, **kw) -> dict:
    return rb.analyze(root, **kw)


def test_word_from_the_block_used_in_the_answer_is_counted(tmp_path):
    recs = turn("what next", f"We should look at {WORDS[0]} now", (recall_text(("mm-aa11", f"the {WORDS[0]} service restarts")),), n=1)
    recs += turn("other question", "unrelated text only", n=2)
    write_session(tmp_path, "s1", recs)
    t = run(tmp_path)["overall"]["claims"]
    assert (t["any_elig"], t["any_obs"]) == (1, 1)
    assert t["any_ctl_pairs"] >= 1 and t["any_ctl"] == 0


def test_a_word_that_is_also_in_the_prompt_is_not_counted(tmp_path):
    recs = turn(f"tell me about {WORDS[0]}", f"The {WORDS[0]} is fine", (recall_text(("mm-aa11", f"the {WORDS[0]} only")),), n=1)
    recs += turn("other question", "unrelated", n=2)
    write_session(tmp_path, "s1", recs)
    t = run(tmp_path)["overall"]["claims"]
    assert t["units"] == 1 and t["any_elig"] == 0 and t["any_obs"] == 0  # no distinctive word left to be "used"


def test_shuffled_pairing_is_lower_than_observed_when_use_is_real(tmp_path):
    recs = []
    for i, w in enumerate(WORDS):
        recs += turn(f"question number {i}", f"Answer mentions {w} explicitly",
                     (recall_text((f"mm-id{i:02d}", f"background about {w} component")),), n=i)
    write_session(tmp_path, "s1", recs)
    t = run(tmp_path, controls=5)["overall"]["claims"]
    assert t["any_rate_observed"] == 1.0
    assert t["any_rate_shuffled"] == 0.0 and t["any_ctl_pairs"] == 5 * len(WORDS)


def test_chance_overlap_shows_up_in_the_shuffle_not_as_use(tmp_path):
    # every answer shares the same common word with every block: observed == shuffled, no signal
    recs = []
    for i in range(6):
        recs += turn(f"q{i}", "the deployment pipeline is healthy", (recall_text((f"mm-c{i:03d}", "deployment pipeline notes")),), n=i)
    write_session(tmp_path, "s1", recs)
    t = run(tmp_path)["overall"]["claims"]
    assert t["any_rate_observed"] == t["any_rate_shuffled"] == 1.0


def test_strong_use_needs_min_words(tmp_path):
    claim = "alpha1 beta22 gamma33 delta44 epsilon"
    recs = turn("q", "alpha1 beta22 only", (recall_text(("mm-s001", claim)),), n=1)
    recs += turn("q2", "nothing", n=2)
    write_session(tmp_path, "s1", recs)
    two = run(tmp_path, min_words=2)["overall"]["claims"]
    three = run(tmp_path, min_words=3)["overall"]["claims"]
    assert two["strong_obs"] == 1 and three["strong_obs"] == 0 and three["any_obs"] == 1


def test_claims_excerpts_and_residual_are_split(tmp_path):
    block = recall_text(("mm-k001", f"claim about {WORDS[0]}"), excerpts=(f"old chat about {WORDS[1]}",))
    recs = turn("q", f"{WORDS[0]} and {WORDS[2]}", (block, residual_text(f"remember {WORDS[2]} decision")), n=1)
    recs += turn("q2", "nothing here", n=2)
    write_session(tmp_path, "s1", recs)
    o = run(tmp_path)["overall"]
    assert (o["claims"]["any_elig"], o["claims"]["any_obs"]) == (1, 1)
    assert (o["excerpts"]["any_elig"], o["excerpts"]["any_obs"]) == (1, 0)  # excerpt word is not in the answer
    assert (o["residual"]["any_elig"], o["residual"]["any_obs"]) == (1, 1)
    assert o["all"]["units"] == 2  # two attachments


def test_residual_and_recall_in_one_attachment_are_both_read(tmp_path):
    content = residual_text(f"remember {WORDS[2]} decision") + "\n\n" + recall_text(("mm-k001", f"claim about {WORDS[0]}"))
    recs = turn("q", f"{WORDS[0]} {WORDS[2]}", (content,), n=1) + turn("q2", "nothing", n=2)
    write_session(tmp_path, "s1", recs)
    o = run(tmp_path)["overall"]
    assert o["claims"]["any_obs"] == 1 and o["residual"]["any_obs"] == 1 and o["all"]["units"] == 1


def test_sidechain_is_ignored(tmp_path):
    recs = turn("q", f"uses {WORDS[0]}", (recall_text(("mm-sc01", f"about {WORDS[0]}")),), n=1, sidechain=True)
    recs += turn("q2", "plain", n=2)
    write_session(tmp_path, "s1", recs)
    assert run(tmp_path)["overall"]["all"]["units"] == 0


def test_only_the_memorymaster_attachment_is_read_among_several_hooks(tmp_path):
    other = "[Routing hint] consider using the " + WORDS[3] + " lane"
    recs = turn("q", f"{WORDS[3]} {WORDS[0]}", (other, recall_text(("mm-h001", f"about {WORDS[0]}"))), n=1)
    recs.append(attachment(recall_text(("mm-h002", f"{WORDS[4]} start")), "ss", event="SessionStart"))
    recs += turn("q2", "plain", n=2)
    write_session(tmp_path, "s1", recs)
    rep = run(tmp_path)
    assert rep["overall"]["all"]["units"] == 1
    assert rep["claims"]["ids_injected"] == 1  # mm-h001 only: not the routing hint, not the SessionStart block


def test_answer_excludes_tool_input_results_and_thinking(tmp_path):
    recs = turn("q", "plain words", (recall_text(("mm-t001", f"about {WORDS[0]}")),), n=1, tool_noise=f"echo {WORDS[0]}")
    recs += turn("q2", "plain", n=2)
    write_session(tmp_path, "s1", recs)
    assert run(tmp_path)["overall"]["claims"]["any_obs"] == 0


def test_malformed_lines_are_tolerated_and_counted(tmp_path):
    recs = turn("q", f"uses {WORDS[0]}", (recall_text(("mm-m001", f"about {WORDS[0]}")),), n=1) + turn("q2", "plain", n=2)
    write_session(tmp_path, "s1", recs, junk=True)
    rep = run(tmp_path)
    assert rep["overall"]["claims"]["any_obs"] == 1 and rep["input"]["files_read"] == 1
    assert rep["input"].get("malformed_lines", 0) >= 1


def test_cited_id_is_reported_separately_from_word_overlap(tmp_path):
    recs = turn("q", "As noted in [mm-z001] we proceed", (recall_text(("mm-z001", f"about {WORDS[0]}")),), n=1)
    recs += turn("q2", "plain", n=2)
    write_session(tmp_path, "s1", recs)
    t = run(tmp_path)["overall"]["claims"]
    assert (t["cite_elig"], t["cite_obs"], t["cite_ctl"]) == (1, 1, 0) and t["any_obs"] == 0


def test_never_used_ids_are_listed_by_injection_count(tmp_path):
    recs = []
    for i in range(3):
        block = recall_text(("mm-dead01", f"about {WORDS[0]}"), ("mm-live01", f"about {WORDS[1]}"))
        recs += turn(f"q{i}", f"plain {WORDS[1]}", (block,), n=i)
    write_session(tmp_path, "s1", recs)
    claims = run(tmp_path)["claims"]
    assert claims["ids_never_used"] == 1
    assert claims["top_never_used"] == [{"id": "mm-dead01", "injected": 3}]


def test_session_without_other_turns_uses_project_pool(tmp_path):
    write_session(tmp_path, "solo", turn("q", f"uses {WORDS[0]}", (recall_text(("mm-p001", f"about {WORDS[0]}")),), n=1))
    write_session(tmp_path, "other", turn("q2", "completely different answer", n=2) + turn("q3", "another one", n=3))
    t = run(tmp_path)["overall"]["claims"]
    assert t["any_obs"] == 1 and t["any_ctl_pairs"] >= 1 and t["any_ctl"] == 0


def test_output_has_counts_and_ids_only(tmp_path, capsys):
    recs = turn(SECRET_PROMPT, "answer with confidential-answer-text and quasar",
                (recall_text(("mm-o001", "quasar secretclaimbody")),), n=1) + turn("q2", "plain", n=2)
    write_session(tmp_path, "s1", recs)
    for argv in (["--root", str(tmp_path)], ["--root", str(tmp_path), "--json"]):
        assert rb.main(argv) == 0
        out = capsys.readouterr().out
        for forbidden in ("sk-live", "bluegrass", "confidential", "secretclaimbody", "someone", "Users"):
            assert forbidden not in out
    assert "demo" in out  # the project name (last cwd segment) is allowed


def test_json_is_valid_and_out_file_written(tmp_path, capsys):
    write_session(tmp_path, "s1", turn("q", "a", (recall_text(("mm-j001", "quasar")),), n=1) + turn("q2", "b", n=2))
    out = tmp_path / "r.json"
    assert rb.main(["--root", str(tmp_path), "--json", "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out) == json.loads(out.read_text(encoding="utf-8"))


def test_transcripts_are_not_modified(tmp_path):
    path = write_session(tmp_path, "s1", turn("q", f"uses {WORDS[0]}", (recall_text(("mm-r001", WORDS[0])),), n=1))
    before = path.read_bytes()
    run(tmp_path)
    assert path.read_bytes() == before
