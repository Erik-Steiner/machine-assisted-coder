// Review tab -> Evaluate sub-page (docs/CODEBOOK_PLAN.md U6): how well one run does against
// the researcher's gold, per theme -- precision, recall and F1 with 95% Wilson intervals, and
// PR-AUC -- next to a second run on the same turns (by default the newest other TF-IDF run,
// the baseline every engine must beat), plus a calibration table. At the bottom, the Decide
// panel (U4): the researcher records what to do next, which closes the round
// (POST /api/rounds/decide, coding_store/rounds.py), and the app goes where that decision
// leads; below it, every closed round (GET /api/rounds), with the latest one withdrawable.
//
// Every rule lives server-side in coding_store/evaluation.py (GET /api/evaluate): gold is
// the turns of interviews marked read in full, segments roll up to turns, scores are out of
// fold. This file picks the runs and draws the answer. Runs come from GET /api/runs (the
// Train tab's Runs list), engine names from model.js's ENGINE_LABELS.
//
// Shares app.js's addSubpageListener()/currentSubpage()/escapeHtml(), runs.js's
// shortCodebookVersion(), model.js's ENGINE_LABELS/engineRegistryReady, and coding.js's
// codingState (the active scheme). Loaded after review.js, which registers the Review
// sub-tabs; review.js's applyReviewMode() and triage keys do nothing on this sub-tab.

const evalState = {
  runs: null, // last GET /api/runs response
  schemeId: null, // scheme the run lists were built for
  modelVersion: null, // the run being evaluated
  compare: null, // the comparison run, or "" for none (null = not chosen yet: use the default)
  request: 0, // bumped per load, so a slow response for an old selection is dropped
  rounds: null, // last GET /api/rounds response
};

// Queues' and Recode's score controls don't apply here; Evaluate has its own run pickers.
const EVAL_HIDDEN_CONTROLS = ["reviewEngineLabel", "reviewMinWordsLabel"];

function evalRunLabel(run, current) {
  const when = run.trained_at ? run.trained_at.slice(0, 16).replace("T", " ") : "?";
  const engine = ENGINE_LABELS[run.engine] || run.engine || "?";
  const codebook = !run.codebook_version ? "codebook not recorded"
    : run.codebook_version === current ? "current codebook"
      : `codebook ${shortCodebookVersion(run.codebook_version)} (changed since)`;
  return `Round ${run.round_no} · ${engine} · ${when} · ${codebook}`;
}

// The newest TF-IDF run other than the one being evaluated, or "" if there is none.
function defaultCompare(runs, modelVersion) {
  const other = runs.find((r) => r.engine === "tfidf" && r.model_version !== modelVersion);
  return other ? other.model_version : "";
}

function renderEvalRunSelects() {
  const { runs, current_codebook_version: current } = evalState.runs;
  const options = (selected) => runs.map((r) =>
    `<option value="${escapeHtml(r.model_version)}"${r.model_version === selected ? " selected" : ""}>` +
    `${escapeHtml(evalRunLabel(r, current))}</option>`).join("");
  document.getElementById("evalRun").innerHTML = options(evalState.modelVersion);
  document.getElementById("evalCompare").innerHTML =
    `<option value=""${evalState.compare ? "" : " selected"}>None</option>` + options(evalState.compare);
}

