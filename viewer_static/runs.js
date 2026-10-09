// Train tab -> Runs sub-page (docs/CODEBOOK_PLAN.md U2): every training run of the active
// scheme, across engines, newest first: when, which engine, the codebook version it was
// trained under (and whether the codebook has changed since), what counted as a negative,
// and per-theme labels and metrics, with pin / unpin. Also houses what moved out of the
// Models sidebar, unchanged: the full-corpus export (model.js binds its button) and the
// Storage panel (storage.js), which loads the first time its section is opened, since
// measuring the database can take seconds.
//
// Reads GET /api/runs (coding_store/runs.py). Engine names come from model.js's
// ENGINE_LABELS (filled from GET /api/engines). Reuses storage.js's togglePin() and
// loadStorageReport(), model.js's NEGATIVE_MODE_LABELS, app.js's addSubpageListener()/
// currentSubpage()/escapeHtml(), and coding.js's codingState (the active scheme). Loaded
// after codebook.js, which registers the Train sub-tabs.

const runsState = {
  data: null, // last GET /api/runs response
  expanded: new Set(), // model_versions whose per-theme rows are open
  request: 0, // bumped per load, so a slow response for an old scheme is dropped
};

async function loadRuns() {
  const schemeId = codingState.activeSchemeId;
  const scheme = (codingState.schemes || []).find((s) => s.scheme_id === schemeId);
  document.getElementById("runsSchemeLabel").textContent = scheme ? `Scheme: ${scheme.name}` : "";
  const statusEl = document.getElementById("runsStatus");
  const request = ++runsState.request;
  if (!schemeId) {
    runsState.data = null;
    statusEl.textContent = "Pick a coding scheme first (Code tab).";
    renderRuns();
    return;
  }
  statusEl.textContent = "";
  await engineRegistryReady; // model.js: engine names for the table
  let data;
  try {
    data = await (await fetch(`/api/runs?scheme_id=${encodeURIComponent(schemeId)}`)).json();
  } catch (err) {
    if (request === runsState.request) statusEl.textContent = "Request failed — is the server running?";
    return;
  }
  if (request !== runsState.request) return;
  if (data.error) {
    statusEl.textContent = "Error: " + data.error;
    return;
  }
  runsState.data = data;
  renderRuns();
}

function shortCodebookVersion(version) {
  return version ? version.replace(/^sha256:/, "").slice(0, 10) : "";
}

function codebookCellHtml(run, current) {
  if (!run.codebook_version) {
    return '<span class="hint" title="Trained before runs recorded their codebook version">not recorded</span>';
  }
  const same = run.codebook_version === current;
  return `<code title="${escapeHtml(run.codebook_version)}">${escapeHtml(shortCodebookVersion(run.codebook_version))}</code> ` +
    (same ? '<span class="runs-cb-current">current</span>'
      : '<span class="runs-cb-changed" title="The codebook (definitions, criteria, notes or flagged examples) ' +
        'has been edited since this run">changed since</span>');
}

const formatMetric = (v, digits = 2) => (v === null || v === undefined ? "—" : v.toFixed(digits));

