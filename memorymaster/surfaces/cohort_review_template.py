"""Standalone local human-review page for immutable Dreaming cohorts."""

REVIEW_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MemoryMaster human review</title>
<style>
*{box-sizing:border-box}
:root{color-scheme:dark;font-family:"Segoe UI",system-ui,-apple-system,sans-serif}
body{margin:0;background:#0f172a;color:#e2e8f0;line-height:1.45}
main{max-width:1100px;margin:0 auto;padding:20px}
.header{display:flex;align-items:center;gap:14px;padding-bottom:16px;border-bottom:1px solid #1e293b}
.logo{width:38px;height:38px;display:grid;place-items:center;border-radius:10px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);font-weight:700;color:#fff}
h1,h2,h3{margin:0;color:#f8fafc;letter-spacing:-.02em}
h1{font-size:1.4rem}.subtitle,.muted{color:#94a3b8;font-size:.86rem}
.header .badge{margin-left:auto}.badge{border:1px solid #475569;border-radius:999px;padding:3px 9px;color:#cbd5e1;font-size:.75rem}
section,.card{background:#1e293b;border:1px solid #334155;border-radius:12px;padding:16px;margin-top:16px;min-width:0}
.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.toolbar>*{min-height:34px;min-width:0;max-width:100%}.toolbar>label{flex-wrap:wrap}
label{display:flex;gap:8px;align-items:center;color:#cbd5e1;font-size:.86rem}
input,select,textarea,button{font:inherit;border-radius:7px;border:1px solid #475569;background:#0f172a;color:#e2e8f0}
input,select,textarea{min-width:0;max-width:100%;padding:7px 9px}input[type=file]{max-width:100%}textarea{width:100%;min-height:90px;resize:vertical}
button{padding:7px 12px;cursor:pointer}button:hover:not(:disabled){background:#334155;border-color:#64748b}
button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:3px solid #93c5fd;outline-offset:2px}
button.primary{background:#2563eb;border-color:#3b82f6;color:#fff}button.primary:hover:not(:disabled){background:#1d4ed8}
button:disabled{cursor:not-allowed;opacity:.45}
.notice{padding:10px;border-radius:8px;background:#172554;border:1px solid #1d4ed8;color:#bfdbfe}
.error{background:#450a0a;border-color:#dc2626;color:#fecaca}.success{background:#052e16;border-color:#16a34a;color:#bbf7d0}
.summary{display:flex;gap:18px;flex-wrap:wrap;margin-top:12px}.stat{display:flex;flex-direction:column}.stat strong{font-size:1.15rem;color:#f8fafc}.stat span{font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;color:#94a3b8}
.review-head{display:flex;justify-content:space-between;gap:10px;align-items:start;flex-wrap:wrap}.progress{color:#cbd5e1;font-size:.88rem}
.memory-text{font-size:1.25rem;line-height:1.35;margin:14px 0;color:#f8fafc;overflow-wrap:anywhere}
.meta{display:flex;gap:8px;flex-wrap:wrap;color:#94a3b8;font-size:.8rem}.mono{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;overflow-wrap:anywhere}
.evidence{margin:16px 0 0;padding:0;list-style:none;display:grid;gap:10px}.evidence li{min-width:0;overflow-wrap:anywhere;border-left:3px solid #3b82f6;padding:9px 12px;background:#0f172a;border-radius:0 7px 7px 0}.evidence strong{display:block;color:#93c5fd;font-size:.8rem}.quote{margin:8px 0 0;padding:8px 10px;border-left:2px solid #64748b;color:#cbd5e1;font-size:.86rem;overflow-wrap:anywhere}.evidence details{margin-top:8px;color:#94a3b8;font-size:.8rem}.evidence summary{cursor:pointer;color:#93c5fd}
.judgments{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-top:16px}.judgments fieldset{min-width:0;border:1px solid #334155;border-radius:8px;margin:0;padding:10px}.judgments legend{padding:0 5px;color:#f1f5f9;font-weight:600;font-size:.86rem}.choices{display:flex;gap:12px;flex-wrap:wrap}.choices label{cursor:pointer}.choices input{accent-color:#60a5fa}
.scope-correction{margin-top:10px}.scope-correction[hidden]{display:none}.scope-correction input{width:100%}
.decision{display:flex;gap:12px;flex-wrap:wrap;margin-top:16px}.decision label{cursor:pointer}.decision input{accent-color:#a78bfa}
.actions{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-top:16px}.actions .nav{display:flex;gap:8px}
.empty{text-align:center;padding:30px;color:#94a3b8}.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
@media(max-width:700px){main{padding:12px}.judgments{grid-template-columns:1fr}.header{align-items:start;flex-wrap:wrap}.header .badge{margin-left:0}.actions button{flex:1}.actions .nav{width:100%}}
</style>
</head>
<body>
<main>
<header class="header">
  <div class="logo" aria-hidden="true">M</div>
  <div><h1>Human review</h1><div class="subtitle">Review immutable Dreaming decisions locally</div></div>
  <span class="badge">Acceptance remains pending</span>
</header>

<section aria-labelledby="packet-heading">
  <h2 id="packet-heading">Review packet</h2>
  <p class="muted">Load a redacted JSON packet. Source evidence stays in this page and is never saved to local storage.</p>
  <div class="toolbar">
    <label for="packet-file">Packet JSON <input id="packet-file" type="file" accept="application/json,.json"></label>
    <label for="reviewer-name">Reviewer <input id="reviewer-name" type="text" autocomplete="name" placeholder="Your name"></label>
  </div>
  <div id="packet-status" class="notice" role="status" aria-live="polite">No packet loaded.</div>
  <div id="storage-status" class="muted" role="status" aria-live="polite"></div>
  <div id="packet-summary" class="summary" hidden>
    <div class="stat"><strong id="population-count">-</strong><span>population</span></div>
    <div class="stat"><strong id="capture-count">-</strong><span>selected captures</span></div>
    <div class="stat"><strong id="decision-count">-</strong><span>decisions</span></div>
    <div class="stat"><strong id="emitted-count">-</strong><span>emitted</span></div>
    <div class="stat"><strong id="required-count">-</strong><span>human reviews required</span></div>
  </div>
</section>

<section id="review-section" aria-labelledby="review-heading" hidden>
  <div class="review-head">
    <div><h2 id="review-heading">Decision review</h2><div id="progress" class="progress" role="status" aria-live="polite"></div></div>
    <label for="review-filter">Show <select id="review-filter"><option value="all">All</option><option value="unreviewed">Unreviewed</option><option value="draft">Draft / unsure</option><option value="complete">Complete</option></select></label>
  </div>
  <article id="memory-card" class="card" aria-live="polite">
    <div id="card-content">
      <div class="meta"><span id="record-id" class="mono"></span><span id="capture-id" class="mono"></span><span id="scope"></span><span id="action"></span></div>
      <p id="memory-text" class="memory-text"></p>
      <h3>Supporting evidence</h3>
      <ul id="evidence-list" class="evidence"></ul>
      <div id="ai-rationale-box" class="notice" hidden><strong>AI rationale</strong><p id="ai-rationale"></p></div>
      <div class="judgments">
        <fieldset><legend>Correctly supported?</legend><div class="choices"><label><input data-review-choice data-field="correct" type="radio" name="correct" value="yes">Yes</label><label><input data-review-choice data-field="correct" type="radio" name="correct" value="no">No</label><label><input data-review-choice data-field="correct" type="radio" name="correct" value="unsure">Unsure</label></div></fieldset>
        <fieldset><legend>Worth remembering / retain?</legend><div class="choices"><label><input data-review-choice data-field="useful" type="radio" name="useful" value="yes">Yes</label><label><input data-review-choice data-field="useful" type="radio" name="useful" value="no">No</label><label><input data-review-choice data-field="useful" type="radio" name="useful" value="unsure">Unsure</label></div></fieldset>
        <fieldset><legend>Still current?</legend><div class="choices"><label><input data-review-choice data-field="stillCurrent" type="radio" name="stillCurrent" value="yes">Yes</label><label><input data-review-choice data-field="stillCurrent" type="radio" name="stillCurrent" value="no">No</label><label><input data-review-choice data-field="stillCurrent" type="radio" name="stillCurrent" value="unsure">Unsure</label></div></fieldset>
        <fieldset><legend>Right scope?</legend><div class="choices"><label><input data-review-choice data-field="rightScope" type="radio" name="rightScope" value="yes">Yes</label><label><input data-review-choice data-field="rightScope" type="radio" name="rightScope" value="no">No</label><label><input data-review-choice data-field="rightScope" type="radio" name="rightScope" value="unsure">Unsure</label></div><div id="scope-correction" class="scope-correction" hidden><label for="corrected-scope">Corrected expected scope</label><input id="corrected-scope" type="text" placeholder="project:example"></div></fieldset>
      </div>
      <fieldset class="decision"><legend>Final decision</legend><label><input data-review-choice data-field="decision" type="radio" name="decision" value="accept">Accept</label><label><input data-review-choice data-field="decision" type="radio" name="decision" value="reject">Reject</label><label><input data-review-choice data-field="decision" type="radio" name="decision" value="unsure">Unsure / keep draft</label></fieldset>
      <label for="rationale" style="display:block;margin-top:12px">Rationale <textarea id="rationale" placeholder="Explain the decision and the evidence supporting it."></textarea></label>
      <div id="decision-help" class="muted" role="status" aria-live="polite"></div>
    </div>
  </article>
  <div id="no-visible-records" class="empty" hidden>No records match this filter.</div>
  <div class="actions"><div class="nav"><button id="previous" type="button">Previous</button><button id="next" type="button">Next</button></div><span id="export-status" class="muted" role="status" aria-live="polite"></span></div>
</section>

<section aria-labelledby="export-heading">
  <h2 id="export-heading">Export</h2>
  <p id="completion-status" class="notice">Load a packet to see completion and export options.</p>
  <div class="toolbar"><button id="export-jsonl" class="primary" type="button" disabled>Export complete JSONL</button><button id="export-drafts" type="button" disabled>Export drafts JSON</button></div>
  <p class="muted">Exports contain reviewer judgments only. Unsure and incomplete records are excluded from acceptance JSONL; exporting does not claim acceptance.</p>
</section>
</main>

<script type="application/json" id="preloaded-packet">__REVIEW_PACKET_JSON__</script>
<script>
(() => {
  "use strict";
  const STORAGE_PREFIX = "memorymaster:human-review:";
  const refs = {
    file: document.getElementById("packet-file"), reviewer: document.getElementById("reviewer-name"),
    packetStatus: document.getElementById("packet-status"), storageStatus: document.getElementById("storage-status"),
    summary: document.getElementById("packet-summary"), population: document.getElementById("population-count"),
    captures: document.getElementById("capture-count"), decisions: document.getElementById("decision-count"),
    emitted: document.getElementById("emitted-count"), required: document.getElementById("required-count"),
    reviewSection: document.getElementById("review-section"), filter: document.getElementById("review-filter"),
    progress: document.getElementById("progress"), card: document.getElementById("memory-card"),
    cardContent: document.getElementById("card-content"), recordId: document.getElementById("record-id"),
    captureId: document.getElementById("capture-id"), scope: document.getElementById("scope"), action: document.getElementById("action"),
    memory: document.getElementById("memory-text"), evidence: document.getElementById("evidence-list"),
    aiBox: document.getElementById("ai-rationale-box"), aiRationale: document.getElementById("ai-rationale"),
    scopeCorrection: document.getElementById("scope-correction"), correctedScope: document.getElementById("corrected-scope"),
    rationale: document.getElementById("rationale"), decisionHelp: document.getElementById("decision-help"),
    noRecords: document.getElementById("no-visible-records"), previous: document.getElementById("previous"), next: document.getElementById("next"),
    exportStatus: document.getElementById("export-status"), completion: document.getElementById("completion-status"),
    exportJsonl: document.getElementById("export-jsonl"), exportDrafts: document.getElementById("export-drafts"),
    preloaded: document.getElementById("preloaded-packet")
  };
  const state = {packet: null, records: [], judgments: Object.create(null), position: 0, filter: "all", reviewer: "", activeRecordId: null};
  const choiceFields = ["correct", "useful", "stillCurrent", "rightScope", "decision"];
  const choiceValues = new Set(["yes", "no", "unsure", "accept", "reject"]);

  function meaningful(value) { return typeof value === "string" && value.trim().length > 0; }
  function setText(node, value) { node.textContent = value == null ? "" : String(value); }
  function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
  function setNotice(node, message, kind) { setText(node, message); node.className = kind ? "notice " + kind : "notice"; }
  function nonNegativeInteger(value) { return Number.isInteger(value) && value >= 0; }

  function validatePacket(packet) {
    const errors = [];
    if (!packet || typeof packet !== "object" || Array.isArray(packet)) return {ok: false, errors: ["packet must be an object"]};
    if (packet.schema !== "memorymaster.human-review-packet.v1") errors.push("unsupported packet schema");
    if (!meaningful(packet.cohort_version)) errors.push("cohort_version is required");
    if (!/^[a-f0-9]{64}$/i.test(String(packet.cohort_fingerprint || ""))) errors.push("cohort_fingerprint must be SHA-256 hex");
    ["population", "selected_captures", "total_decisions", "emitted_decisions", "required_human_reviews"].forEach((name) => {
      if (!nonNegativeInteger(packet[name])) errors.push(name + " must be a non-negative integer");
    });
    if (packet.selected_captures > packet.population) errors.push("selected_captures exceeds population");
    if (packet.total_decisions < packet.emitted_decisions) errors.push("emitted_decisions exceeds total_decisions");
    if (!Array.isArray(packet.records)) { errors.push("records must be an array"); return {ok: false, errors}; }
    if (packet.records.length !== packet.emitted_decisions) errors.push("records must contain emitted_decisions records");
    const ids = new Set();
    packet.records.forEach((record, index) => {
      const prefix = "record " + (index + 1);
      if (!record || typeof record !== "object" || Array.isArray(record)) { errors.push(prefix + " must be an object"); return; }
      if (!meaningful(record.record_id) || ids.has(record.record_id)) errors.push(prefix + " has a missing or duplicate record_id");
      ids.add(record.record_id);
      if (!nonNegativeInteger(record.capture_id)) errors.push(prefix + " capture_id must be a non-negative integer");
      if (!meaningful(record.memory_text) || !meaningful(record.actual_scope) || !meaningful(record.actual_action)) errors.push(prefix + " needs memory_text, actual_scope and actual_action");
      if (!Array.isArray(record.evidence) || record.evidence.length === 0) errors.push(prefix + " needs supporting evidence");
      (record.evidence || []).forEach((item) => {
        if (!item || !meaningful(item.message_id) || !meaningful(item.text)) errors.push(prefix + " has invalid evidence");
      });
      if (!meaningful(record.evidence_quote)) errors.push(prefix + " needs evidence_quote");
    });
    return {ok: errors.length === 0, errors};
  }

  function emptyJudgment() { return {correct: null, useful: null, stillCurrent: null, rightScope: null, correctedScope: "", decision: null, rationale: ""}; }
  function normalizeJudgment(value) {
    const result = emptyJudgment();
    if (!value || typeof value !== "object" || Array.isArray(value)) return result;
    choiceFields.forEach((field) => { if (choiceValues.has(value[field])) result[field] = value[field]; });
    if (typeof value.correctedScope === "string") result.correctedScope = value.correctedScope.slice(0, 300);
    if (typeof value.rationale === "string") result.rationale = value.rationale.slice(0, 4000);
    return result;
  }
  function storageKey() { return STORAGE_PREFIX + state.packet.cohort_fingerprint; }
  function restoreSaved() {
    state.judgments = Object.create(null); state.reviewer = "";
    try {
      const saved = JSON.parse(window.localStorage.getItem(storageKey()) || "null");
      if (saved && typeof saved.reviewer === "string") state.reviewer = saved.reviewer.slice(0, 200);
      const known = new Set(state.records.map((record) => record.record_id));
      if (saved && saved.judgments && typeof saved.judgments === "object") Object.keys(saved.judgments).forEach((id) => {
        if (known.has(id)) state.judgments[id] = normalizeJudgment(saved.judgments[id]);
      });
      setText(refs.storageStatus, "Progress is saved locally for this cohort fingerprint.");
    } catch (error) { setText(refs.storageStatus, "Local progress storage is unavailable; exports still work."); }
    refs.reviewer.value = state.reviewer;
  }
  function saveProgress() {
    if (!state.packet) return;
    try {
      const known = new Set(state.records.map((record) => record.record_id));
      const judgments = Object.create(null);
      Object.keys(state.judgments).forEach((id) => { if (known.has(id)) judgments[id] = normalizeJudgment(state.judgments[id]); });
      window.localStorage.setItem(storageKey(), JSON.stringify({reviewer: state.reviewer, judgments}));
      setText(refs.storageStatus, "Progress saved locally; source evidence is not stored.");
    } catch (error) { setText(refs.storageStatus, "Could not save local progress; exports still work."); }
  }

  function judgmentFor(record) { if (!state.judgments[record.record_id]) state.judgments[record.record_id] = emptyJudgment(); return state.judgments[record.record_id]; }
  function activeRecord() { return state.records.find((record) => record.record_id === state.activeRecordId) || null; }
  function isComplete(record) {
    const judgment = judgmentFor(record);
    const choicesReady = ["correct", "useful", "stillCurrent", "rightScope"].every((field) => ["yes", "no"].includes(judgment[field]));
    const scopeReady = judgment.rightScope !== "no" || (meaningful(judgment.correctedScope) && judgment.correctedScope.trim() !== record.actual_scope);
    return choicesReady && scopeReady && ["accept", "reject"].includes(judgment.decision) && meaningful(state.reviewer) && meaningful(judgment.rationale);
  }
  function isStarted(record) {
    const judgment = judgmentFor(record);
    return choiceFields.some((field) => judgment[field] !== null) || meaningful(judgment.correctedScope) || meaningful(judgment.rationale);
  }
  function visibleRecords() {
    return state.records.filter((record) => {
      if (state.filter === "unreviewed") return !isStarted(record);
      if (state.filter === "draft") return isStarted(record) && !isComplete(record);
      if (state.filter === "complete") return isComplete(record);
      return true;
    });
  }
  function setChoice(field, value) {
    document.querySelectorAll("[data-review-choice]").forEach((input) => { if (input.dataset.field === field) input.checked = input.value === value; });
  }
  function applyJudgment(judgment) {
    choiceFields.forEach((field) => setChoice(field, judgment[field]));
    refs.correctedScope.value = judgment.correctedScope || "";
    refs.scopeCorrection.hidden = judgment.rightScope !== "no";
    refs.correctedScope.disabled = judgment.rightScope !== "no";
    refs.rationale.value = judgment.rationale || "";
  }
  function renderEvidence(record) {
    clear(refs.evidence);
    record.evidence.forEach((item) => {
      const li = document.createElement("li");
      const title = document.createElement("strong"); setText(title, "Message " + item.message_id); li.appendChild(title);
      const quote = document.createElement("blockquote"); quote.className = "quote"; setText(quote, "Quoted evidence: " + record.evidence_quote); li.appendChild(quote);
      const quoteStart = item.text.indexOf(record.evidence_quote);
      const contextStart = quoteStart < 0 ? 0 : Math.max(0, quoteStart - 240);
      const contextEnd = quoteStart < 0 ? Math.min(item.text.length, 480) : Math.min(item.text.length, quoteStart + record.evidence_quote.length + 240);
      const context = document.createElement("div"); setText(context, (contextStart ? "..." : "") + item.text.slice(contextStart, contextEnd) + (contextEnd < item.text.length ? "..." : "")); li.appendChild(context);
      const details = document.createElement("details");
      const summary = document.createElement("summary"); setText(summary, "Show full source message"); details.appendChild(summary);
      const source = document.createElement("div"); setText(source, item.text); details.appendChild(source); li.appendChild(details);
      refs.evidence.appendChild(li);
    });
  }
  function renderCard(record) {
    if (!record) { state.activeRecordId = null; refs.card.hidden = true; refs.noRecords.hidden = false; return; }
    state.activeRecordId = record.record_id;
    refs.card.hidden = false; refs.noRecords.hidden = true;
    const judgment = judgmentFor(record);
    setText(refs.recordId, "Record " + record.record_id); setText(refs.captureId, "Capture " + record.capture_id);
    setText(refs.scope, "Actual scope: " + record.actual_scope); setText(refs.action, "Observed action: " + record.actual_action);
    setText(refs.memory, record.memory_text); renderEvidence(record);
    refs.aiBox.hidden = !meaningful(record.ai_rationale); setText(refs.aiRationale, record.ai_rationale || "");
    applyJudgment(judgment);
    if (judgment.decision === "unsure") setText(refs.decisionHelp, "Unsure remains a draft and is excluded from acceptance JSONL.");
    else if (["accept", "reject"].includes(judgment.decision) && !isComplete(record)) setText(refs.decisionHelp, "Finalize requires yes/no for every review choice, a reviewer name, and a meaningful rationale.");
    else if (isComplete(record)) setText(refs.decisionHelp, "Complete and ready to export.");
    else setText(refs.decisionHelp, "Unanswered or unsure choices keep this decision incomplete.");
  }
  function updateExportStatus() {
    if (!state.packet) return;
    const complete = state.records.filter((record) => isComplete(record)).length;
    const started = state.records.filter((record) => isStarted(record)).length;
    const excluded = state.records.length - complete;
    setText(refs.completion, complete + "/" + state.records.length + " complete; " + excluded + " incomplete or unsure will be excluded from acceptance JSONL. Human acceptance remains pending.");
    refs.exportJsonl.disabled = complete === 0; refs.exportDrafts.disabled = started === 0;
  }
  function render() {
    if (!state.packet) return;
    const records = visibleRecords();
    if (state.position >= records.length) state.position = Math.max(0, records.length - 1);
    const record = records[state.position];
    setText(refs.progress, records.length ? "Showing " + (state.position + 1) + " of " + records.length + " filtered records" : "No records match this filter.");
    refs.previous.disabled = !records.length || state.position === 0; refs.next.disabled = !records.length || state.position >= records.length - 1;
    renderCard(record); updateExportStatus();
  }

  function loadPacket(packet) {
    const validation = validatePacket(packet);
    if (!validation.ok) { state.packet = null; state.records = []; refs.reviewSection.hidden = true; refs.summary.hidden = true; setNotice(refs.packetStatus, "Packet rejected: " + validation.errors.join("; "), "error"); updateExportStatus(); return; }
    state.packet = packet; state.records = packet.records.slice(); state.position = 0; state.filter = "all"; refs.filter.value = "all";
    restoreSaved(); refs.reviewSection.hidden = false; refs.summary.hidden = false;
    setText(refs.population, packet.population); setText(refs.captures, packet.selected_captures); setText(refs.decisions, packet.total_decisions);
    setText(refs.emitted, packet.emitted_decisions); setText(refs.required, packet.required_human_reviews);
    setNotice(refs.packetStatus, "Loaded cohort " + packet.cohort_version + " (" + packet.emitted_decisions + " emitted decisions).", "success"); render();
  }
  function exportRow(record, judgment) {
    const row = {record_id: record.record_id, capture_id: record.capture_id, cohort_fingerprint: state.packet.cohort_fingerprint,
      label_origin: "human", reviewer: state.reviewer.trim(), human_accept: judgment.decision === "accept", rationale: judgment.rationale.trim(),
      semantic_sufficiency: judgment.correct === "yes", current_validity: judgment.stillCurrent === "yes", useful: judgment.useful === "yes",
      should_emit: judgment.useful === "yes", scope_affirmed: judgment.rightScope === "yes", actual_scope: record.actual_scope,
      expected_scope: judgment.rightScope === "no" ? judgment.correctedScope.trim() : record.actual_scope};
    return row;
  }
  function download(filename, text, type) {
    const url = URL.createObjectURL(new Blob([text], {type})); const link = document.createElement("a"); link.href = url; link.download = filename;
    document.body.appendChild(link); link.click(); link.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }
  function exportJsonl() {
    const rows = state.records.filter(isComplete).map((record) => exportRow(record, judgmentFor(record)));
    if (!rows.length) return; download("human-review-" + state.packet.cohort_version + ".jsonl", rows.map((row) => JSON.stringify(row)).join("\n") + "\n", "application/x-ndjson");
    setText(refs.exportStatus, "Exported " + rows.length + " complete human decisions. Acceptance is still a separate gate.");
  }
  function exportDrafts() {
    const records = state.records.filter(isStarted).map((record) => ({record_id: record.record_id, judgment: judgmentFor(record)}));
    if (!records.length) return; const payload = {schema: "memorymaster.human-review-drafts.v1", cohort_version: state.packet.cohort_version,
      cohort_fingerprint: state.packet.cohort_fingerprint, reviewer: state.reviewer.trim(), records};
    download("human-review-drafts-" + state.packet.cohort_version + ".json", JSON.stringify(payload, null, 2) + "\n", "application/json");
    setText(refs.exportStatus, "Exported " + records.length + " drafts; unsure decisions remain non-accepting.");
  }

  refs.file.addEventListener("change", async () => {
    const file = refs.file.files && refs.file.files[0]; if (!file) return;
    try { loadPacket(JSON.parse(await file.text())); } catch (error) { setNotice(refs.packetStatus, "Packet file could not be parsed as JSON.", "error"); }
  });
  refs.reviewer.addEventListener("input", () => { state.reviewer = refs.reviewer.value.slice(0, 200); saveProgress(); render(); });
  refs.filter.addEventListener("change", () => { state.filter = refs.filter.value; state.position = 0; render(); });
  refs.previous.addEventListener("click", () => { state.position -= 1; render(); }); refs.next.addEventListener("click", () => { state.position += 1; render(); });
  refs.cardContent.addEventListener("change", (event) => {
    const target = event.target; if (!target || !target.dataset || !target.hasAttribute("data-review-choice")) return;
    const record = activeRecord(); if (!record) return; judgmentFor(record)[target.dataset.field] = target.value; saveProgress(); render();
  });
  refs.correctedScope.addEventListener("input", () => { const record = activeRecord(); if (!record) return; judgmentFor(record).correctedScope = refs.correctedScope.value.slice(0, 300); saveProgress(); updateExportStatus(); });
  refs.rationale.addEventListener("input", () => { const record = activeRecord(); if (!record) return; judgmentFor(record).rationale = refs.rationale.value.slice(0, 4000); saveProgress(); updateExportStatus(); });
  refs.exportJsonl.addEventListener("click", exportJsonl); refs.exportDrafts.addEventListener("click", exportDrafts);
  try { const raw = refs.preloaded.textContent.trim(); if (raw && raw !== "null") loadPacket(JSON.parse(raw)); } catch (error) { setNotice(refs.packetStatus, "Preloaded packet could not be parsed.", "error"); }
})();
</script>
</body>
</html>
"""
