// Train tab -> Codebook sub-page (docs/CODEBOOK_PLAN.md K2): prepare each theme's
// codebook entry for a model -- a definition, inclusion / exclusion criteria and
// boundary notes -- and see how complete each entry is (coding_store/codebook.py's
// theme_completeness(): definition length, criteria, exemplars, near misses, a positive
// in a read-in-full interview). Completeness is advisory only: it's flagged here, on
// the Models page and on the Code tab's theme rows, and never blocks training or export.
//
// Also owns the Train tab's Models | Codebook switcher. Shares codingState (the
// active scheme) and themesByParent() from coding.js, escapeHtml()/switchTab() from
// app.js, and extends model.js's window.onEnterModelTab. Loaded after model.js and
// storage.js.

const codebookState = {
  subpage: "models", // "models" | "codebook"
  overview: [], // GET /api/codebook/overview rows for the active scheme
  selectedThemeId: null,
  dirty: false, // unsaved edits in the form
  examples: { exemplars: [], near_misses: [] }, // GET /api/codebook/examples for the selected theme
};

function switchModelSubpage(name) {
  codebookState.subpage = name;
  document.getElementById("modelModelsPage").classList.toggle("hidden", name !== "models");
  document.getElementById("codebookPage").classList.toggle("hidden", name !== "codebook");
  document.querySelectorAll(".model-subnav-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.subpage === name);
  });
  if (name === "codebook") loadCodebook();
}

async function loadCodebook() {
  const schemeId = codingState.activeSchemeId;
  const scheme = (codingState.schemes || []).find((s) => s.scheme_id === schemeId);
  document.getElementById("codebookSchemeLabel").textContent = scheme ? `Scheme: ${scheme.name}` : "";
  if (!schemeId) {
    codebookState.overview = [];
  } else {
    const res = await fetch(`/api/codebook/overview?scheme_id=${encodeURIComponent(schemeId)}`);
    const data = await res.json();
    codebookState.overview = data.error ? [] : data;
  }
  if (!codebookState.overview.some((t) => t.theme_id === codebookState.selectedThemeId)) {
    codebookState.selectedThemeId = null;
    codebookState.dirty = false;
  }
  renderCodebookList();
  renderCodebookNote();
  if (codebookState.selectedThemeId && !codebookState.dirty) renderCodebookDetail();
  else if (!codebookState.selectedThemeId) showCodebookDetail(false);
}

function renderCodebookList() {
  const container = document.getElementById("codebookThemeItems");
  container.innerHTML = "";
  if (!codebookState.overview.length) {
    container.innerHTML = '<div class="hint">No themes in this scheme yet.</div>';
    return;
  }
  const byParent = themesByParent(codebookState.overview);
  const ordered = [];
  const walk = (parentId) => (byParent.get(parentId) || []).forEach((t) => { ordered.push(t); walk(t.theme_id); });
  walk(null);
  const placed = new Set(ordered.map((t) => t.theme_id));
  codebookState.overview.forEach((t) => { if (!placed.has(t.theme_id)) ordered.push(t); });

  ordered.forEach((t) => {
    const c = t.completeness;
    const div = document.createElement("div");
    div.className = "codebook-theme-item" + (t.theme_id === codebookState.selectedThemeId ? " selected" : "");
    div.dataset.themeId = t.theme_id;
    div.style.paddingLeft = 4 + Math.min(t.depth || 0, 6) * 16 + "px";
    const missing = missingText(c);
    div.innerHTML =
      `<span class="theme-dot" style="background:${escapeHtml(t.color || "#6b7fd7")}"></span>` +
      `<span class="codebook-theme-item-name">${escapeHtml(t.name)}</span>` +
      `<span class="codebook-score${c.score === c.total ? " complete" : ""}" ` +
      `title="${escapeHtml(missing || "Complete")}">${c.score}/${c.total}</span>`;
    container.appendChild(div);
  });
}

function showCodebookDetail(show) {
  document.getElementById("codebookDetailEmpty").classList.toggle("hidden", show);
  document.getElementById("codebookDetailContent").classList.toggle("hidden", !show);
}

function selectedCodebookTheme() {
  return codebookState.overview.find((t) => t.theme_id === codebookState.selectedThemeId);
}

function selectCodebookTheme(themeId) {
  if (codebookState.dirty && themeId !== codebookState.selectedThemeId &&
      !confirm("You have unsaved changes to this theme's codebook entry. Discard them?")) return;
  codebookState.selectedThemeId = themeId;
  codebookState.dirty = false;
  renderCodebookList();
  renderCodebookDetail();
}

