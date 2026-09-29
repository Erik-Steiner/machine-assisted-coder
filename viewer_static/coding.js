// Coding tab: codebook management + segment-level theme tagging.
//
// Shares the global `state` object and helpers (escapeHtml, prev, next,
// switchTab) defined in app.js, which loads before this file. Interview
// navigation is shared with Browse (same state.filtered/state.pos) so
// filtering/jumping to an interview in one tab carries over to the other;
// this file only adds coding-specific rendering on top.

const codingState = {
  schemes: [], // independent codebooks: [{scheme_id, name, description, theme_count, code_count}]
  activeSchemeId: null, // which scheme's themes are currently loaded/shown -- the single
                         // source of truth model.js/review.js read (never set) on load
  themes: [], // the active scheme's themes, flat but each carrying parent_theme_id/depth so
              // renderThemeList()/buildChipsHtml() can build a tree client-side:
              // [{theme_id, name, description, color, code_count, scheme_id, parent_theme_id, depth}]
  pendingParentThemeId: null, // set by "+ Add sub-code"; read/cleared by addTheme()
  modelRuns: {}, // {theme_id: {f1, pr_auc, precision, recall, n_pos, model_version, trained_at}}
  datasets: [], // [{id, label, count, source, created_at, status: "active"|"excluded"|"unloaded"}]
  itemId: null,
  segments: [], // [{segment_id, seg_index, speaker_name, speaker_role, depth, word_count, timestamp, text, theme_ids}]
  focusIndex: -1,
  pendingFocusSegmentId: null, // set by model.js's "View in Coding" before switching tabs
  minWords: 0, // live "min word count" filter -- segments below this are hidden, not deleted
};

// Builds {parent_theme_id_or_null: [theme, ...]} from the flat codingState.themes
// array -- the one shared helper renderThemeList()/buildChipsHtml()'s numbering/
// startMergeUI()'s descendant-exclusion all walk instead of each re-deriving it.
function themesByParent(themeList) {
  const map = new Map();
  themeList.forEach((t) => {
    const key = t.parent_theme_id || null;
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(t);
  });
  return map;
}

// theme_id's descendants within codingState.themes, walked from themesByParent()'s
// map -- used to keep a theme from being merged into or reparented under its own
// child (the store layer also rejects this; this is just so the UI never offers it).
function descendantThemeIds(themeId, byParent) {
  const out = [];
  const frontier = [themeId];
  while (frontier.length) {
    const current = frontier.pop();
    (byParent.get(current) || []).forEach((child) => {
      out.push(child.theme_id);
      frontier.push(child.theme_id);
    });
  }
  return out;
}

// Segments below minWords, or whose speaker_role is excluded, stay in
// codingState.segments (data untouched) but are skipped by rendering and by
// j/k/number-key navigation -- filtering is a view concern only.
function visibleSegmentIndices() {
  const out = [];
  codingState.segments.forEach((seg, idx) => {
    if ((seg.word_count || 0) >= codingState.minWords) out.push(idx);
  });
  return out;
}

// --- Schemes (independent codebooks) -------------------------------------------------
//
// codingState.activeSchemeId is the single source of truth for which codebook is
// currently in view -- the Coding tab is the only place it's ever changed;
// model.js/review.js just read it when they load their own theme lists.

async function loadSchemes() {
  const res = await fetch("/api/schemes");
  codingState.schemes = await res.json();
  if (!codingState.activeSchemeId || !codingState.schemes.some((s) => s.scheme_id === codingState.activeSchemeId)) {
    codingState.activeSchemeId = codingState.schemes.length ? codingState.schemes[0].scheme_id : null;
  }
  renderSchemeSwitcher();
  renderSchemesList();
}

function renderSchemeSwitcher() {
  const select = document.getElementById("schemeSwitcher");
  select.innerHTML = codingState.schemes
    .map((s) => `<option value="${escapeHtml(s.scheme_id)}">${escapeHtml(s.name)} (${s.theme_count})</option>`)
    .join("");
  if (codingState.activeSchemeId) select.value = codingState.activeSchemeId;
}

async function switchScheme(schemeId) {
  codingState.activeSchemeId = schemeId;
  await loadThemes();
}

function renderSchemesList() {
  const container = document.getElementById("schemesList");
  container.innerHTML = "";
  if (!codingState.schemes.length) {
    container.innerHTML = '<div class="hint">No schemes yet — add one below.</div>';
    return;
  }
  codingState.schemes.forEach((s) => container.appendChild(renderSchemeRow(s)));
}

