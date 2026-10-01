// Review tab: recode/refine one or more themes at once. A cross-interview
// queue of every segment already coded under the selected theme(s), plus
// segments a trained model top-scores for them but that aren't coded yet --
// useful when two themes' boundaries turn out to overlap and need
// re-triaging, which is a recurring qualitative-research need, not a
// one-off. Recode actions are tagged source="recoded" (see coding_store.py's
// add_code) so they're distinguishable later from first-pass manual coding.
// A model-flagged segment can also be rejected for a theme ("✕ Not this
// theme"), tagged source="review_reject", which keeps it out of that theme's
// future queues (see coding_store/codes.py's add_negative()).
//
// Besides that recode view, the View selector offers per-theme queue modes from
// GET /api/review/queue (coding_store/queues.py): highest scores, borderline,
// random check, likely mislabels, and overlap with other themes -- paged, and
// skipping passages already decided for the theme. Same cards and actions.
//
// Reuses coding.js's shared buildChipsHtml/postCode/deleteCode/postNegative/
// deleteNegative/jumpToSegmentInCoding rather than reimplementing segment-card rendering
// and code-toggling from scratch. Shares app.js's escapeHtml/switchTab.

const reviewState = {
  themes: [],
  loadedSchemeId: null, // last scheme these themes/checkboxes were loaded for
  selectedThemeIds: new Set(),
  candidates: [],
  minWords: 0, // live "min word count" filter, same idea as coding.js's -- hides, never deletes
  engine: "tfidf", // whose predictions fill the "model-flagged" list -- one engine at a time,
                   // never mixed (see coding_store/review.py's get_review_candidates)
  mode: "recode", // "recode" (the multi-theme view above) or a queues.py mode
  queueThemeId: null, // the one theme a queue mode is for
  queue: null, // last /api/review/queue response, items accumulated across "Load more"
  queueRequest: 0, // bumped per load, so a slow response for an old selection is dropped
};

const REVIEW_MODE_HINTS = {
  top: "Highest-scoring passages you haven't decided for this theme yet: the quickest way to find more.",
  uncertain: "Passages the model is least sure about (scores nearest its threshold). Deciding these " +
    "teaches the next model the most.",
  audit: "A random sample of undecided passages above the threshold, in a fixed order for this run. " +
    "How many you accept here estimates the model's real precision.",
  suspect: "Passages you've already decided where the model most disagrees: coded passages it scores " +
    "low, and judged negatives it scores high. Scores come from cross-validation, so the model never " +
    "saw the passage. Possible miscodes, or a theme definition that has drifted.",
  confusion: "Passages coded with this theme that score above the threshold for a different theme in " +
    "the scheme, and haven't been coded or rejected for it: where theme boundaries may overlap.",
};

async function loadReviewThemes() {
  // codingState.activeSchemeId (Coding tab's scheme switcher) is read-only here.
  // A scheme switch clears the theme-id selection -- reviewing theme_ids from a
  // scheme that's no longer active would silently mix codebooks in the queue.
  if (reviewState.loadedSchemeId !== codingState.activeSchemeId) {
    reviewState.selectedThemeIds = new Set();
    reviewState.loadedSchemeId = codingState.activeSchemeId;
  }
  const schemePart = codingState.activeSchemeId
    ? `?scheme_id=${encodeURIComponent(codingState.activeSchemeId)}` : "";
  const res = await fetch(`/api/codebook/themes${schemePart}`);
  reviewState.themes = await res.json();
  renderReviewThemeCheckboxes();
  renderReviewQueueThemeSelect();
  renderReviewSchemeLabel();
  applyReviewMode();
}

function renderReviewQueueThemeSelect() {
  const select = document.getElementById("reviewQueueTheme");
  // Same tree order as the Code tab (coding.js's themesByParent), sub-codes indented.
  const byParent = themesByParent(reviewState.themes);
  const options = [];
  const walk = (parentId, depth) => {
    (byParent.get(parentId) || []).forEach((t) => {
      options.push(`<option value="${escapeHtml(t.theme_id)}">${"\u00a0\u00a0".repeat(depth)}${escapeHtml(t.name)}</option>`);
      walk(t.theme_id, depth + 1);
    });
  };
  walk(null, 0);
  select.innerHTML = options.join("");
  if (!reviewState.themes.some((t) => t.theme_id === reviewState.queueThemeId)) {
    reviewState.queueThemeId = reviewState.themes.length ? reviewState.themes[0].theme_id : null;
  }
  if (reviewState.queueThemeId) select.value = reviewState.queueThemeId;
}