function renderCodebookDetail() {
  const t = selectedCodebookTheme();
  if (!t) {
    showCodebookDetail(false);
    return;
  }
  showCodebookDetail(true);
  document.getElementById("codebookThemeName").textContent = t.name;
  document.getElementById("codebookDefinition").value = t.description || "";
  document.getElementById("codebookInclusion").value = t.inclusion_criteria || "";
  document.getElementById("codebookExclusion").value = t.exclusion_criteria || "";
  document.getElementById("codebookBoundary").value = t.boundary_notes || "";
  document.getElementById("codebookSaveStatus").textContent = "";
  renderCodebookChecklist(t.completeness);
  updateDefinitionCount();
  loadCodebookExamples(t.theme_id);
}

// The theme's flagged exemplars and near misses (coding_store/examples.py), so the
// whole set can be reviewed in one place before exporting it.
async function loadCodebookExamples(themeId) {
  const res = await fetch(`/api/codebook/examples?theme_id=${encodeURIComponent(themeId)}`);
  const data = await res.json();
  if (codebookState.selectedThemeId !== themeId || data.error) return;
  codebookState.examples = data;
  renderExampleList("codebookExemplars", "codebookExemplarCount", data.exemplars, "exemplar",
    "None flagged yet.");
  renderExampleList("codebookNearMisses", "codebookNearMissCount", data.near_misses, "near_miss",
    "None flagged yet.");
}

function renderExampleList(listId, countId, rows, kind, emptyText) {
  document.getElementById(countId).textContent = `(${rows.length})`;
  const el = document.getElementById(listId);
  if (!rows.length) {
    el.innerHTML = `<div class="hint">${escapeHtml(emptyText)}</div>`;
    return;
  }
  el.innerHTML = rows.map((r) => {
    const twin = r.twin_of.length
      ? `<span class="codebook-twin" title="Near-identical to another flagged passage (${escapeHtml(r.twin_of.join(", "))}). ` +
        `A model would see it twice; consider unflagging one.">possible twin</span>` : "";
    return `<div class="codebook-example" data-segment-id="${escapeHtml(r.segment_id)}" data-kind="${kind}">` +
      `<div class="codebook-example-meta">${escapeHtml(r.person_name || "")}` +
      `${r.group_name ? " \u2014 " + escapeHtml(r.group_name) : ""} ${twin}</div>` +
      `<div class="codebook-example-text">${escapeHtml(r.text)}</div>` +
      `<div class="codebook-example-note">${r.note ? "Note: " + escapeHtml(r.note) : '<span class="hint">No note</span>'}</div>` +
      `<div class="codebook-example-actions">` +
      `<button type="button" class="btn-secondary example-open-btn">Open in Code</button>` +
      `<button type="button" class="btn-secondary example-note-btn">${r.note ? "Edit note" : "Add note"}</button>` +
      `<button type="button" class="btn-secondary example-unflag-btn">Unflag</button>` +
      `</div></div>`;
  }).join("");
}

async function updateExampleFlag(segmentId, kind, flagged, note) {
  const res = await fetch("/api/codebook/flag", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ segment_id: segmentId, theme_id: codebookState.selectedThemeId, kind, flagged, note }),
  });
  const data = await res.json();
  if (data.error) {
    alert(data.error);
    return;
  }
  await loadCodebook(); // counts and checklist
  loadCodebookExamples(codebookState.selectedThemeId);
}

function onExampleAction(e) {
  const card = e.target.closest(".codebook-example");
  if (!card) return;
  const kind = card.dataset.kind;
  const rows = kind === "exemplar" ? codebookState.examples.exemplars : codebookState.examples.near_misses;
  const row = rows.find((r) => r.segment_id === card.dataset.segmentId);
  if (!row) return;
  if (e.target.classList.contains("example-open-btn")) {
    jumpToSegmentInCoding(row); // coding.js
  } else if (e.target.classList.contains("example-note-btn")) {
    const question = kind === "exemplar"
      ? "Why is this a clear, typical case of the theme?"
      : "What makes this not the theme, even though it looks like it?";
    const note = prompt(question, row.note || "");
    if (note !== null) updateExampleFlag(row.segment_id, kind, true, note);
  } else if (e.target.classList.contains("example-unflag-btn")) {
    updateExampleFlag(row.segment_id, kind, false, null);
  }
}