function renderSchemeRow(s) {
  const row = document.createElement("div");
  row.className = "theme-row";
  row.dataset.schemeId = s.scheme_id;
  const descHtml = s.description
    ? `<div class="theme-row-desc">${escapeHtml(s.description)}</div>`
    : "";
  row.innerHTML =
    `<div class="theme-row-main">` +
    `<span class="theme-row-name">${escapeHtml(s.name)}</span>` +
    `<span class="theme-row-count">${s.theme_count} theme(s), ${s.code_count} code(s)</span>` +
    `<span class="theme-row-actions">` +
    `<button class="btn-secondary scheme-archive-btn" data-id="${escapeHtml(s.scheme_id)}">Archive</button>` +
    `</span>` +
    `</div>` +
    descHtml;
  return row;
}

async function addScheme() {
  const nameEl = document.getElementById("newSchemeName");
  const descEl = document.getElementById("newSchemeDesc");
  const statusEl = document.getElementById("schemeFormStatus");
  const name = nameEl.value.trim();
  if (!name) {
    statusEl.textContent = "Name is required.";
    return;
  }
  statusEl.textContent = "Adding…";
  try {
    const res = await fetch("/api/schemes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, description: descEl.value.trim() }),
    });
    const data = await res.json();
    if (data.error) {
      statusEl.textContent = "Error: " + data.error;
      return;
    }
    nameEl.value = "";
    descEl.value = "";
    statusEl.textContent = "";
    codingState.activeSchemeId = data.scheme_id; // switch straight to the new scheme
    await loadSchemes();
    await loadThemes();
  } catch (err) {
    statusEl.textContent = "Request failed — is the server running?";
  }
}

async function archiveScheme(schemeId) {
  const scheme = codingState.schemes.find((s) => s.scheme_id === schemeId);
  if (!scheme) return;
  if (!confirm(`Archive scheme "${scheme.name}"? Its themes and codes are kept, but it won't be offered as a scheme to code against.`)) return;
  await fetch(`/api/schemes/${encodeURIComponent(schemeId)}/archive`, { method: "POST" });
  await loadSchemes();
  if (codingState.activeSchemeId !== schemeId) return;
  await loadThemes();
}

// --- Codebook (themes) --------------------------------------------------------------

async function loadThemes() {
  if (!codingState.activeSchemeId) {
    codingState.themes = [];
    renderThemeList();
    renderSegments();
    return;
  }
  const [themesRes, runsRes] = await Promise.all([
    fetch(`/api/codebook/themes?scheme_id=${encodeURIComponent(codingState.activeSchemeId)}`),
    fetch("/api/coding/model_runs"),
  ]);
  codingState.themes = await themesRes.json();
  codingState.modelRuns = await runsRes.json();
  renderThemeList();
  renderSegments(); // chip options changed
}

function renderThemeList() {
  const container = document.getElementById("themeList");
  container.innerHTML = "";
  if (!codingState.themes.length) {
    container.innerHTML = '<div class="hint">No themes yet — add one below to start coding.</div>';
    return;
  }
  const byParent = themesByParent(codingState.themes);
  const appendBranch = (parentId) => {
    (byParent.get(parentId) || []).forEach((t) => {
      container.appendChild(renderThemeRow(t));
      appendBranch(t.theme_id);
    });
  };
  appendBranch(null);
}

function renderThemeRow(t) {
  const row = document.createElement("div");
  row.className = "theme-row";
  row.dataset.themeId = t.theme_id;
  row.style.marginLeft = Math.min(t.depth || 0, 6) * 20 + "px";
  const descHtml = t.description
    ? `<div class="theme-row-desc">${escapeHtml(t.description)}</div>`
    : `<div class="theme-row-desc theme-row-desc-empty">No description yet — click Edit to add one.</div>`;
  const run = codingState.modelRuns[t.theme_id];
  const badgeHtml = run
    ? `<span class="model-badge" title="trained on ${run.n_pos} positives">` +
      `F1 ${run.f1.toFixed(2)} · PR-AUC ${run.pr_auc.toFixed(2)}</span>`
    : "";
  row.innerHTML =
    `<div class="theme-row-main">` +
    `<span class="theme-dot" style="background:${escapeHtml(t.color || "#6b7fd7")}"></span>` +
    `<span class="theme-row-name">${escapeHtml(t.name)}</span>` +
    `<span class="theme-row-count">${t.code_count}</span>` +
    badgeHtml +
    `<span class="theme-row-actions">` +
    `<button class="btn-secondary theme-add-sub-btn" data-id="${escapeHtml(t.theme_id)}">+ Add sub-code</button>` +
    `<button class="btn-secondary theme-edit-btn" data-id="${escapeHtml(t.theme_id)}">Edit</button>` +
    `<button class="btn-secondary theme-merge-btn" data-id="${escapeHtml(t.theme_id)}">Merge</button>` +
    `<button class="btn-secondary theme-archive-btn" data-id="${escapeHtml(t.theme_id)}">Archive</button>` +
    `</span>` +
    `</div>` +
    descHtml;
  return row;
}

