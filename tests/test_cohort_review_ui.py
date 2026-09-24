"""Regression checks for the standalone local human-review page."""

from __future__ import annotations

import json
import shutil
import subprocess

from memorymaster.surfaces.cohort_review_template import REVIEW_HTML


def test_review_page_is_self_contained_and_uses_text_only_rendering():
    assert REVIEW_HTML.startswith("<!doctype html>")
    assert 'id="preloaded-packet"' in REVIEW_HTML
    assert "fetch(" not in REVIEW_HTML
    assert "<script src=" not in REVIEW_HTML
    assert "innerHTML" not in REVIEW_HTML
    assert "textContent" in REVIEW_HTML
    assert 'summary, "Show full source message"' in REVIEW_HTML


def test_review_events_persist_and_export_the_validator_contract():
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required for the review UI regression")
    packet = {
        "schema": "memorymaster.human-review-packet.v1",
        "cohort_version": "ui-test-v1",
        "cohort_fingerprint": "a" * 64,
        "population": 2,
        "selected_captures": 2,
        "total_decisions": 2,
        "emitted_decisions": 2,
        "required_human_reviews": 1,
        "records": [{
            "record_id": "__proto__",
            "capture_id": 7,
            "memory_text": "Remember this source-backed fact.",
            "actual_scope": "project:test",
            "actual_action": "add",
            "evidence": [{"message_id": "m1", "text": "Before quote. Frozen source quote. After quote."}],
            "evidence_quote": "Frozen source quote",
            "ai_rationale": "The source supports this memory.",
        }, {
            "record_id": "r2",
            "capture_id": 8,
            "memory_text": "Remember the second source-backed fact.",
            "actual_scope": "project:test",
            "actual_action": "add",
            "evidence": [{"message_id": "m2", "text": "Second frozen source quote."}],
            "evidence_quote": "Second frozen source quote.",
            "ai_rationale": "The second source supports this memory.",
        }],
    }
    rendered_html = REVIEW_HTML.replace("__REVIEW_PACKET_JSON__", json.dumps(packet, separators=(",", ":")))
    harness = r'''
const vm = require("node:vm");
const html = require("node:fs").readFileSync(0, "utf8");
const matches = [...html.matchAll(/<script(?: [^>]*)?>([\s\S]*?)<\/script>/g)];
if (matches.length !== 2) throw new Error("expected two scripts");
const source = matches[1][1];
class Node {
  constructor(tag) { this.tagName = tag; this.children = []; this.listeners = {}; this.dataset = {}; this.attributes = new Set();
    this.hidden = false; this.disabled = false; this.checked = false; this.value = ""; this.textContent = ""; this.className = ""; }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  fire(name, event = {target: this}) { if (this.listeners[name]) this.listeners[name](event); }
  appendChild(child) { this.children.push(child); this.firstChild = this.children[0]; return child; }
  removeChild(child) { this.children = this.children.filter((item) => item !== child); this.firstChild = this.children[0]; }
  remove() {}
  click() {}
  hasAttribute(name) { return this.attributes.has(name); }
}
const ids = ["packet-file", "reviewer-name", "packet-status", "storage-status", "packet-summary", "population-count",
  "capture-count", "decision-count", "emitted-count", "required-count", "review-section", "review-filter", "progress",
  "memory-card", "card-content", "record-id", "capture-id", "scope", "action", "memory-text", "evidence-list",
  "ai-rationale-box", "ai-rationale", "scope-correction", "corrected-scope", "rationale", "decision-help",
  "no-visible-records", "previous", "next", "export-status", "completion-status", "export-jsonl", "export-drafts",
  "preloaded-packet"];
const byId = Object.fromEntries(ids.map((id) => [id, new Node("div")]));
byId["packet-file"].files = [];
byId["preloaded-packet"].textContent = html.match(/<script type="application\/json" id="preloaded-packet">([\s\S]*?)<\/script>/)[1];
const radios = [];
for (const field of ["correct", "useful", "stillCurrent", "rightScope"]) {
  for (const value of ["yes", "no", "unsure"]) { const radio = new Node("input"); radio.dataset.field = field; radio.dataset.reviewChoice = "";
    radio.attributes.add("data-review-choice"); radio.value = value; radios.push(radio); }
}
for (const value of ["accept", "reject", "unsure"]) { const radio = new Node("input"); radio.dataset.field = "decision";
  radio.dataset.reviewChoice = ""; radio.attributes.add("data-review-choice"); radio.value = value; radios.push(radio); }
const store = new Map(); let lastBlob = null;
const document = {body: new Node("body"), createElement: (tag) => new Node(tag), getElementById: (id) => byId[id],
  querySelectorAll: () => radios};
const localStorage = {getItem: (key) => store.get(key) || null, setItem: (key, value) => store.set(key, value)};
class TestBlob { constructor(parts, options) { this.textValue = parts.join(""); this.type = options.type; } async text() { return this.textValue; } }
const URL = {createObjectURL: (blob) => { lastBlob = blob; return "blob:test"; }, revokeObjectURL: () => {}};
const window = {localStorage, URL, setTimeout: (fn) => fn()};
vm.runInNewContext(source, {window, document, Blob: TestBlob, URL, console});
function radio(field, value) { return radios.find((item) => item.dataset.field === field && item.value === value); }
function change(field, value) { byId["card-content"].fire("change", {target: radio(field, value)}); }
byId["reviewer-name"].value = "operator"; byId["reviewer-name"].fire("input");
change("useful", "yes");
const saved = JSON.parse(store.get("memorymaster:human-review:" + "a".repeat(64)));
if (!Object.prototype.hasOwnProperty.call(saved.judgments, "__proto__") || saved.judgments["__proto__"].useful !== "yes") throw new Error("empty data-review-choice marker did not persist safely");
change("correct", "yes"); change("stillCurrent", "yes"); change("rightScope", "yes"); change("decision", "accept");
byId["rationale"].value = "Checked the supporting source."; byId["rationale"].fire("input");
if (byId["export-jsonl"].disabled) throw new Error("complete review was not exportable");
byId["export-jsonl"].fire("click");
lastBlob.text().then((text) => { const row = JSON.parse(text.trim());
  for (const key of ["should_emit", "scope_affirmed", "actual_scope", "expected_scope"]) if (!(key in row)) throw new Error("missing " + key);
  if (!row.should_emit || !row.scope_affirmed || row.actual_scope !== "project:test" || row.expected_scope !== "project:test") throw new Error("scope/useful export mismatch");
  byId["next"].fire("click"); change("useful", "yes"); change("correct", "yes"); change("stillCurrent", "yes"); change("rightScope", "yes"); change("decision", "accept");
  byId["review-filter"].value = "draft"; byId["review-filter"].fire("change");
  byId["rationale"].value = "A"; byId["rationale"].fire("input"); byId["rationale"].value = "AB"; byId["rationale"].fire("input");
  const savedSecond = JSON.parse(store.get("memorymaster:human-review:" + "a".repeat(64)));
  if (savedSecond.judgments.r2.rationale !== "AB") throw new Error("draft typing moved to a different filtered record");
  change("rightScope", "no"); byId["corrected-scope"].value = "project:test"; byId["corrected-scope"].fire("input");
  byId["export-jsonl"].fire("click"); if (lastBlob.textValue.trim().split("\n").length !== 1) throw new Error("same-scope correction was accepted");
  byId["corrected-scope"].value = "project:other"; byId["corrected-scope"].fire("input");
  byId["export-jsonl"].fire("click"); if (lastBlob.textValue.trim().split("\n").length !== 2) throw new Error("different scope correction was rejected");
  change("useful", "unsure"); byId["export-jsonl"].fire("click");
  if (lastBlob.textValue.trim().split("\n").length !== 1) throw new Error("unsure review was exported");
}).catch((error) => { console.error(error.stack || error); process.exitCode = 1; });
'''
    result = subprocess.run([node, "-e", harness], input=rendered_html, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout


def test_rendered_packet_validation_rejects_malformed_capture_ids():
    node = shutil.which("node")
    if node is None:
        raise AssertionError("Node.js is required for the review UI regression")
    packet = {
        "schema": "memorymaster.human-review-packet.v1",
        "cohort_version": "ui-validation-v1",
        "cohort_fingerprint": "b" * 64,
        "population": 1,
        "selected_captures": 1,
        "total_decisions": 1,
        "emitted_decisions": 1,
        "required_human_reviews": 1,
        "records": [{
            "record_id": "r1",
            "capture_id": 7,
            "memory_text": "A memory.",
            "actual_scope": "project:test",
            "actual_action": "add",
            "evidence": [{"message_id": "m1", "text": "Source quote."}],
            "evidence_quote": "Source quote.",
        }],
    }
    rendered_html = REVIEW_HTML.replace("__REVIEW_PACKET_JSON__", json.dumps(packet, separators=(",", ":")))
    harness = r'''
const vm = require("node:vm");
const html = require("node:fs").readFileSync(0, "utf8");
const source = html.match(/<script(?: [^>]*)?>([\s\S]*?)<\/script>/g)[1]
  .replace(/^<script(?: [^>]*)?>/, "").replace(/<\/script>$/, "");
const start = source.indexOf("  function meaningful");
const end = source.indexOf("  function emptyJudgment");
const validators = source.slice(start, end) + "\nresult = validatePacket(packet);";
for (const invalid of [null, "7", -1]) {
  const packet = JSON.parse(html.match(/<script type="application\/json" id="preloaded-packet">([\s\S]*?)<\/script>/)[1]);
  packet.records[0].capture_id = invalid;
  const context = {packet, result: null}; vm.runInNewContext(validators, context);
  if (context.result.ok) throw new Error("malformed capture_id was accepted: " + String(invalid));
}
'''
    result = subprocess.run([node, "-e", harness], input=rendered_html, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr or result.stdout