function runRowHtml(run, current) {
  const open = runsState.expanded.has(run.model_version);
  const trainedAt = run.trained_at ? run.trained_at.slice(0, 16).replace("T", " ") : "—";
  const pinTitle = run.pinned && run.pin_note ? ` title="${escapeHtml(run.pin_note)}"` : "";
  const row =
    `<tr class="runs-row${run.pinned ? " runs-pinned" : ""}" data-version="${escapeHtml(run.model_version)}">` +
    `<td><button type="button" class="runs-expand" aria-expanded="${open}" ` +
    `title="Per-theme labels and metrics">${open ? "▾" : "▸"}</button></td>` +
    `<td>${escapeHtml(trainedAt)}</td>` +
    `<td>${escapeHtml(ENGINE_LABELS[run.engine] || run.engine || "?")}</td>` +
    `<td>${codebookCellHtml(run, current)}</td>` +
    `<td>${escapeHtml(NEGATIVE_MODE_LABELS[run.negative_mode] || run.negative_mode || "—")}</td>` +
    `<td>${run.n_themes}</td>` +
    `<td>${formatMetric(run.mean_f1)}</td>` +
    `<td>${formatMetric(run.mean_pr_auc)}</td>` +
    `<td><button type="button" class="btn-secondary runs-pin-btn"${pinTitle}>${run.pinned ? "Unpin" : "Pin"}</button></td>` +
    `</tr>`;
  if (!open) return row;
  const themeRows = run.themes.map((t) =>
    `<tr><td>${escapeHtml(t.name)}${t.status !== "active" ? ` <span class="hint">(${escapeHtml(t.status)})</span>` : ""}</td>` +
    `<td>${t.n_pos}</td><td>${t.n_neg}</td>` +
    `<td>${formatMetric(t.precision)}</td><td>${formatMetric(t.recall)}</td>` +
    `<td>${formatMetric(t.f1)}</td><td>${formatMetric(t.pr_auc)}</td><td>${formatMetric(t.threshold, 3)}</td></tr>`,
  ).join("");
  return row +
    `<tr class="runs-detail"><td></td><td colspan="8">` +
    `<div class="hint">Run ${escapeHtml(run.model_version)}. Positives and negatives are the labels each theme was trained on; ` +
    `metrics are cross-validated.</div>` +
    `<table class="runs-theme-table"><thead><tr><th>Theme</th><th>Positives</th><th>Negatives</th>` +
    `<th>Precision</th><th>Recall</th><th>F1</th><th>PR-AUC</th><th>Threshold</th></tr></thead>` +
    `<tbody>${themeRows}</tbody></table></td></tr>`;
}

function renderRuns() {
  const tbody = document.getElementById("runsTable").querySelector("tbody");
  const versionEl = document.getElementById("runsCurrentVersion");
  const data = runsState.data;
  if (!data) {
    versionEl.textContent = "";
    tbody.innerHTML = "";
    return;
  }
  const current = data.current_codebook_version;
  versionEl.innerHTML = `Current codebook version: <code title="${escapeHtml(current)}">` +
    `${escapeHtml(shortCodebookVersion(current))}</code>`;
  tbody.innerHTML = data.runs.length
    ? data.runs.map((run) => runRowHtml(run, current)).join("")
    : '<tr><td colspan="9" class="hint">No training runs for this scheme yet. Train one on the Models tab.</td></tr>';
}

function bindRunsEvents() {
  document.getElementById("runsTable").addEventListener("click", async (e) => {
    const row = e.target.closest("tr.runs-row");
    if (!row || !runsState.data) return;
    const version = row.dataset.version;
    if (e.target.closest(".runs-expand")) {
      if (runsState.expanded.has(version)) runsState.expanded.delete(version);
      else runsState.expanded.add(version);
      renderRuns();
    } else if (e.target.closest(".runs-pin-btn")) {
      const run = runsState.data.runs.find((r) => r.model_version === version);
      if (!run) return;
      const error = await togglePin(version, run.pinned); // storage.js; reloads this list
      if (error) document.getElementById("runsStatus").textContent = "Error: " + error;
    }
  });
  document.getElementById("runsStorageSection").addEventListener("toggle", (e) => {
    if (e.target.open && !storageState.report) loadStorageReport(); // storage.js
  });
  addSubpageListener("model", (name) => { if (name === "runs") loadRuns(); });
  // Extend the Train-tab entry hook (model.js, codebook.js): keep an open Runs page current.
  const enterTrain = window.onEnterModelTab;
  window.onEnterModelTab = () => {
    if (enterTrain) enterTrain();
    if (currentSubpage("model") === "runs") loadRuns();
  };
}

bindRunsEvents();