// Pre-fills the "add theme" form to create a sub-code under parentId -- cleared
// after a successful add (or Cancel) by resetPendingSubTheme().
function startAddSubTheme(parentId) {
  const parent = codingState.themes.find((t) => t.theme_id === parentId);
  if (!parent) return;
  codingState.pendingParentThemeId = parentId;
  const hint = document.getElementById("newThemeParentHint");
  hint.textContent = `Adding a sub-code under "${parent.name}".`;
  hint.classList.remove("hidden");
  document.getElementById("cancelSubThemeBtn").classList.remove("hidden");
  document.getElementById("newThemeName").focus();
}

function resetPendingSubTheme() {
  codingState.pendingParentThemeId = null;
  document.getElementById("newThemeParentHint").classList.add("hidden");
  document.getElementById("cancelSubThemeBtn").classList.add("hidden");
}

function startEditUI(themeId) {
  const theme = codingState.themes.find((t) => t.theme_id === themeId);
  const row = document.querySelector(`.theme-row[data-theme-id="${themeId}"]`);
  if (!theme || !row) return;
  row.innerHTML =
    `<div class="theme-edit-form">` +
    `<input type="text" class="theme-edit-name" value="${escapeHtml(theme.name)}" placeholder="Theme name">` +
    `<textarea class="theme-edit-desc" rows="2" placeholder="What counts as this theme?">${escapeHtml(theme.description || "")}</textarea>` +
    `<div class="theme-edit-actions">` +
    `<button class="theme-edit-save" data-id="${escapeHtml(themeId)}">Save</button>` +
    `<button class="btn-secondary theme-edit-cancel">Cancel</button>` +
    `</div>` +
    `</div>`;
  row.querySelector(".theme-edit-name").focus();
}

async function saveThemeEdit(themeId) {
  const row = document.querySelector(`.theme-row[data-theme-id="${themeId}"]`);
  if (!row) return;
  const name = row.querySelector(".theme-edit-name").value.trim();
  const description = row.querySelector(".theme-edit-desc").value.trim();
  if (!name) {
    alert("Name is required.");
    return;
  }
  await fetch(`/api/codebook/themes/${encodeURIComponent(themeId)}/update`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, description }),
  });
  await loadThemes();
}

function startMergeUI(themeId) {
  const row = document.querySelector(`.theme-row[data-theme-id="${themeId}"]`);
  const byParent = themesByParent(codingState.themes);
  const descendants = new Set(descendantThemeIds(themeId, byParent));
  const others = codingState.themes.filter((t) => t.theme_id !== themeId && !descendants.has(t.theme_id));
  if (!row) return;
  if (!others.length) {
    alert("No other theme to merge into yet — add one first.");
    return;
  }
  const actions = row.querySelector(".theme-row-actions");
  actions.innerHTML =
    `<select class="merge-target-select">` +
    others.map((t) => `<option value="${escapeHtml(t.theme_id)}">into ${escapeHtml(t.name)}</option>`).join("") +
    `</select>` +
    `<button class="btn-secondary theme-merge-confirm" data-id="${escapeHtml(themeId)}">Merge</button>` +
    `<button class="btn-secondary theme-merge-cancel">Cancel</button>`;
}

async function confirmMerge(sourceId, targetId) {
  const source = codingState.themes.find((t) => t.theme_id === sourceId);
  const target = codingState.themes.find((t) => t.theme_id === targetId);
  if (!source || !target) return;
  const ok = confirm(
    `Merge "${source.name}" into "${target.name}"? ` +
    `All ${source.code_count} coded segment(s) currently tagged "${source.name}" will be retagged ` +
    `"${target.name}" instead (the text stays coded — only the theme changes). ` +
    `"${source.name}" will then be archived.`
  );
  if (!ok) {
    renderThemeList();
    return;
  }
  await fetch(`/api/codebook/themes/${encodeURIComponent(sourceId)}/merge_into`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target_theme_id: targetId }),
  });
  await loadThemes();
  if (codingState.itemId != null) await loadCodingSegments(codingState.itemId);
}