function renderCodebookChecklist(c) {
  const hints = {
    exemplars: "Flag with the \u2606 next to a coded theme in the Code tab, or press e in a Review queue.",
    near_misses: "Press m on a passage in a Review queue.",
    gold: "Mark an interview “fully coded” after reading it end to end, or confirm a bulk mark.",
  };
  document.getElementById("codebookChecklist").innerHTML =
    `<li><strong>Codebook ${c.score}/${c.total}</strong> <span class="hint">(advisory: training and export always work)</span></li>` +
    ["definition", "criteria", "exemplars", "near_misses", "gold"].map((k) => {
      const item = c[k];
      const hint = !item.ok && hints[k] ? ` <span class="hint">${escapeHtml(hints[k])}</span>` : "";
      return `<li class="${item.ok ? "check-ok" : "check-missing"}">${item.ok ? "✓" : "○"} ` +
        `${escapeHtml(item.label)}: ${escapeHtml(item.detail)}${hint}</li>`;
    }).join("");
}

function updateDefinitionCount() {
  const words = document.getElementById("codebookDefinition").value.trim().split(/\s+/).filter(Boolean).length;
  document.getElementById("codebookDefinitionCount").textContent = `(${words} words; aim for 15+)`;
}

async function saveCodebookEntry() {
  const t = selectedCodebookTheme();
  if (!t) return;
  const statusEl = document.getElementById("codebookSaveStatus");
  statusEl.textContent = "Saving…";
  const res = await fetch(`/api/codebook/themes/${encodeURIComponent(t.theme_id)}/update`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      description: document.getElementById("codebookDefinition").value.trim(),
      inclusion_criteria: document.getElementById("codebookInclusion").value.trim(),
      exclusion_criteria: document.getElementById("codebookExclusion").value.trim(),
      boundary_notes: document.getElementById("codebookBoundary").value.trim(),
    }),
  });
  const data = await res.json();
  if (data.error) {
    statusEl.textContent = "Error: " + data.error;
    return;
  }
  codebookState.dirty = false;
  await loadCodebook(); // refreshes the checklist and every badge
  document.getElementById("codebookSaveStatus").textContent = "Saved.";
  if (typeof loadThemes === "function") loadThemes(); // coding.js: definitions/badges on the Code tab
}

// One line on the Models page when entries are incomplete (advisory, never blocking).
function renderCodebookNote() {
  const el = document.getElementById("modelCodebookNote");
  const rows = codebookState.overview;
  const incomplete = rows.filter((t) => t.completeness.score < t.completeness.total);
  const noCriteria = rows.filter((t) => !t.completeness.criteria.ok);
  el.innerHTML = incomplete.length
    ? `${incomplete.length} of ${rows.length} theme(s) have incomplete codebook entries` +
      (noCriteria.length ? ` (${noCriteria.length} without inclusion/exclusion criteria)` : "") +
      `. <a id="openCodebookLink">Open Codebook</a>`
    : "";
}

// Used by the Code tab's per-theme "Codebook n/5" badges (coding.js).
function openCodebookTheme(themeId) {
  switchTab("model");
  switchModelSubpage("codebook");
  codebookState.selectedThemeId = themeId;
  codebookState.dirty = false;
}

function bindCodebookEvents() {
  document.getElementById("modelSubnav").addEventListener("click", (e) => {
    const btn = e.target.closest(".model-subnav-btn");
    if (!btn || btn.dataset.subpage === codebookState.subpage) return;
    if (codebookState.dirty && !confirm("You have unsaved changes to a codebook entry. Discard them?")) return;
    codebookState.dirty = false;
    switchModelSubpage(btn.dataset.subpage);
  });
  document.getElementById("codebookThemeItems").addEventListener("click", (e) => {
    const item = e.target.closest(".codebook-theme-item");
    if (item) selectCodebookTheme(item.dataset.themeId);
  });
  ["codebookDefinition", "codebookInclusion", "codebookExclusion", "codebookBoundary"].forEach((id) => {
    document.getElementById(id).addEventListener("input", () => {
      codebookState.dirty = true;
      document.getElementById("codebookSaveStatus").textContent = "Unsaved changes";
      if (id === "codebookDefinition") updateDefinitionCount();
    });
  });
  document.getElementById("codebookSaveBtn").addEventListener("click", saveCodebookEntry);
  document.getElementById("codebookExemplars").addEventListener("click", onExampleAction);
  document.getElementById("codebookNearMisses").addEventListener("click", onExampleAction);
  document.getElementById("modelCodebookNote").addEventListener("click", (e) => {
    if (e.target.id === "openCodebookLink") switchModelSubpage("codebook");
  });
  // Extend model.js's Train-tab entry hook: keep the note (and an open Codebook page) current.
  const enterModels = window.onEnterModelTab;
  window.onEnterModelTab = () => {
    if (enterModels) enterModels();
    loadCodebook();
  };
}

bindCodebookEvents();