// Shows the recode view or a queue mode's view, then loads whichever is active.
function applyReviewMode() {
  const recode = reviewState.mode === "recode";
  document.getElementById("reviewThemeSelect").classList.toggle("hidden", !recode);
  document.getElementById("reviewRecodeHint").classList.toggle("hidden", !recode);
  document.getElementById("reviewQueueThemeLabel").classList.toggle("hidden", recode);
  document.getElementById("reviewModeQueue").classList.toggle("hidden", recode);
  if (recode) {
    loadReviewCandidates(); // clears the queue itself when nothing's selected
  } else {
    document.getElementById("reviewEmpty").classList.add("hidden");
    document.getElementById("reviewQueue").classList.add("hidden");
    loadModeQueue(false);
  }
}

async function loadModeQueue(append) {
  const listEl = document.getElementById("reviewModeList");
  const summaryEl = document.getElementById("reviewModeSummary");
  document.getElementById("reviewModeHint").textContent = REVIEW_MODE_HINTS[reviewState.mode] || "";
  if (!reviewState.queueThemeId) {
    reviewState.queue = null;
    listEl.innerHTML = "";
    summaryEl.textContent = "No themes in this scheme yet.";
    document.getElementById("reviewLoadMore").classList.add("hidden");
    return;
  }
  const offset = append && reviewState.queue ? reviewState.queue.items.length : 0;
  if (!append) {
    listEl.innerHTML = "<em>Loading…</em>";
    summaryEl.textContent = "";
  }
  const request = ++reviewState.queueRequest;
  const res = await fetch(
    `/api/review/queue?theme_id=${encodeURIComponent(reviewState.queueThemeId)}` +
    `&mode=${encodeURIComponent(reviewState.mode)}&engine=${encodeURIComponent(reviewState.engine)}` +
    `&offset=${offset}&limit=25`,
  );
  const data = await res.json();
  if (request !== reviewState.queueRequest) return; // a newer selection superseded this one
  if (data.error) {
    listEl.innerHTML = "";
    summaryEl.textContent = "Error: " + data.error;
    return;
  }
  data.items = data.items.map((item) => queueItemToCandidate(item, data.model_version));
  if (append && reviewState.queue) data.items = reviewState.queue.items.concat(data.items);
  reviewState.queue = data;
  renderModeQueue();
}

// Turns a queue item into the candidate shape renderReviewCard() already draws:
// `reasons` says why it's here, in this mode's terms.
function queueItemToCandidate(item, modelVersion) {
  const tid = reviewState.queueThemeId;
  const predicted = (themeId, score, version) => ({
    type: "predicted", theme_id: themeId, score, engine: reviewState.engine, model_version: version,
  });
  let reasons;
  if (reviewState.mode === "suspect") {
    reasons = [
      { type: item.label === "positive" ? "coded" : "judged", theme_id: tid },
      { type: "oof", theme_id: tid, score: item.oof_score },
    ];
  } else if (reviewState.mode === "confusion") {
    reasons = [{ type: "coded", theme_id: tid },
      predicted(item.other_theme_id, item.other_score, item.other_model_version)];
  } else {
    reasons = [predicted(tid, item.score, modelVersion)];
  }
  return { ...item, reasons };
}

function renderModeQueue() {
  const q = reviewState.queue;
  const listEl = document.getElementById("reviewModeList");
  const summaryEl = document.getElementById("reviewModeSummary");
  const engineLabel = ENGINE_LABELS[reviewState.engine] || reviewState.engine;
  listEl.innerHTML = "";
  if (!q.model_version) {
    summaryEl.textContent = `No ${engineLabel} run for this theme yet — train it in the Train tab.`;
    document.getElementById("reviewLoadMore").classList.add("hidden");
    return;
  }
  const visible = q.items.filter((c) => (c.word_count || 0) >= reviewState.minWords);
  const hiddenCount = q.items.length - visible.length;
  summaryEl.textContent =
    `Showing ${q.items.length} of ${q.total}` + (hiddenCount ? ` (${hiddenCount} hidden by min words)` : "") +
    ` · ${engineLabel} run ${q.model_version}, threshold ${q.threshold.toFixed(3)}`;
  if (!q.total) {
    listEl.innerHTML = reviewState.mode === "suspect"
      ? '<div class="hint">Nothing to show. If this theme&#39;s latest run is older than out-of-fold scores, retrain it.</div>'
      : '<div class="hint">Nothing to show for this theme in this view.</div>';
  }
  visible.forEach((c) => listEl.appendChild(renderReviewCard(c)));
  document.getElementById("reviewLoadMore").classList.toggle("hidden", q.items.length >= q.total);
}

// Re-render whichever view is showing after a code/rejection changes a card.
function rerenderReview() {
  if (reviewState.mode === "recode") renderReviewQueue();
  else if (reviewState.queue) renderModeQueue();
}

function renderReviewSchemeLabel() {
  const el = document.getElementById("reviewSchemeLabel");
  if (!el) return;
  const scheme = (codingState.schemes || []).find((s) => s.scheme_id === codingState.activeSchemeId);
  el.textContent = scheme ? `Scheme: ${scheme.name}` : "";
}

