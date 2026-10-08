// Model tab: train/retrain the per-theme classifiers, then monitor and
// interpret them -- metrics + history, top predictive terms, and top-scored
// exemplar segments. Read-only: no accept/reject-into-codebook actions here.
//
// Shares app.js's `state` (for "View in Coding" dataset/interview navigation)
// and coding.js's `codingState` (for the pendingFocusSegmentId hook), and
// reuses `escapeHtml`/`switchTab`/`pollBackgroundJob` from app.js. Loaded after
// coding.js.

const modelState = {
  themes: [],
  progress: null, // /api/coding/progress (no dataset param -> all groups)
  latestRuns: {}, // {theme_id: latest TF-IDF model_run} -- the sidebar badges are always TF-IDF;
                  // the detail panel's engine toggle is what shows embedding runs
  selectedThemeId: null,
  selectedEngine: "tfidf", // "tfidf" | "embed" -- which engine's runs the detail panel shows
  history: [], // last /api/coding/model_runs?theme_id= response for the selected theme
  interpret: null, // last /api/model/interpret response for the selected theme
  trainJobId: null,
  trainPollTimer: null,
  embedTrainJobId: null,
  embedTrainPollTimer: null,
  installJobId: null,
  installPollTimer: null,
  installStartedAt: null,
};

// Mirrors coding_store/model_runs.py's ENGINE_PREFIXES -- keep the two in sync.
function engineOfVersion(modelVersion) {
  return modelVersion && modelVersion.startsWith("embed_v1_") ? "embed" : "tfidf";
}

// Display names for engine ids; shared with review.js (loaded after this file).
const ENGINE_LABELS = { tfidf: "TF-IDF", embed: "Embedding" };

// --- Theme list / sidebar --------------------------------------------------------------

async function loadModelThemes() {
  // codingState.activeSchemeId (set by the Coding tab's scheme switcher) is the
  // shared source of truth for which scheme's themes this sidebar shows -- Model
  // only reads it, never sets it. Training is scoped to the same scheme unless the
  // "every scheme" box is ticked -- see trainingScope().
  const schemePart = codingState.activeSchemeId
    ? `?scheme_id=${encodeURIComponent(codingState.activeSchemeId)}` : "";
  const [themesRes, runsRes, progressRes] = await Promise.all([
    fetch(`/api/codebook/themes${schemePart}`),
    fetch("/api/coding/model_runs?engine=tfidf"),
    fetch(`/api/coding/progress${schemePart}`),
  ]);
  modelState.themes = await themesRes.json();
  modelState.latestRuns = await runsRes.json();
  modelState.progress = await progressRes.json();
  renderModelBanner();
  renderModelThemeList();
  renderModelSchemeLabel();
}

function renderModelSchemeLabel() {
  const el = document.getElementById("modelSchemeLabel");
  if (!el) return;
  const scheme = (codingState.schemes || []).find((s) => s.scheme_id === codingState.activeSchemeId);
  el.textContent = scheme ? `Scheme: ${scheme.name}` : "";
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

  // Tree order with indented sub-codes, same as the Code tab's codebook list
  // (coding.js's themesByParent()) -- the API returns themes flat, sorted by depth.
  const byParent = themesByParent(modelState.themes);
  const ordered = [];
  const appendBranch = (parentId) => {
    (byParent.get(parentId) || []).forEach((t) => {
      ordered.push(t);
      appendBranch(t.theme_id);
    });
  };
  appendBranch(null);
  // A sub-code whose parent is archived (so absent from this active list) would never
  // be reached from the roots -- list it at the end rather than dropping it.
  const placed = new Set(ordered.map((t) => t.theme_id));
  modelState.themes.forEach((t) => { if (!placed.has(t.theme_id)) ordered.push(t); });

  ordered.forEach((t) => {
    const div = document.createElement("div");
    div.className = "model-theme-item" + (t.theme_id === modelState.selectedThemeId ? " selected" : "");
    div.dataset.themeId = t.theme_id;
    div.style.paddingLeft = 4 + Math.min(t.depth || 0, 6) * 16 + "px";

    const run = modelState.latestRuns[t.theme_id];
    const count = (byTheme[t.theme_id] && byTheme[t.theme_id].count) || 0;
    const badge = run
      ? `<span class="model-badge" title="latest TF-IDF run: ${escapeHtml(run.model_version)}">` +
        `F1 ${run.f1.toFixed(2)} · PR-AUC ${run.pr_auc.toFixed(2)}</span>`
      : `<span class="model-theme-item-hint">${count}/25 coded</span>`;

    div.innerHTML =
      `<span class="theme-dot" style="background:${escapeHtml(t.color || "#6b7fd7")}"></span>` +
      `<span class="model-theme-item-name">${escapeHtml(t.name)}</span>` +
      badge;
    container.appendChild(div);
  });
}

