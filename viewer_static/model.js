// Model tab: train/retrain the per-theme classifiers, then monitor and
// interpret them -- metrics + history, top predictive terms, and top-scored
// exemplar segments. Read-only: no accept/reject-into-codebook actions here.
//
// Shares app.js's `state` (for "View in Coding" dataset/interview navigation)
// and coding.js's `codingState` (for the pendingFocusSegmentId hook), and
// reuses `escapeHtml`/`switchTab` from app.js. Loaded after coding.js.

const modelState = {
  themes: [],
  progress: null, // /api/coding/progress (no dataset param -> all groups)
  latestRuns: {}, // {theme_id: latest model_run}
  selectedThemeId: null,
  interpret: null, // last /api/model/interpret response for the selected theme
  trainJobId: null,
  trainPollTimer: null,
};

// --- Theme list / sidebar --------------------------------------------------------------

async function loadModelThemes() {
  const [themesRes, runsRes, progressRes] = await Promise.all([
    fetch("/api/codebook/themes"),
    fetch("/api/coding/model_runs"),
    fetch("/api/coding/progress"),
  ]);
  modelState.themes = await themesRes.json();
  modelState.latestRuns = await runsRes.json();
  modelState.progress = await progressRes.json();
  renderModelBanner();
  renderModelThemeList();
}

function renderModelBanner() {
  const el = document.getElementById("modelBanner");
  if (Object.keys(modelState.latestRuns).length > 0) {
    el.textContent = "";
    return;
  }
  const byTheme = (modelState.progress && modelState.progress.by_theme) || {};
  let closest = null;
  modelState.themes.forEach((t) => {
    const count = (byTheme[t.theme_id] && byTheme[t.theme_id].count) || 0;
    if (!closest || count > closest.count) closest = { name: t.name, count };
  });
  const closestText = closest
    ? ` Closest: ${escapeHtml(closest.name)} (${closest.count}/25).`
    : "";
  el.innerHTML = `No classifiers trained yet — themes need 25+ coded segments.${closestText}`;
}

function renderModelThemeList() {
  const container = document.getElementById("modelThemeItems");
  container.innerHTML = "";
  const byTheme = (modelState.progress && modelState.progress.by_theme) || {};

  modelState.themes.forEach((t) => {
    const div = document.createElement("div");
    div.className = "model-theme-item" + (t.theme_id === modelState.selectedThemeId ? " selected" : "");
    div.dataset.themeId = t.theme_id;

    const run = modelState.latestRuns[t.theme_id];
    const count = (byTheme[t.theme_id] && byTheme[t.theme_id].count) || 0;
    const badge = run
      ? `<span class="model-badge">F1 ${run.f1.toFixed(2)} · PR-AUC ${run.pr_auc.toFixed(2)}</span>`
      : `<span class="model-theme-item-hint">${count}/25 coded</span>`;

    div.innerHTML =
      `<span class="theme-dot" style="background:${escapeHtml(t.color || "#6b7fd7")}"></span>` +
      `<span class="model-theme-item-name">${escapeHtml(t.name)}</span>` +
      badge;
    container.appendChild(div);
  });
}

// --- Selected theme detail: interpret + metrics + history ---------------------------------

async function selectModelTheme(themeId) {
  modelState.selectedThemeId = themeId;
  renderModelThemeList(); // update the "selected" highlight

  document.getElementById("modelDetailEmpty").classList.add("hidden");
  document.getElementById("modelDetailContent").classList.remove("hidden");
  document.getElementById("modelThemeName").textContent =
    (modelState.themes.find((t) => t.theme_id === themeId) || {}).name || themeId;

  const res = await fetch(`/api/model/interpret?theme_id=${encodeURIComponent(themeId)}`);
  modelState.interpret = await res.json();
  if (modelState.selectedThemeId !== themeId) return; // superseded by a newer selection
  renderModelDetail();

  if (modelState.interpret.trained) {
    renderMetricsHistory(themeId);
  } else {
    document.getElementById("modelHistoryTable").querySelector("tbody").innerHTML = "";
  }
}

function renderModelDetail() {
  const data = modelState.interpret;

  if (!data.trained) {
    document.getElementById("modelValueProp").innerHTML =
      `Not trained yet — ${data.progress.count || 0}/${data.min_positives} segments coded for this theme. ` +
      `Keep coding, then click "Train / retrain all eligible themes".`;
    document.getElementById("modelMetricsSection").classList.add("hidden");
    document.getElementById("modelTermsSection").classList.add("hidden");
    document.getElementById("modelExemplarsSection").classList.add("hidden");
    return;
  }

  document.getElementById("modelMetricsSection").classList.remove("hidden");
  document.getElementById("modelTermsSection").classList.remove("hidden");
  document.getElementById("modelExemplarsSection").classList.remove("hidden");

  const vp = data.value_prop;
  document.getElementById("modelValueProp").innerHTML =
    `You've coded <strong>${vp.coded_count}</strong> segment(s) for this theme. ` +
    `The model flags <strong>${vp.flagged_uncoded_count}</strong> more above its threshold ` +
    `(${vp.threshold.toFixed(3)}) that you haven't coded yet.`;

  const m = data.metrics;
  const caution = m.n_pos < data.trust_positives_floor
    ? ` <span class="hint">(below the ${data.trust_positives_floor}-positive rule of thumb — treat with caution)</span>`
    : "";
  document.getElementById("modelLatestMetrics").innerHTML =
    `<span>n_pos <strong>${m.n_pos}</strong></span>` +
    `<span>precision <strong>${m.precision.toFixed(2)}</strong></span>` +
    `<span>recall <strong>${m.recall.toFixed(2)}</strong></span>` +
    `<span>F1 <strong>${m.f1.toFixed(2)}</strong></span>` +
    `<span>PR-AUC <strong>${m.pr_auc.toFixed(2)}</strong></span>` +
    `<span>threshold <strong>${m.threshold.toFixed(3)}</strong></span>` +
    caution;

  renderTermsList("modelTermsPos", data.top_terms.positive);
  renderTermsList("modelTermsNeg", data.top_terms.negative);

  renderExemplars(data.exemplars);
}