function renderReviewThemeCheckboxes() {
  const container = document.getElementById("reviewThemeSelect");
  container.innerHTML = "";
  reviewState.themes.forEach((t) => {
    const label = document.createElement("label");
    label.className = "checkbox-item";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = t.theme_id;
    cb.checked = reviewState.selectedThemeIds.has(t.theme_id);
    cb.addEventListener("change", () => {
      if (cb.checked) reviewState.selectedThemeIds.add(t.theme_id);
      else reviewState.selectedThemeIds.delete(t.theme_id);
      loadReviewCandidates();
    });
    label.appendChild(cb);
    label.appendChild(document.createTextNode(" " + t.name));
    container.appendChild(label);
  });
}

async function loadReviewCandidates() {
  const codedList = document.getElementById("reviewCodedList");
  const predictedList = document.getElementById("reviewPredictedList");

  if (!reviewState.selectedThemeIds.size) {
    reviewState.candidates = [];
    codedList.innerHTML = "";
    predictedList.innerHTML = "";
    document.getElementById("reviewEmpty").classList.remove("hidden");
    document.getElementById("reviewQueue").classList.add("hidden");
    return;
  }

  document.getElementById("reviewEmpty").classList.add("hidden");
  document.getElementById("reviewQueue").classList.remove("hidden");
  codedList.innerHTML = "<em>Loading…</em>";
  predictedList.innerHTML = "";

  const themeIds = [...reviewState.selectedThemeIds].join(",");
  const res = await fetch(
    `/api/coding/review_candidates?theme_ids=${encodeURIComponent(themeIds)}` +
    `&predicted_limit=30&engine=${encodeURIComponent(reviewState.engine)}`,
  );
  reviewState.candidates = await res.json();
  renderReviewQueue();
}

function renderReviewQueue() {
  const visible = reviewState.candidates.filter((c) => (c.word_count || 0) >= reviewState.minWords);
  const coded = visible.filter((c) => c.reasons.some((r) => r.type === "coded"));
  const predicted = visible
    .filter((c) => !c.reasons.some((r) => r.type === "coded"))
    .sort((a, b) => maxScore(b) - maxScore(a));

  document.getElementById("reviewCodedCount").textContent = coded.length;
  document.getElementById("reviewPredictedCount").textContent = predicted.length;

  const codedList = document.getElementById("reviewCodedList");
  const predictedList = document.getElementById("reviewPredictedList");
  codedList.innerHTML = "";
  predictedList.innerHTML = "";

  if (!coded.length) codedList.innerHTML = '<div class="hint">No segments currently coded under the selected theme(s).</div>';
  if (!predicted.length) {
    const engineLabel = ENGINE_LABELS[reviewState.engine] || reviewState.engine;
    predictedList.innerHTML =
      `<div class="hint">No additional ${escapeHtml(engineLabel)}-flagged candidates (no ${escapeHtml(engineLabel)} ` +
      `run for the selected theme(s) yet, or all top candidates are already coded).</div>`;
  }

  coded.forEach((c) => codedList.appendChild(renderReviewCard(c)));
  predicted.forEach((c) => predictedList.appendChild(renderReviewCard(c)));
}

function maxScore(candidate) {
  return Math.max(0, ...candidate.reasons.filter((r) => r.type === "predicted").map((r) => r.score));
}

// A predicted reason gets a "Not this theme" button that records an explicit rejection
// (coding.js's postNegative()), so the segment stays out of future queues for that
// theme. A rejected reason stays on screen (with Undo) until the queue is reloaded.
function reasonPillsHtml(candidate) {
  const rejected = candidate.rejected_theme_ids || [];
  return candidate.reasons
    .map((r) => {
      const theme = reviewState.themes.find((t) => t.theme_id === r.theme_id);
      const name = theme ? theme.name : r.theme_id;
      if (r.type === "coded") {
        return `<span class="reason-pill reason-coded">coded: ${escapeHtml(name)}</span>`;
      }
      if (r.type === "judged") {
        return `<span class="reason-pill reason-judged">judged not: ${escapeHtml(name)}</span>`;
      }
      if (r.type === "oof") {
        return `<span class="reason-pill reason-oof" title="Out-of-fold score: from a cross-validation ` +
          `model that never saw this passage">model, unseen: ${r.score.toFixed(2)}</span>`;
      }
      const tid = escapeHtml(r.theme_id);
      if (rejected.includes(r.theme_id)) {
        return `<span class="reason-pill reason-rejected">rejected: ${escapeHtml(name)}</span>` +
          `<button type="button" class="reason-action review-unreject-btn" data-theme="${tid}">Undo</button>`;
      }
      const action = candidate.theme_ids.includes(r.theme_id)
        ? "" // coded since the queue loaded -- nothing to reject
        : `<button type="button" class="reason-action review-reject-btn" data-theme="${tid}" ` +
          `title="Record that this segment is not ${escapeHtml(name)}, so it stops being suggested">✕ Not this theme</button>`;
      return `<span class="reason-pill reason-predicted" title="${escapeHtml(r.model_version || "")}">` +
        `predicted: ${escapeHtml(name)} (${r.score.toFixed(2)} · ${escapeHtml(ENGINE_LABELS[r.engine] || r.engine || "?")})</span>` +
        action;
    })
    .join("");
}