// --- Selected theme detail: interpret + metrics + history ---------------------------------

async function selectModelTheme(themeId, engine) {
  modelState.selectedThemeId = themeId;
  modelState.selectedEngine = engine || modelState.selectedEngine || "tfidf";
  renderModelThemeList(); // update the "selected" highlight

  document.getElementById("modelDetailEmpty").classList.add("hidden");
  document.getElementById("modelStoragePanel").classList.add("hidden"); // storage.js's panel shares this area
  document.getElementById("modelDetailContent").classList.remove("hidden");
  document.getElementById("modelThemeName").textContent =
    (modelState.themes.find((t) => t.theme_id === themeId) || {}).name || themeId;

  const historyRes = await fetch(`/api/coding/model_runs?theme_id=${encodeURIComponent(themeId)}`);
  const historyData = await historyRes.json();
  if (modelState.selectedThemeId !== themeId) return; // superseded by a newer selection
  modelState.history = historyData.history || [];

  // The engine toggle only appears once this theme has at least one embed_v1_* run --
  // most themes will never have one, and showing an always-present toggle would be noise.
  const hasEmbed = modelState.history.some((r) => engineOfVersion(r.model_version) === "embed");
  if (!hasEmbed) modelState.selectedEngine = "tfidf";
  const toggle = document.getElementById("modelEngineToggle");
  toggle.classList.toggle("hidden", !hasEmbed);
  toggle.querySelectorAll(".model-engine-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.engine === modelState.selectedEngine);
  });

  const latestForEngine = modelState.history.find(
    (r) => engineOfVersion(r.model_version) === modelState.selectedEngine,
  );
  const versionParam = latestForEngine
    ? `&model_version=${encodeURIComponent(latestForEngine.model_version)}` : "";

  const res = await fetch(`/api/model/interpret?theme_id=${encodeURIComponent(themeId)}${versionParam}`);
  modelState.interpret = await res.json();
  if (modelState.selectedThemeId !== themeId) return;
  renderModelDetail();
  renderMetricsHistory();
}