async function loadEvaluate() {
  const statusEl = document.getElementById("evalStatus");
  const schemeId = codingState.activeSchemeId;
  const request = ++evalState.request;
  if (!schemeId) {
    statusEl.textContent = "Pick a coding scheme first (Code tab).";
    renderEvaluation(null);
    return;
  }
  await engineRegistryReady; // model.js: engine names
  try {
    const runs = await (await fetch(`/api/runs?scheme_id=${encodeURIComponent(schemeId)}`)).json();
    if (request !== evalState.request) return;
    if (runs.error) throw new Error(runs.error);
    if (evalState.schemeId !== schemeId) { // a new scheme: start from its defaults
      evalState.schemeId = schemeId;
      evalState.modelVersion = null;
      evalState.compare = null;
    }
    evalState.runs = runs;
  } catch (err) {
    if (request === evalState.request) statusEl.textContent = "Couldn't load runs: " + err.message;
    return;
  }
  const runs = evalState.runs.runs;
  if (!runs.length) {
    statusEl.textContent = "No training runs for this scheme yet. Train one in Train › Models.";
    document.getElementById("evalRun").innerHTML = "";
    document.getElementById("evalCompare").innerHTML = "";
    renderEvaluation(null);
    return;
  }
  if (!runs.some((r) => r.model_version === evalState.modelVersion)) evalState.modelVersion = runs[0].model_version;
  if (evalState.compare === null || (evalState.compare && !runs.some((r) => r.model_version === evalState.compare))) {
    evalState.compare = defaultCompare(runs, evalState.modelVersion);
  }
  renderEvalRunSelects();
  loadRounds();
  await loadEvaluation(request);
}

async function loadEvaluation(request = ++evalState.request) {
  const statusEl = document.getElementById("evalStatus");
  statusEl.textContent = "Evaluating… (the first time, the gold interviews' transcripts are read from disk)";
  const params = `scheme_id=${encodeURIComponent(evalState.schemeId)}` +
    `&model_version=${encodeURIComponent(evalState.modelVersion)}` +
    (evalState.compare ? `&compare=${encodeURIComponent(evalState.compare)}` : "");
  let data;
  try {
    data = await (await fetch(`/api/evaluate?${params}`)).json();
  } catch (err) {
    if (request === evalState.request) statusEl.textContent = "Request failed — is the server running?";
    return;
  }
  if (request !== evalState.request) return;
  if (data.error) {
    statusEl.textContent = "Error: " + data.error;
    renderEvaluation(null);
    return;
  }
  statusEl.textContent = "";
  renderEvaluation(data);
}

const evalPct = (v) => (v === null || v === undefined ? "—" : v.toFixed(2));
const evalCi = (ci) => (ci ? ` <span class="eval-ci">[${ci[0].toFixed(2)}, ${ci[1].toFixed(2)}]</span>` : "");

const EVAL_NOTES = {
  no_gold: "No judged interviews yet: none marked read in full since this theme was created. " +
    "Mark interviews “fully coded” after reading them (Code tab).",
  scores_pruned: "This run's scores were deleted in Storage (Train › Runs); pick a newer run.",
  no_scored_gold: "The gold interviews have no passages this run scored.",
};

function evalThemeRowHtml(t, hasCompare) {
  const name = escapeHtml(t.name) + (t.status !== "active" ? ` <span class="hint">(${escapeHtml(t.status)})</span>` : "");
  if (!t.run) {
    return `<tr><td>${name}</td><td colspan="${hasCompare ? 6 : 5}" class="hint">${escapeHtml(EVAL_NOTES[t.note] || "")}</td></tr>`;
  }
  const m = t.run;
  const c = t.compare;
  const compareCell = !hasCompare ? "" : "<td>" + (c
    ? `${evalPct(c.f1)} · ${evalPct(c.pr_auc)}` + (c.n_units !== t.n_units ? ` <span class="hint">(${c.n_units} turns)</span>` : "")
    : '<span class="hint">not trained</span>') + "</td>";
  const fewPositives = t.n_pos < 10 ? ' title="Fewer than 10 positive turns: read the intervals, not the point estimates"' : "";
  return `<tr><td>${name}</td>` +
    `<td${fewPositives}>${t.n_pos} / ${t.n_units}${t.n_pos < 10 ? " <span class=\"eval-few\">few</span>" : ""}</td>` +
    `<td>${evalPct(m.precision)}${evalCi(m.precision_ci)}</td>` +
    `<td>${evalPct(m.recall)}${evalCi(m.recall_ci)}</td>` +
    `<td>${evalPct(m.f1)}</td><td>${evalPct(m.pr_auc)}</td>` +
    `${compareCell}</tr>`;
}

