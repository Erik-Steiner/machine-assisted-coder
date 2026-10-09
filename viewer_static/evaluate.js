// Review tab -> Evaluate sub-page (docs/CODEBOOK_PLAN.md U6): how well one run does against
// the researcher's gold, per theme -- precision, recall and F1 with 95% Wilson intervals, and
// PR-AUC -- next to a second run on the same turns (by default the newest other TF-IDF run,
// the baseline every engine must beat), plus a calibration table.
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
};

// Queues' and Recode's score controls don't apply here; Evaluate has its own run pickers.
const EVAL_HIDDEN_CONTROLS = ["reviewEngineLabel", "reviewMinWordsLabel"];

function evalRunLabel(run, current) {
  const when = run.trained_at ? run.trained_at.slice(0, 16).replace("T", " ") : "?";
  const engine = ENGINE_LABELS[run.engine] || run.engine || "?";
  const codebook = !run.codebook_version ? "codebook not recorded"
    : run.codebook_version === current ? "current codebook"
      : `codebook ${shortCodebookVersion(run.codebook_version)} (changed since)`;
  return `${engine} · ${when} · ${codebook}`;
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

function bindEvaluateEvents() {
  document.getElementById("evalRun").addEventListener("change", (e) => {
    evalState.modelVersion = e.target.value;
    // Keep an explicit comparison, unless it is now the run itself.
    if (evalState.compare === evalState.modelVersion) evalState.compare = defaultCompare(evalState.runs.runs, evalState.modelVersion);
    renderEvalRunSelects();
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