function renderModelDetail() {
  const data = modelState.interpret;

  if (!data.trained) {
    document.getElementById("modelValueProp").innerHTML =
      `Not trained yet — ${data.progress.count || 0}/${data.min_positives} segments coded for this theme. ` +
      `Keep coding, then click "Train / retrain this scheme's themes".`;
    document.getElementById("modelCoverage").textContent = "";
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
    `(${vp.threshold.toFixed(3)}) that you haven't coded` +
    (vp.rejected_count ? ` or rejected (${vp.rejected_count} rejected so far).` : " yet.");

  const cov = data.coverage;
  if (cov) {
    const pct = (cov.coverage_pct * 100).toFixed(0);
    document.getElementById("modelCoverage").textContent =
      `Applied to the full corpus: covers ${cov.covered_count}/${cov.total_segments} segments ` +
      `(${pct}%) at this threshold — ${cov.coded_count} already coded, ` +
      `${cov.flagged_uncoded_count} more flagged.`;
  }

  const m = data.metrics;
  const caution = m.n_pos < data.trust_positives_floor
    ? ` <span class="hint">(below the ${data.trust_positives_floor}-positive rule of thumb — treat with caution)</span>`
    : "";
  document.getElementById("modelLatestMetrics").innerHTML =
    `<span>n_pos <strong>${m.n_pos}</strong></span>` +
    `<span>n_neg <strong>${m.n_neg}</strong></span>` +
    `<span>negatives <strong>${escapeHtml(NEGATIVE_MODE_LABELS[m.negative_mode] || m.negative_mode || "—")}</strong></span>` +
    `<span>precision <strong>${m.precision.toFixed(2)}</strong></span>` +
    `<span>recall <strong>${m.recall.toFixed(2)}</strong></span>` +
    `<span>F1 <strong>${m.f1.toFixed(2)}</strong></span>` +
    `<span>PR-AUC <strong>${m.pr_auc.toFixed(2)}</strong></span>` +
    `<span>threshold <strong>${m.threshold.toFixed(3)}</strong></span>` +
    caution;

  renderTermsList("modelTermsPos", data.top_terms.positive);
  renderTermsList("modelTermsNeg", data.top_terms.negative);

  renderExemplars(data.exemplars, (data.top_terms.positive || []).map((t) => t.term));
}

function renderTermsList(elementId, terms) {
  const el = document.getElementById(elementId);
  el.innerHTML = terms.length
    ? terms.map((t) => `<li><span>${escapeHtml(t.term)}</span><span class="term-weight">${t.weight.toFixed(3)}</span></li>`).join("")
    : '<li class="hint">—</li>';
}

function renderMetricsHistory() {
  const tbody = document.getElementById("modelHistoryTable").querySelector("tbody");
  const rows = modelState.interpret.trained ? modelState.history : [];
  tbody.innerHTML = rows
    .map((r) => {
      const trainedAt = r.trained_at ? r.trained_at.slice(0, 19).replace("T", " ") : "—";
      return (
        `<tr><td>${escapeHtml(trainedAt)}</td><td>${escapeHtml(r.model_version)}</td>` +
        `<td>${r.n_pos}</td><td>${r.n_neg}</td>` +
        `<td>${escapeHtml(NEGATIVE_MODE_LABELS[r.negative_mode] || r.negative_mode || "—")}</td>` +
        `<td>${r.precision.toFixed(2)}</td><td>${r.recall.toFixed(2)}</td>` +
        `<td>${r.f1.toFixed(2)}</td><td>${r.pr_auc.toFixed(2)}</td><td>${r.threshold.toFixed(3)}</td></tr>`
      );
    })
    .join("");
}

function renderExemplars(exemplars, highlightTerms) {
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
      : ex.already_rejected
      ? '<span class="exemplar-coded-badge">rejected</span>'
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
      `<div class="exemplar-text">${highlightTermsHtml(ex.text, highlightTerms)}</div>` +
      `<button class="btn-secondary exemplar-view-btn">View in Code</button>`;
    div.querySelector(".exemplar-view-btn").addEventListener("click", () => viewExemplarInCoding(ex));
    container.appendChild(div);
  });
}

// --- View in Coding ---------------------------------------------------------------------

async function viewExemplarInCoding(exemplar) {
  await jumpToSegmentInCoding(exemplar);
}

// --- Train / retrain ----------------------------------------------------------------------

// Which scheme a training pass (TF-IDF or embedding) covers: the active scheme by
// default, or null (every scheme) when the "every scheme" box is ticked or no
// scheme exists yet. The server treats an omitted scheme_id as "every scheme".
function trainingScope() {
  const all = document.getElementById("trainAllSchemes").checked;
  return all ? null : codingState.activeSchemeId || null;
}

function trainingRequestInit() {
  const schemeId = trainingScope();
  const body = { negative_mode: document.getElementById("trainNegativeMode").value };
  if (schemeId) body.scheme_id = schemeId;
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
}

// Short labels for classifier.NEGATIVE_MODES, shown in metrics and the run history.
const NEGATIVE_MODE_LABELS = { reviewed: "judged only", read_in_full: "read in full only", all_uncoded: "all uncoded" };

// "Done — ..." summary of a finished training job, breaking skipped themes down by
// what they still need (job.skipped[].status -- see viewer_server._skipped_entry()).
function trainingDoneText(job) {
  const parts = [`Done — ${job.trained.length} theme(s) trained`];
  const byStatus = {};
  job.skipped.forEach((t) => { (byStatus[t.status] = byStatus[t.status] || []).push(t); });
  const list = (items) => items.map((t) => `${t.name} ${t.have}/${t.needed}`).join(", ");
  if (byStatus.skipped_insufficient_positives) {
    parts.push(`${byStatus.skipped_insufficient_positives.length} need more coded segments (${list(byStatus.skipped_insufficient_positives)})`);
  }
  if (byStatus.skipped_below_trust_floor) {
    parts.push(`${byStatus.skipped_below_trust_floor.length} below the trust floor (${list(byStatus.skipped_below_trust_floor)})`);
  }
  if (byStatus.skipped_insufficient_negatives) {
    parts.push(
      `${byStatus.skipped_insufficient_negatives.length} need more judged negatives ` +
      `(${list(byStatus.skipped_insufficient_negatives)}) — mark interviews "fully coded" in the Code tab, ` +
      `or train with "Every uncoded passage"`,
    );
  }
  return parts.join("; ") + ".";
}