function renderEvaluation(data) {
  const tbody = document.getElementById("evalTable").querySelector("tbody");
  const calBody = document.getElementById("evalCalibration").querySelector("tbody");
  const goldEl = document.getElementById("evalGold");
  const notesEl = document.getElementById("evalNotes");
  if (!data) {
    tbody.innerHTML = "";
    calBody.innerHTML = "";
    goldEl.textContent = "";
    notesEl.textContent = "";
    return;
  }
  const g = data.gold;
  goldEl.innerHTML =
    `Gold: every turn of the <strong>${g.read_in_full_interviews}</strong> interview(s) marked read in full ` +
    `(confirmed, or marked “fully coded” right after reading), for the themes each mark covers. A turn is the ` +
    `theme if you coded any of its passages. This gold isn't blind or random: you chose which interviews to read.` +
    (g.unconfirmed_marks
      ? ` ${g.unconfirmed_marks} bulk “fully coded” mark(s) aren't confirmed yet and don't count; confirm them in ` +
        `the Code tab's Datasets panel.`
      : "");
  const hasCompare = !!data.compare_version;
  document.getElementById("evalCompareHead").classList.toggle("hidden", !hasCompare);
  tbody.innerHTML = data.themes.map((t) => evalThemeRowHtml(t, hasCompare)).join("");

  const notes = [
    "Scores are out of fold where the run trained on a passage, so the run never saw the passage it is scored on. " +
    "Each theme's threshold is the run's own, chosen to maximize F1 on those same scores, so the point " +
    "estimates are slightly optimistic.",
  ];
  if (data.units_by_segment) {
    notes.push(`${data.units_by_segment} gold passage(s) are counted as segments, not turns: their dataset ` +
      "couldn't be read from disk.");
  }
  if (data.unit !== "turn") notes.push("Evaluated per segment: turn information isn't available.");
  notesEl.textContent = notes.join(" ");

  calBody.innerHTML = data.calibration.map((b) =>
    `<tr><td>${escapeHtml(b.band)}</td><td>${b.n}</td><td>${evalPct(b.share_positive)}</td></tr>`).join("");
}

// --- Decide panel and round history (U4) ----------------------------------------------

async function loadRounds() {
  const schemeId = evalState.schemeId;
  let data;
  try {
    data = await (await fetch(`/api/rounds?scheme_id=${encodeURIComponent(schemeId)}`)).json();
  } catch (err) {
    return; // the panel just stays as it was
  }
  if (data.error || schemeId !== evalState.schemeId) return;
  evalState.rounds = data;
  renderRounds();
}

function renderRounds() {
  const data = evalState.rounds;
  const optionsEl = document.getElementById("evalDecisionOptions");
  if (!optionsEl.children.length) {
    optionsEl.innerHTML = data.decisions.map((d, i) =>
      `<label class="eval-decision"><input type="radio" name="evalDecision" value="${escapeHtml(d.id)}"` +
      `${i === 0 ? " checked" : ""}> ${escapeHtml(d.label)}</label>`).join("");
  }
  const open = data.open;
  document.getElementById("evalRoundLabel").textContent =
    `· round ${open.round_no} open, ${open.runs.length} run(s) trained in it so far`;
  renderRunRoundNote();
  const runLabel = (ref) => {
    const run = (evalState.runs?.runs || []).find((r) => r.model_version === ref);
    if (!run) return escapeHtml(ref || "—");
    return `${escapeHtml(ENGINE_LABELS[run.engine] || run.engine)} · ${escapeHtml(run.trained_at.slice(0, 16).replace("T", " "))}`;
  };
  const tbody = document.getElementById("evalRoundsTable").querySelector("tbody");
  tbody.innerHTML = data.rounds.length
    ? data.rounds.slice().reverse().map((r) =>
      `<tr><td>${r.round_no}</td><td>${escapeHtml(r.created_at.slice(0, 16).replace("T", " "))}</td>` +
      `<td>${escapeHtml(r.label)}</td><td>${runLabel(r.run_ref)}</td><td>${r.runs.length}</td>` +
      `<td>${escapeHtml(r.note || "")}</td></tr>`).join("")
    : '<tr><td colspan="6" class="hint">No decisions yet: this is round 1.</td></tr>';
  document.getElementById("evalWithdrawBtn").classList.toggle("hidden", !data.rounds.length);
}