function renderTermsList(elementId, terms) {
  const el = document.getElementById(elementId);
  el.innerHTML = terms.length
    ? terms.map((t) => `<li><span>${escapeHtml(t.term)}</span><span class="term-weight">${t.weight.toFixed(3)}</span></li>`).join("")
    : '<li class="hint">—</li>';
}

async function renderMetricsHistory(themeId) {
  const res = await fetch(`/api/coding/model_runs?theme_id=${encodeURIComponent(themeId)}`);
  const data = await res.json();
  const tbody = document.getElementById("modelHistoryTable").querySelector("tbody");
  tbody.innerHTML = (data.history || [])
    .map((r) => {
      const trainedAt = r.trained_at ? r.trained_at.slice(0, 19).replace("T", " ") : "—";
      return (
        `<tr><td>${escapeHtml(trainedAt)}</td><td>${escapeHtml(r.model_version)}</td>` +
        `<td>${r.n_pos}</td><td>${r.precision.toFixed(2)}</td><td>${r.recall.toFixed(2)}</td>` +
        `<td>${r.f1.toFixed(2)}</td><td>${r.pr_auc.toFixed(2)}</td><td>${r.threshold.toFixed(3)}</td></tr>`
      );
    })
    .join("");
}

function renderExemplars(exemplars) {
  const container = document.getElementById("modelExemplars");
  container.innerHTML = "";
  if (!exemplars.length) {
    container.innerHTML = '<div class="hint">No scored segments yet.</div>';
    return;
  }
  exemplars.forEach((ex) => {
    const div = document.createElement("div");
    div.className = "exemplar-card";
    const codedBadge = ex.already_coded
      ? '<span class="exemplar-coded-badge">already coded</span>'
      : '<span class="exemplar-coded-badge">not coded</span>';
    const roleBadge = ex.speaker_role === "interviewer"
      ? '<span class="seg-role-badge seg-role-interviewer">interviewer</span>'
      : "";
    div.innerHTML =
      `<div class="exemplar-header">` +
      `<span class="exemplar-score">${ex.score.toFixed(3)}</span>` +
      `<span>${escapeHtml(ex.group_name)} — ${escapeHtml(ex.person_name || "")}</span>` +
      roleBadge +
      codedBadge +
      `</div>` +
      `<div class="exemplar-text">${escapeHtml(ex.text)}</div>` +
      `<button class="btn-secondary exemplar-view-btn">View in Coding</button>`;
    div.querySelector(".exemplar-view-btn").addEventListener("click", () => viewExemplarInCoding(ex));
    container.appendChild(div);
  });
}

// --- View in Coding ---------------------------------------------------------------------

async function viewExemplarInCoding(exemplar) {
  await jumpToSegmentInCoding(exemplar);
}

// --- Train / retrain ----------------------------------------------------------------------

async function startTraining() {
  const btn = document.getElementById("trainModelsBtn");
  btn.disabled = true;
  document.getElementById("modelTrainStatus").textContent = "Starting…";

  let data;
  try {
    const res = await fetch("/api/model/train", { method: "POST" });
    data = await res.json();
  } catch (err) {
    document.getElementById("modelTrainStatus").textContent = "Request failed — is the server running?";
    btn.disabled = false;
    return;
  }
  if (data.error) {
    document.getElementById("modelTrainStatus").textContent = "Error: " + data.error;
    btn.disabled = false;
    return;
  }
  modelState.trainJobId = data.job_id;
  pollTrainJob();
}

function pollTrainJob() {
  clearTimeout(modelState.trainPollTimer);
  modelState.trainPollTimer = setTimeout(async () => {
    const res = await fetch(`/api/model/train_status?job_id=${encodeURIComponent(modelState.trainJobId)}`);
    const job = await res.json();
    const statusEl = document.getElementById("modelTrainStatus");

    if (job.status === "running") {
      const themeText = job.current_theme ? `: ${job.current_theme.name}` : "";
      statusEl.textContent = `Training theme ${job.themes_done}/${job.themes_total}${themeText}…`;
      pollTrainJob();
      return;
    }

    document.getElementById("trainModelsBtn").disabled = false;

    if (job.status === "done") {
      const nTrained = job.trained.length;
      const nSkipped = job.skipped.length;
      statusEl.textContent = `Done — ${nTrained} theme(s) trained, ${nSkipped} skipped (not enough codes yet).`;
      loadModelThemes().then(() => {
        if (modelState.selectedThemeId) selectModelTheme(modelState.selectedThemeId);
      });
    } else if (job.status === "error") {
      statusEl.textContent = "Error: " + job.error;
    }
  }, 1000);
}

// --- Events / init --------------------------------------------------------------------

function bindModelEvents() {
  document.getElementById("trainModelsBtn").addEventListener("click", startTraining);
  document.getElementById("modelThemeItems").addEventListener("click", (e) => {
    const item = e.target.closest(".model-theme-item");
    if (item) selectModelTheme(item.dataset.themeId);
  });
  window.onEnterModelTab = () => loadModelThemes();
}

function initModel() {
  bindModelEvents();
}

initModel();