function renderReviewCard(candidate) {
  const div = document.createElement("div");
  const isInterviewer = candidate.speaker_role === "interviewer";
  div.className = "exemplar-card";
  div.dataset.segmentId = candidate.segment_id;
  div.style.marginLeft = Math.min(candidate.depth || 0, 6) * 20 + "px";

  const chips = isInterviewer ? "" : buildChipsHtml(candidate.theme_ids, reviewState.themes, candidate.rejected_theme_ids);
  const roleBadge = isInterviewer
    ? '<span class="seg-role-badge seg-role-interviewer">interviewer — not coded</span>'
    : "";

  div.innerHTML =
    `<div class="exemplar-header">` +
    `<span>${escapeHtml(candidate.group_name)} — ${escapeHtml(candidate.person_name || "")}</span>` +
    `<span>${escapeHtml(candidate.speaker_name || "")}</span>` +
    roleBadge +
    `</div>` +
    `<div class="reason-pills">${reasonPillsHtml(candidate)}</div>` +
    `<div class="exemplar-text">${escapeHtml(candidate.text)}</div>` +
    (isInterviewer ? "" : `<div class="segment-chips">${chips}</div>`) +
    `<button class="btn-secondary review-view-btn">View in Code</button>`;

  div.addEventListener("click", (e) => {
    const chip = e.target.closest(".theme-chip");
    if (chip) {
      toggleReviewCode(candidate, chip.dataset.theme);
      return;
    }
    if (e.target.classList.contains("review-reject-btn")) {
      setReviewRejection(candidate, e.target.dataset.theme, true);
      return;
    }
    if (e.target.classList.contains("review-unreject-btn")) {
      setReviewRejection(candidate, e.target.dataset.theme, false);
      return;
    }
    if (e.target.classList.contains("review-view-btn")) {
      jumpToSegmentInCoding(candidate);
    }
  });

  return div;
}

async function toggleReviewCode(candidate, themeId) {
  const active = candidate.theme_ids.includes(themeId);
  candidate.theme_ids = active
    ? candidate.theme_ids.filter((id) => id !== themeId)
    : [...candidate.theme_ids, themeId];
  // Coding cancels a rejection server-side (codes.add_code()); mirror that locally.
  if (!active) candidate.rejected_theme_ids = (candidate.rejected_theme_ids || []).filter((id) => id !== themeId);
  rerenderReview();
  try {
    if (active) await deleteCode(candidate.segment_id, themeId);
    else await postCode(candidate.segment_id, themeId, { source: "recoded" });
  } catch (err) {
    // leave the optimistic UI state as-is
  }
}

async function setReviewRejection(candidate, themeId, reject) {
  const rejected = (candidate.rejected_theme_ids || []).filter((id) => id !== themeId);
  candidate.rejected_theme_ids = reject ? [...rejected, themeId] : rejected;
  // Rejecting cancels a code server-side (codes.add_negative()); mirror that locally.
  if (reject) candidate.theme_ids = candidate.theme_ids.filter((id) => id !== themeId);
  rerenderReview();
  try {
    if (reject) await postNegative(candidate.segment_id, themeId, { source: "review_reject" });
    else await deleteNegative(candidate.segment_id, themeId);
  } catch (err) {
    // leave the optimistic UI state as-is
  }
}

function bindReviewEvents() {
  document.getElementById("reviewMinWords").addEventListener("input", (e) => {
    reviewState.minWords = Number(e.target.value) || 0;
    rerenderReview();
  });
  document.getElementById("reviewEngine").addEventListener("change", (e) => {
    reviewState.engine = e.target.value;
    applyReviewMode();
  });
  document.getElementById("reviewMode").addEventListener("change", (e) => {
    reviewState.mode = e.target.value;
    applyReviewMode();
  });
  document.getElementById("reviewQueueTheme").addEventListener("change", (e) => {
    reviewState.queueThemeId = e.target.value;
    loadModeQueue(false);
  });
  document.getElementById("reviewLoadMore").addEventListener("click", () => loadModeQueue(true));
  window.onEnterReviewTab = () => loadReviewThemes();
}

function initReview() {
  bindReviewEvents();
}

initReview();