async function addTheme() {
  const nameEl = document.getElementById("newThemeName");
  const descEl = document.getElementById("newThemeDesc");
  const statusEl = document.getElementById("themeFormStatus");
  const name = nameEl.value.trim();
  if (!name) {
    statusEl.textContent = "Name is required.";
    return;
  }
  if (!codingState.activeSchemeId) {
    statusEl.textContent = "Add a scheme first (Manage schemes, above).";
    return;
  }
  statusEl.textContent = "Adding…";
  try {
    const res = await fetch("/api/codebook/themes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        name, description: descEl.value.trim(),
        scheme_id: codingState.activeSchemeId,
        parent_theme_id: codingState.pendingParentThemeId,
      }),
    });
    const data = await res.json();
    if (data.error) {
      statusEl.textContent = "Error: " + data.error;
      return;
    }
    nameEl.value = "";
    descEl.value = "";
    statusEl.textContent = "";
    resetPendingSubTheme();
    await loadThemes();
  } catch (err) {
    statusEl.textContent = "Request failed — is the server running?";
  }
}

async function archiveTheme(themeId) {
  const theme = codingState.themes.find((t) => t.theme_id === themeId);
  if (!theme) return;
  if (!confirm(`Archive "${theme.name}"? Existing codes are kept, but it won't be offered for new coding.`)) return;
  await fetch(`/api/codebook/themes/${encodeURIComponent(themeId)}/archive`, { method: "POST" });
  await loadThemes();
}

// --- Datasets (whole-corpus include/exclude/unload) ------------------------------------

const DATASET_STATUS_LABELS = {
  active: "Active",
  excluded: "Excluded from analysis",
  unloaded: "Unloaded",
};

async function loadDatasetStatuses() {
  const res = await fetch("/api/datasets/all");
  codingState.datasets = await res.json();
  renderDatasetsPanel();
}

function renderDatasetsPanel() {
  const container = document.getElementById("datasetsList");
  container.innerHTML = "";
  if (!codingState.datasets.length) {
    container.innerHTML = '<div class="hint">No datasets yet — import or search one in Search / Import.</div>';
    return;
  }
  codingState.datasets.forEach((d) => {
    container.appendChild(renderDatasetRow(d));
  });
}

function renderDatasetRow(d) {
  const row = document.createElement("div");
  row.className = "dataset-row";
  row.dataset.datasetId = d.id;
  const statusLabel = DATASET_STATUS_LABELS[d.status] || d.status;
  let actionsHtml;
  if (d.status === "active") {
    actionsHtml =
      `<button class="btn-secondary dataset-exclude-btn" data-id="${escapeHtml(d.id)}">Exclude from analysis</button>` +
      `<button class="btn-secondary dataset-unload-btn" data-id="${escapeHtml(d.id)}">Unload…</button>`;
  } else if (d.status === "excluded") {
    actionsHtml =
      `<button class="btn-secondary dataset-restore-btn" data-id="${escapeHtml(d.id)}">Include in analysis</button>` +
      `<button class="btn-secondary dataset-unload-btn" data-id="${escapeHtml(d.id)}">Unload…</button>`;
  } else {
    actionsHtml = `<button class="btn-secondary dataset-restore-btn" data-id="${escapeHtml(d.id)}">Restore</button>`;
  }
  const segmentHtml = d.segment_count > 0
    ? `<span class="dataset-row-segments">${d.segment_count} segment(s)</span>`
    : `<span class="dataset-row-segments dataset-row-unsegmented">not segmented</span>` +
      `<button class="btn-secondary dataset-segment-btn" data-id="${escapeHtml(d.id)}">Segment</button>`;
  row.innerHTML =
    `<div class="dataset-row-main">` +
    `<span class="dataset-row-label">${escapeHtml(d.label)}</span>` +
    `<span class="dataset-row-count">${d.count} interview(s)</span>` +
    segmentHtml +
    `<span class="dataset-status-badge dataset-status-${escapeHtml(d.status)}">${escapeHtml(statusLabel)}</span>` +
    `<span class="dataset-row-actions">${actionsHtml}</span>` +
    `</div>`;
  return row;
}

// --- Segmenting datasets (the in-app equivalent of scripts/build_segments.py) --------