async function startTraining() {
  const btn = document.getElementById("trainModelsBtn");
  btn.disabled = true;
  document.getElementById("modelTrainStatus").textContent = "Starting…";

  let data;
  try {
    const res = await fetch("/api/model/train", trainingRequestInit());
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
  modelState.trainPollTimer?.cancel();
  modelState.trainPollTimer = pollBackgroundJob(
    `/api/model/train_status?job_id=${encodeURIComponent(modelState.trainJobId)}`,
    {
      onRunning: (job) => {
        const themeText = job.current_theme ? `: ${job.current_theme.name}` : "";
        document.getElementById("modelTrainStatus").textContent =
          `Training theme ${job.themes_done}/${job.themes_total}${themeText}…`;
      },
      onDone: (job) => {
        document.getElementById("trainModelsBtn").disabled = false;
        document.getElementById("modelTrainStatus").textContent =
          trainingDoneText(job);
        loadModelThemes().then(() => {
          if (modelState.selectedThemeId) selectModelTheme(modelState.selectedThemeId);
        });
      },
      onError: (job) => {
        document.getElementById("trainModelsBtn").disabled = false;
        document.getElementById("modelTrainStatus").textContent = "Error: " + job.error;
      },
    },
  );
}

// --- Export full-corpus topic model -------------------------------------------------------

async function exportFullCorpus() {
  const btn = document.getElementById("exportFullCorpusBtn");
  const statusEl = document.getElementById("modelExportStatus");
  btn.disabled = true;
  statusEl.textContent = "Exporting…";
  try {
    const schemePart = codingState.activeSchemeId
      ? `?scheme_id=${encodeURIComponent(codingState.activeSchemeId)}` : "";
    const res = await fetch(`/api/model/export_full_corpus${schemePart}`);
    const data = await res.json();
    if (data.error) {
      statusEl.textContent = "Error: " + data.error;
    } else {
      statusEl.textContent = `Saved ${data.files.join(", ")} to ${data.exported_to}/`;
    }
  } catch (err) {
    statusEl.textContent = "Request failed — is the server running?";
  }
  btn.disabled = false;
}

// --- Optional embedding-based ("richer") engine ---------------------------------------
//
// Gated behind /api/model/embedding_readiness: sentence-transformers must be installed
// (see requirements-embeddings.txt -- not part of the default install) AND at least one
// theme must have cleared the TRUST_POSITIVES floor. Both conditions are surfaced
// separately so the researcher knows exactly what's missing, not just "unavailable."

async function loadEmbeddingReadiness() {
  // Same scope the embedding training request will use, so the button is never
  // enabled by a different scheme's volume than the one it would train.
  const schemeId = trainingScope();
  const schemePart = schemeId ? `?scheme_id=${encodeURIComponent(schemeId)}` : "";
  const res = await fetch(`/api/model/embedding_readiness${schemePart}`);
  renderEmbeddingReadiness(await res.json());
}

function renderEmbeddingReadiness(readiness) {
  const textEl = document.getElementById("embeddingReadinessText");
  const btn = document.getElementById("trainEmbeddingsBtn");
  const installBtn = document.getElementById("installEmbeddingsBtn");
  if (!readiness.installed) {
    textEl.textContent =
      "Embedding-based topic model: needs the optional embedding dependencies (a few hundred MB, " +
      "one-time download) to enable this.";
    installBtn.classList.remove("hidden");
    btn.classList.add("hidden");
  } else if (!readiness.volume_ready) {
    textEl.textContent =
      `Embedding-based topic model: needs at least one theme with ${readiness.trust_positives}+ ` +
      `coded segments (${readiness.themes_meeting_trust.length}/${readiness.themes_total_active} ` +
      `themes there so far).`;
    installBtn.classList.add("hidden");
    btn.classList.add("hidden");
  } else {
    textEl.textContent = "Embedding-based topic model available, bootstrapped from your codebook's coded segments.";
    installBtn.classList.add("hidden");
    btn.classList.remove("hidden");
  }
}

async function startInstallEmbeddingsDeps() {
  const btn = document.getElementById("installEmbeddingsBtn");
  const statusEl = document.getElementById("installEmbeddingsStatus");
  btn.disabled = true;
  statusEl.textContent = "Starting install…";

  let data;
  try {
    const res = await fetch("/api/model/install_embeddings_deps", { method: "POST" });
    data = await res.json();
  } catch (err) {
    statusEl.textContent = "Request failed — is the server running?";
    btn.disabled = false;
    return;
  }
  if (data.error) {
    statusEl.textContent = "Error: " + data.error;
    btn.disabled = false;
    return;
  }
  modelState.installJobId = data.job_id;
  modelState.installStartedAt = Date.now();
  pollInstallEmbeddingsJob();
}

function pollInstallEmbeddingsJob() {
  modelState.installPollTimer?.cancel();
  modelState.installPollTimer = pollBackgroundJob(
    `/api/model/install_embeddings_deps_status?job_id=${encodeURIComponent(modelState.installJobId)}`,
    {
      onRunning: () => {
        const elapsed = Math.round((Date.now() - modelState.installStartedAt) / 1000);
        document.getElementById("installEmbeddingsStatus").textContent =
          `Installing… (${elapsed}s — first install downloads PyTorch, can take several minutes)`;
      },
      onDone: () => {
        document.getElementById("installEmbeddingsBtn").disabled = false;
        document.getElementById("installEmbeddingsStatus").textContent = "Installed.";
        loadEmbeddingReadiness();
      },
      onError: (job) => {
        document.getElementById("installEmbeddingsBtn").disabled = false;
        document.getElementById("installEmbeddingsStatus").textContent = "Error: " + job.error;
      },
    },
  );
}

async function startEmbeddingTraining() {
  const btn = document.getElementById("trainEmbeddingsBtn");
  const statusEl = document.getElementById("embeddingTrainStatus");
  btn.disabled = true;
  statusEl.textContent = "Starting…";

  let data;
  try {
    const res = await fetch("/api/model/train_embeddings", trainingRequestInit());
    data = await res.json();
  } catch (err) {
    statusEl.textContent = "Request failed — is the server running?";
    btn.disabled = false;
    return;
  }
  if (data.error) {
    statusEl.textContent = "Error: " + data.error;
    btn.disabled = false;
    return;
  }
  modelState.embedTrainJobId = data.job_id;
  pollEmbeddingTrainJob();
}

function pollEmbeddingTrainJob() {
  modelState.embedTrainPollTimer?.cancel();
  modelState.embedTrainPollTimer = pollBackgroundJob(
    `/api/model/train_embeddings_status?job_id=${encodeURIComponent(modelState.embedTrainJobId)}`,
    {
      onRunning: (job) => {
        const themeText = job.current_theme ? `: ${job.current_theme.name}` : "";
        document.getElementById("embeddingTrainStatus").textContent =
          `Training theme ${job.themes_done}/${job.themes_total}${themeText}…`;
      },
      onDone: (job) => {
        document.getElementById("trainEmbeddingsBtn").disabled = false;
        document.getElementById("embeddingTrainStatus").textContent =
          trainingDoneText(job);
        loadModelThemes().then(() => {
          if (modelState.selectedThemeId) selectModelTheme(modelState.selectedThemeId);
        });
      },
      onError: (job) => {
        document.getElementById("trainEmbeddingsBtn").disabled = false;
        document.getElementById("embeddingTrainStatus").textContent = "Error: " + job.error;
      },
    },
  );
}

// --- Events / init --------------------------------------------------------------------

function bindModelEvents() {
  document.getElementById("trainModelsBtn").addEventListener("click", startTraining);
  document.getElementById("exportFullCorpusBtn").addEventListener("click", exportFullCorpus);
  document.getElementById("trainEmbeddingsBtn").addEventListener("click", startEmbeddingTraining);
  document.getElementById("installEmbeddingsBtn").addEventListener("click", startInstallEmbeddingsDeps);
  document.getElementById("trainAllSchemes").addEventListener("change", loadEmbeddingReadiness);
  document.getElementById("modelThemeItems").addEventListener("click", (e) => {
    const item = e.target.closest(".model-theme-item");
    if (item) selectModelTheme(item.dataset.themeId);
  });
  document.getElementById("modelEngineToggle").addEventListener("click", (e) => {
    const btn = e.target.closest(".model-engine-btn");
    if (btn && modelState.selectedThemeId) selectModelTheme(modelState.selectedThemeId, btn.dataset.engine);
  });
  window.onEnterModelTab = () => {
    loadModelThemes();
    loadEmbeddingReadiness();
  };
}

function initModel() {
  bindModelEvents();
}

initModel();