// Says so when the run being decided on was trained in an earlier, closed round.
function renderRunRoundNote() {
  const el = document.getElementById("evalRunRoundNote");
  const run = (evalState.runs?.runs || []).find((r) => r.model_version === evalState.modelVersion);
  const open = evalState.rounds?.open;
  el.textContent = run && open && run.round_no !== open.round_no
    ? `The selected run was trained in round ${run.round_no}; the decision will close round ${open.round_no}.`
    : "";
}

async function recordDecision() {
  const statusEl = document.getElementById("evalDecideStatus");
  const picked = document.querySelector('input[name="evalDecision"]:checked');
  if (!picked || !evalState.modelVersion) {
    statusEl.textContent = "Pick a run and a decision first.";
    return;
  }
  statusEl.textContent = "Recording…";
  const res = await fetch("/api/rounds/decide", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      scheme_id: evalState.schemeId, decision: picked.value, run_ref: evalState.modelVersion,
      note: document.getElementById("evalDecisionNote").value,
    }),
  });
  const data = await res.json();
  if (data.error) {
    statusEl.textContent = "Error: " + data.error;
    return;
  }
  statusEl.textContent = `Round ${data.round_no} closed.`;
  document.getElementById("evalDecisionNote").value = "";
  await loadRounds();
  if (typeof refreshCycleStrips === "function") refreshCycleStrips(); // cycle.js
  // More labels opens the borderline queue: deciding those teaches the next model the most.
  if (data.next.tab === "review" && data.next.subpage === "queues") {
    reviewState.queueMode = "uncertain"; // review.js
    document.getElementById("reviewMode").value = "uncertain";
  }
  goToSubpage(data.next.tab, data.next.subpage);
}

async function withdrawDecision() {
  const latest = evalState.rounds?.rounds.slice(-1)[0];
  if (!latest) return;
  if (!confirm(`Withdraw round ${latest.round_no}'s decision (${latest.label})? Round ${latest.round_no} ` +
    "becomes the open round again. The decision stays in the record, marked withdrawn.")) return;
  const res = await fetch("/api/rounds/withdraw", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ scheme_id: evalState.schemeId }),
  });
  const data = await res.json();
  document.getElementById("evalDecideStatus").textContent = data.error
    ? "Error: " + data.error : `Round ${latest.round_no}'s decision withdrawn.`;
  await loadRounds();
  if (typeof refreshCycleStrips === "function") refreshCycleStrips();
}

function bindEvaluateEvents() {
  document.getElementById("evalDecideBtn").addEventListener("click", recordDecision);
  document.getElementById("evalWithdrawBtn").addEventListener("click", withdrawDecision);
  document.getElementById("evalRun").addEventListener("change", (e) => {
    evalState.modelVersion = e.target.value;
    // Keep an explicit comparison, unless it is now the run itself.
    if (evalState.compare === evalState.modelVersion) evalState.compare = defaultCompare(evalState.runs.runs, evalState.modelVersion);
    renderEvalRunSelects();
    renderRunRoundNote();
    loadEvaluation();
  });
  document.getElementById("evalCompare").addEventListener("change", (e) => {
    evalState.compare = e.target.value;
    loadEvaluation();
  });
  const showControls = (name) => {
    EVAL_HIDDEN_CONTROLS.forEach((id) => document.getElementById(id).classList.toggle("hidden", name === "evaluate"));
  };
  addSubpageListener("review", (name) => {
    showControls(name);
    if (name === "evaluate") loadEvaluate();
  });
  // Extend the Review-tab entry hook (review.js): keep an open Evaluate page current.
  const enterReview = window.onEnterReviewTab;
  window.onEnterReviewTab = () => {
    if (enterReview) enterReview();
    showControls(currentSubpage("review"));
    if (currentSubpage("review") === "evaluate") loadEvaluate();
  };
}

bindEvaluateEvents();