function pollSegmentJob(jobId, statusElId) {
  const statusEl = document.getElementById(statusElId);
  document.getElementById("segmentAllBtn").disabled = true;
  pollBackgroundJob(
    `/api/coding/segment_status?job_id=${encodeURIComponent(jobId)}`,
    {
      onRunning: (job) => {
        const current = job.current_dataset ? `: ${job.current_dataset}` : "";
        statusEl.textContent = `Segmenting ${job.datasets_done}/${job.datasets_total}${current}…`;
      },
      onDone: (job) => {
        document.getElementById("segmentAllBtn").disabled = false;
        statusEl.textContent = `Done — ${job.segments_added_total} segment(s) added across ${job.datasets_done} dataset(s).`;
        loadDatasetStatuses();
      },
      onError: (job) => {
        document.getElementById("segmentAllBtn").disabled = false;
        statusEl.textContent = "Error: " + job.error;
      },
    },
  );
}

async function segmentDatasets(datasetIds) {
  const statusEl = document.getElementById("segmentJobStatus");
  statusEl.textContent = "Starting…";
  const res = await fetch("/api/coding/segment_datasets", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(datasetIds ? { dataset_ids: datasetIds } : {}),
  });
  const data = await res.json();
  if (data.error) {
    statusEl.textContent = "Error: " + data.error;
    return;
  }
  pollSegmentJob(data.job_id, "segmentJobStatus");
}

// --- Scanning for near-duplicate transcripts (the in-app equivalent of
// scripts/find_duplicates.py) -----------------------------------------------------

async function scanForDuplicates() {
  const btn = document.getElementById("scanDuplicatesBtn");
  const statusEl = document.getElementById("duplicateScanStatus");
  btn.disabled = true;
  statusEl.textContent = "Starting…";

  let data;
  try {
    const res = await fetch("/api/duplicates/scan", { method: "POST" });
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

  pollBackgroundJob(
    `/api/duplicates/scan_status?job_id=${encodeURIComponent(data.job_id)}`,
    {
      onRunning: (job) => {
        if (job.clusters_total) {
          statusEl.textContent = `Comparing… cluster ${job.clusters_done}/${job.clusters_total}`;
        } else if (job.n_items_scanned) {
          statusEl.textContent = `Comparing ${job.n_items_scanned} item(s) across ${job.n_datasets} dataset(s)…`;
        } else {
          statusEl.textContent = "Scanning items…";
        }
      },
      onDone: (job) => {
        btn.disabled = false;
        if (!job.run_id) {
          statusEl.textContent = "Done — no scannable items found (need person_name/publish_date/transcript_text).";
          return;
        }
        statusEl.textContent =
          `Done — ${job.n_clusters} cluster(s) found across ${job.n_items_scanned} item(s), ` +
          `${job.needs_attention_total} flagged for review.`;
        refreshDuplicateStatus(); // app.js -- refreshes the current dataset's duplicate flags
      },
      onError: (job) => {
        btn.disabled = false;
        statusEl.textContent = "Error: " + job.error;
      },
    },
  );
}

async function setDatasetStatus(datasetId, status) {
  await fetch("/api/datasets/status", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ dataset_id: datasetId, status }),
  });
  await loadDatasetStatuses();
  await refreshDatasetList(); // app.js -- keeps the topbar selector in sync
}

async function unloadDataset(datasetId) {
  const d = codingState.datasets.find((x) => x.id === datasetId);
  if (!d) return;
  if (!confirm(
    `Unload "${d.label}"? It'll disappear from Browse, Code, and Analyze until you restore it from this panel.`
  )) return;
  await setDatasetStatus(datasetId, "unloaded");
}

// --- Shared segment-chip rendering + code API (reused by review.js) -------------------

// Numbers only top-level (depth 0) themes for the 1-9 keyboard shortcuts --
// broad themes get the low-friction numeric path, specific child/sub-codes are
// click-only. See toggleFocusedSegmentTheme(), which looks up the same
// depth-0-only ordering by index.
function buildChipsHtml(themeIds, themes) {
  let topLevelIndex = 0;
  return themes
    .map((t) => {
      const active = themeIds.includes(t.theme_id);
      let num = "";
      if ((t.depth || 0) === 0) {
        num = topLevelIndex < 9 ? `<span class="chip-num">${topLevelIndex + 1}</span>` : "";
        topLevelIndex += 1;
      }
      const marginPx = Math.min(t.depth || 0, 6) * 10;
      return (
        `<button type="button" class="theme-chip${active ? " active" : ""}" ` +
        `data-theme="${escapeHtml(t.theme_id)}" ` +
        `style="--chip-color:${escapeHtml(t.color || "#6b7fd7")};margin-left:${marginPx}px">` +
        `${num}${escapeHtml(t.name)}</button>`
      );
    })
    .join("");
}

function postCode(segmentId, themeId, extra) {
  return fetch("/api/coding/codes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ segment_id: segmentId, theme_id: themeId, ...(extra || {}) }),
  });
}

function deleteCode(segmentId, themeId) {
  return fetch("/api/coding/codes/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ segment_id: segmentId, theme_id: themeId }),
  });
}

// Switches Browse's dataset if needed, positions on the segment's interview,
// and switches to the Coding tab focused on that exact segment. Used by
// model.js's "View in Coding" and review.js's equivalent -- a segment shown
// elsewhere in the app can belong to any dataset, not necessarily the one
// currently selected in Browse.
async function jumpToSegmentInCoding(target) {
  await refreshDatasetList(target.dataset_id);
  await loadDataset(target.dataset_id);
  const idx = state.filtered.findIndex((r) => r.item_id === target.item_id);
  state.pos = Math.max(idx, 0);
  codingState.pendingFocusSegmentId = target.segment_id;
  switchTab("coding");
}

// --- Segments -------------------------------------------------------------------------

async function loadCodingSegments(itemId) {
  codingState.itemId = itemId;
  codingState.focusIndex = -1;
  document.getElementById("segmentContent").innerHTML = "<em>Loading segments…</em>";
  const res = await fetch(`/api/coding/segments?item_id=${encodeURIComponent(itemId)}`);
  const data = await res.json();
  if (codingState.itemId !== itemId) return; // superseded by a newer navigation
  codingState.segments = data.segments || [];
  renderSegments();

  if (codingState.pendingFocusSegmentId) {
    const idx = codingState.segments.findIndex((s) => s.segment_id === codingState.pendingFocusSegmentId);
    codingState.pendingFocusSegmentId = null;
    if (idx >= 0) setSegmentFocus(idx);
  }
}

function renderSegments() {
  const container = document.getElementById("segmentContent");
  container.innerHTML = "";
  if (!codingState.segments.length) {
    container.innerHTML = "<em>No segments found for this interview — open <strong>Datasets</strong> above and click <strong>Segment</strong> next to its dataset.</em>";
    return;
  }
  const visible = visibleSegmentIndices();
  if (!visible.length) {
    container.innerHTML = "<em>No segments meet the current “min words” filter.</em>";
    return;
  }
  visible.forEach((idx) => {
    container.appendChild(renderSegmentCard(codingState.segments[idx], idx));
  });
}

function renderSegmentCard(seg, idx) {
  const div = document.createElement("div");
  const isInterviewer = seg.speaker_role === "interviewer";
  div.className = "segment-card" + (idx === codingState.focusIndex ? " focused" : "");
  div.dataset.idx = String(idx);
  div.style.marginLeft = Math.min(seg.depth || 0, 6) * 20 + "px";

  const roleBadge = isInterviewer
    ? '<span class="seg-role-badge seg-role-interviewer">interviewer — not coded</span>'
    : seg.speaker_role === "other"
      ? '<span class="seg-role-badge seg-role-other">unclear speaker</span>'
      : "";
  const header =
    `<div class="segment-header">` +
    `<span class="seg-speaker">${escapeHtml(seg.speaker_name || "Unknown")}</span>` +
    roleBadge +
    (seg.timestamp ? `<span class="seg-timestamp">${escapeHtml(seg.timestamp)}</span>` : "") +
    `</div>`;
  const text = `<div class="segment-text">${escapeHtml(seg.text)}</div>`;
  // Interviewer turns display for context but aren't coded -- no chip buttons
  // rendered at all, so the normal workflow can't touch them (coding_store.
  // add_code itself has no role check, this is a UI-level default only).
  const chips = isInterviewer ? "" : buildChipsHtml(seg.theme_ids, codingState.themes);

  div.innerHTML = header + text + (isInterviewer ? "" : `<div class="segment-chips">${chips}</div>`);

  div.addEventListener("click", (e) => {
    const chip = e.target.closest(".theme-chip");
    if (chip) {
      toggleCode(seg, chip.dataset.theme);
      return;
    }
    setSegmentFocus(idx);
  });

  return div;
}

async function toggleCode(seg, themeId) {
  const active = seg.theme_ids.includes(themeId);
  seg.theme_ids = active
    ? seg.theme_ids.filter((id) => id !== themeId)
    : [...seg.theme_ids, themeId];
  renderSegments();
  try {
    if (active) await deleteCode(seg.segment_id, themeId);
    else await postCode(seg.segment_id, themeId);
  } catch (err) {
    // leave the optimistic UI state as-is; a refresh will resync if this failed
  }
  refreshProgress();
  refreshThemeCounts();
}

function setSegmentFocus(idx) {
  codingState.focusIndex = idx;
  document.querySelectorAll("#segmentContent .segment-card").forEach((card) => {
    card.classList.toggle("focused", Number(card.dataset.idx) === idx);
  });
  const el = document.querySelector(`#segmentContent .segment-card[data-idx="${idx}"]`);
  if (el) el.scrollIntoView({ block: "nearest" });
}

function moveSegmentFocus(delta) {
  const visible = visibleSegmentIndices();
  if (!visible.length) return;
  const pos = visible.indexOf(codingState.focusIndex);
  let nextPos = (pos === -1 ? (delta > 0 ? -1 : visible.length) : pos) + delta;
  nextPos = Math.max(0, Math.min(visible.length - 1, nextPos));
  setSegmentFocus(visible[nextPos]);
}

function toggleFocusedSegmentTheme(themeIdx) {
  if (codingState.focusIndex < 0) return;
  // Only depth-0 (top-level) themes get a numeric shortcut -- see buildChipsHtml().
  const topLevel = codingState.themes.filter((t) => (t.depth || 0) === 0);
  const theme = topLevel[themeIdx];
  const seg = codingState.segments[codingState.focusIndex];
  if (!theme || !seg) return;
  if (seg.speaker_role === "interviewer") return; // not coded, per the interviewer-turn default
  toggleCode(seg, theme.theme_id);
}

async function refreshThemeCounts() {
  if (!codingState.activeSchemeId) return;
  const res = await fetch(`/api/codebook/themes?scheme_id=${encodeURIComponent(codingState.activeSchemeId)}`);
  const themes = await res.json();
  const byId = new Map(themes.map((t) => [t.theme_id, t.code_count]));
  codingState.themes.forEach((t) => {
    if (byId.has(t.theme_id)) t.code_count = byId.get(t.theme_id);
  });
  renderThemeList();
}

// --- Progress -------------------------------------------------------------------------

async function refreshProgress() {
  const el = document.getElementById("codingProgress");
  try {
    const schemePart = codingState.activeSchemeId
      ? `&scheme_id=${encodeURIComponent(codingState.activeSchemeId)}` : "";
    const res = await fetch(`/api/coding/progress?dataset=${encodeURIComponent(state.datasetId)}${schemePart}`);
    const p = await res.json();
    const needing = Object.entries(p.by_theme || {})
      .filter(([, v]) => v.count < 25)
      .map(([, v]) => `${v.name} (${v.count}/25)`);
    let text = `${p.coded_segments}/${p.total_segments} segments coded`;
    if (needing.length) text += ` · needs more: ${needing.join(", ")}`;
    el.textContent = text;
  } catch (err) {
    el.textContent = "";
  }
}

// --- Interview navigation (shared with Browse) -----------------------------------------

function renderCodingMeta(rec) {
  document.getElementById("codingPositionCounter").textContent = state.filtered.length
    ? `Interview ${state.pos + 1} of ${state.filtered.length}`
    : "No interviews match the current filters";
  if (!rec) {
    document.getElementById("codingPersonName").textContent = "";
    document.getElementById("codingItemTitle").textContent = "";
    document.getElementById("codingDupNotice").innerHTML = "";
    return;
  }
  document.getElementById("codingPersonName").textContent =
    `${rec.person_name} — ${rec.group_name} (${rec.person_title || "—"})`;
  document.getElementById("codingItemTitle").textContent = rec.item_title || "(untitled)";
  document.getElementById("codingDupNotice").innerHTML = duplicateSectionHtml(rec);
}

function syncCodingToCurrentInterview() {
  const rec = state.filtered[state.pos];
  renderCodingMeta(rec);
  if (rec && rec.item_id != null) {
    loadCodingSegments(rec.item_id);
  } else {
    codingState.segments = [];
    renderSegments();
  }
  refreshProgress();
}

// --- Events -----------------------------------------------------------------------------

function bindCodingEvents() {
  document.getElementById("codingPrevBtn").addEventListener("click", () => {
    prev(); // from app.js
    syncCodingToCurrentInterview();
  });
  document.getElementById("codingNextBtn").addEventListener("click", () => {
    next(); // from app.js
    syncCodingToCurrentInterview();
  });
  document.getElementById("toggleCodebook").addEventListener("click", () => {
    document.getElementById("codebookPanel").classList.toggle("hidden");
  });
  document.getElementById("toggleDatasets").addEventListener("click", () => {
    document.getElementById("datasetsPanel").classList.toggle("hidden");
  });
  document.getElementById("toggleSchemes").addEventListener("click", () => {
    document.getElementById("schemesPanel").classList.toggle("hidden");
  });
  document.getElementById("schemeSwitcher").addEventListener("change", (e) => {
    switchScheme(e.target.value);
  });
  document.getElementById("addSchemeBtn").addEventListener("click", addScheme);
  document.getElementById("newSchemeName").addEventListener("keydown", (e) => {
    if (e.key === "Enter") addScheme();
  });
  document.getElementById("schemesList").addEventListener("click", (e) => {
    if (e.target.classList.contains("scheme-archive-btn")) {
      archiveScheme(e.target.dataset.id);
    }
  });
  document.getElementById("cancelSubThemeBtn").addEventListener("click", resetPendingSubTheme);
  document.getElementById("codingMinWords").addEventListener("input", (e) => {
    codingState.minWords = Number(e.target.value) || 0;
    if (codingState.focusIndex >= 0 && (codingState.segments[codingState.focusIndex].word_count || 0) < codingState.minWords) {
      codingState.focusIndex = -1;
    }
    renderSegments();
  });
  document.getElementById("addThemeBtn").addEventListener("click", addTheme);
  document.getElementById("newThemeName").addEventListener("keydown", (e) => {
    if (e.key === "Enter") addTheme();
  });

  document.getElementById("themeList").addEventListener("click", (e) => {
    if (e.target.classList.contains("theme-add-sub-btn")) {
      startAddSubTheme(e.target.dataset.id);
    } else if (e.target.classList.contains("theme-edit-btn")) {
      startEditUI(e.target.dataset.id);
    } else if (e.target.classList.contains("theme-edit-save")) {
      saveThemeEdit(e.target.dataset.id);
    } else if (e.target.classList.contains("theme-edit-cancel")) {
      renderThemeList();
    } else if (e.target.classList.contains("theme-archive-btn")) {
      archiveTheme(e.target.dataset.id);
    } else if (e.target.classList.contains("theme-merge-btn")) {
      startMergeUI(e.target.dataset.id);
    } else if (e.target.classList.contains("theme-merge-confirm")) {
      const row = e.target.closest(".theme-row");
      const select = row.querySelector(".merge-target-select");
      confirmMerge(e.target.dataset.id, select.value);
    } else if (e.target.classList.contains("theme-merge-cancel")) {
      renderThemeList();
    }
  });

  document.getElementById("themeList").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.classList.contains("theme-edit-name")) {
      e.preventDefault();
      const row = e.target.closest(".theme-row");
      saveThemeEdit(row.dataset.themeId);
    }
  });

  document.getElementById("datasetsList").addEventListener("click", (e) => {
    if (e.target.classList.contains("dataset-exclude-btn")) {
      setDatasetStatus(e.target.dataset.id, "excluded");
    } else if (e.target.classList.contains("dataset-restore-btn")) {
      setDatasetStatus(e.target.dataset.id, "active");
    } else if (e.target.classList.contains("dataset-unload-btn")) {
      unloadDataset(e.target.dataset.id);
    } else if (e.target.classList.contains("dataset-segment-btn")) {
      segmentDatasets([e.target.dataset.id]);
    }
  });

  document.getElementById("segmentAllBtn").addEventListener("click", () => segmentDatasets(null));
  document.getElementById("scanDuplicatesBtn").addEventListener("click", scanForDuplicates);

  document.addEventListener("keydown", (e) => {
    if (document.body.dataset.tab !== "coding") return;
    const tag = document.activeElement.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA") return;
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      syncCodingToCurrentInterview(); // app.js's own listener already moved state.pos
    } else if (e.key === "j" || e.key === "ArrowDown") {
      e.preventDefault();
      moveSegmentFocus(1);
    } else if (e.key === "k" || e.key === "ArrowUp") {
      e.preventDefault();
      moveSegmentFocus(-1);
    } else if (e.key >= "1" && e.key <= "9") {
      toggleFocusedSegmentTheme(Number(e.key) - 1);
    }
  });

  window.onEnterCodingTab = syncCodingToCurrentInterview;
  window.onDatasetLoaded = () => {
    if (document.body.dataset.tab === "coding") syncCodingToCurrentInterview();
  };
}

async function initCoding() {
  bindCodingEvents();
  await loadSchemes();
  await loadThemes();
  await loadDatasetStatuses();
}

initCoding();
