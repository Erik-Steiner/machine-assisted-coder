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
// The page has two sub-tabs (app.js's setupSubnav, docs/CODEBOOK_PLAN.md U1): Recode,
// that multi-theme view (still the default), and Queues. On Queues, the Queue selector
// offers per-theme modes from GET /api/review/queue (coding_store/queues.py): highest
// scores, borderline, random check, likely mislabels, near-miss hunt, and overlap with
// other themes -- paged, and
// skipping passages already decided for the theme. In those queues each card is
// triaged with Accept / Reject / Skip (keys a / r / s, j / k to move, u to undo),
// recorded via POST /api/review/decisions (coding_store/decisions.py) with the
// queue context, so roadmap C2 can compute per-round accept rates and precision.
// Every card also explains itself (roadmap B3): the theme's top predictive terms
// are highlighted in its text (coding.js's highlightTermsHtml), and small bars show
// each scheme theme's score against its threshold (GET /api/review/segment_scores).
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
  mode: "recode", // "recode" (the Recode sub-tab) or a queues.py mode (Queues) -- set by applyReviewMode()
  queueMode: "top", // the Queues sub-tab's selected mode, kept while Recode is showing
  queueThemeId: null, // the one theme a queue mode is for
  queue: null, // last /api/review/queue response, items accumulated across "Load more"
  queueRequest: 0, // bumped per load, so a slow response for an old selection is dropped
  focusIndex: 0, // which visible queue card the triage keys act on
  undoStack: [], // decided candidates in this queue (same run + theme + mode), newest last -- kept
                 // across reloads, see loadModeQueue()
  undoRoundKey: null, // "model_version:theme_id:mode" the undo stack belongs to
  round: null, // {accept, reject, skip, total} for this queue's round (run + theme + mode)
  termsCache: {}, // "model_version:theme_id" -> [term, ...] (a run's top terms never change)
};

const SCORE_BATCH = 300; // the server's per-call cap on /api/review/segment_scores

// Fills candidate.scores ({theme_id: {score, threshold}}) for the score bars.
async function attachScores(candidates) {
  const scheme = codingState.activeSchemeId ? `&scheme_id=${encodeURIComponent(codingState.activeSchemeId)}` : "";
  for (let i = 0; i < candidates.length; i += SCORE_BATCH) {
    const batch = candidates.slice(i, i + SCORE_BATCH);
    const ids = batch.map((c) => c.segment_id).join(",");
    const res = await fetch(
      `/api/review/segment_scores?segment_ids=${encodeURIComponent(ids)}&engine=${encodeURIComponent(reviewState.engine)}${scheme}`,
    );
    const scores = await res.json();
    batch.forEach((c) => { c.scores = scores[c.segment_id] || {}; });
  }
}

async function topTermsFor(themeId, modelVersion) {
  const key = `${modelVersion}:${themeId}`;
  if (!(key in reviewState.termsCache)) {
    const res = await fetch(
      `/api/model/top_terms?theme_id=${encodeURIComponent(themeId)}&model_version=${encodeURIComponent(modelVersion)}`,
    );
    const data = await res.json();
    reviewState.termsCache[key] = (data.positive || []).map((t) => t.term);
  }
  return reviewState.termsCache[key];
}

// One small bar per scheme theme the passage was scored for, in codebook order:
// filled to the score, a tick at that theme's threshold, coloured when above it.
function scoreBarsHtml(scores) {
  if (!scores) return "";
  const rows = reviewState.themes
    .filter((t) => scores[t.theme_id])
    .map((t) => {
      const s = scores[t.theme_id];
      const pct = (v) => Math.max(0, Math.min(100, v * 100)).toFixed(1);
      const above = s.score >= s.threshold;
      const color = escapeHtml(t.color || "#6b7fd7");
      return `<div class="score-bar-row" title="${escapeHtml(t.name)}: ${s.score.toFixed(3)} ` +
        `(threshold ${s.threshold.toFixed(3)}${above ? ", above" : ", below"})">` +
        `<span class="score-bar-name">${escapeHtml(t.name)}</span>` +
        `<span class="score-bar-track"><span class="score-bar-fill${above ? " above" : ""}" ` +
        `style="width:${pct(s.score)}%;--bar-color:${color}"></span>` +
        `<span class="score-bar-threshold" style="left:${pct(s.threshold)}%"></span></span>` +
        `<span class="score-bar-value">${s.score.toFixed(2)}</span></div>`;
    });
  return rows.length ? `<div class="score-bars">${rows.join("")}</div>` : "";
}

const TRIAGE_KEYS_HINT = "Keys: j / k move · a accept · r reject · s skip · e accept as exemplar · " +
  "m reject as near miss · u undo.";

const REVIEW_MODE_HINTS = {
  top: "Highest-scoring passages you haven't decided for this theme yet: the quickest way to find more.",
  uncertain: "Passages the model is least sure about (scores nearest its threshold). Deciding these " +
    "teaches the next model the most.",
  audit: "A random sample of undecided passages above the threshold, in a fixed order for this run. " +
    "How many you accept here estimates the model's real precision.",
  suspect: "Passages you've already decided where the model most disagrees: coded passages it scores " +
    "low, and judged negatives it scores high. Scores come from cross-validation, so the model never " +
    "saw the passage. Possible miscodes, or a theme definition that has drifted.",
  boundary: "Passages judged not to be this theme (rejected, confirmed \u201cnone of these\u201d, or uncoded in " +
    "an interview read in full), the ones the model most thinks are the theme first. Press a if it is the " +
    "theme after all (a missed positive), m to keep it out and flag it as a near miss for the codebook, " +
    "r to keep it out, s to skip.",
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
  await renderReviewEngineOptions();
  renderReviewThemeCheckboxes();
  renderReviewQueueThemeSelect();
  renderReviewSchemeLabel();
  applyReviewMode();
}

// "Model scores from" lists the engine registry (model.js, GET /api/engines), never a
// hard-coded list (CODEBOOK_PLAN L0).
async function renderReviewEngineOptions() {
  await engineRegistryReady;
  const select = document.getElementById("reviewEngine");
  if (select.options.length) return;
  select.innerHTML = ENGINE_REGISTRY
    .map((e) => `<option value="${escapeHtml(e.id)}">${escapeHtml(e.label)}</option>`).join("");
  select.value = reviewState.engine;
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

// Loads whichever sub-tab is showing: Recode, or the queue mode picked on Queues. The
// sub-tab helper shows and hides each one's controls.
function applyReviewMode() {
  reviewState.mode = currentSubpage("review") === "recode" ? "recode" : reviewState.queueMode;
  if (reviewState.mode === "recode") {
    loadReviewCandidates(); // clears the queue itself when nothing's selected
  } else {
    loadModeQueue(false);
  }
}

async function loadModeQueue(append) {
  const listEl = document.getElementById("reviewModeList");
  const summaryEl = document.getElementById("reviewModeSummary");
  document.getElementById("reviewModeHint").textContent =
    `${REVIEW_MODE_HINTS[reviewState.mode] || ""} ${TRIAGE_KEYS_HINT}`;
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
  data.items.forEach((c) => { c.highlightTerms = data.top_terms || []; });
  await attachScores(data.items);
  if (request !== reviewState.queueRequest) return;
  if (append && reviewState.queue) {
    data.items = reviewState.queue.items.concat(data.items);
  } else {
    reviewState.focusIndex = 0;
    // Keep undo history across reloads of the same queue (e.g. after visiting another
    // tab); start fresh only when the run, theme or mode changes.
    const roundKey = `${data.model_version}:${reviewState.queueThemeId}:${reviewState.mode}`;
    if (reviewState.undoRoundKey !== roundKey) reviewState.undoStack = [];
    reviewState.undoRoundKey = roundKey;
    reviewState.round = null;
    if (data.model_version) {
      const roundRes = await fetch(
        `/api/review/round_summary?model_version=${encodeURIComponent(data.model_version)}` +
        `&theme_id=${encodeURIComponent(reviewState.queueThemeId)}&mode=${encodeURIComponent(reviewState.mode)}`,
      );
      if (request !== reviewState.queueRequest) return;
      reviewState.round = await roundRes.json();
    }
  }
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
  if (reviewState.mode === "boundary") {
    reasons = [
      { type: "judged", theme_id: tid },
      { type: "oof", theme_id: tid, score: item.rank_score },
    ];
  } else if (reviewState.mode === "suspect") {
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
  // The theme a triage decision is about: the *other* theme in the overlap queue.
  const confusion = reviewState.mode === "confusion";
  return {
    ...item, reasons, inQueue: true, decision: null, decisionId: null,
    decisionThemeId: confusion ? item.other_theme_id : tid,
    decisionScore: confusion ? item.other_score : item.score,
    decisionOof: confusion ? null : item.oof_score,
    decisionModelVersion: confusion ? item.other_model_version : modelVersion,
  };
}

function triageBarHtml(candidate) {
  const theme = reviewState.themes.find((t) => t.theme_id === candidate.decisionThemeId);
  const name = escapeHtml(theme ? theme.name : candidate.decisionThemeId);
  if (candidate.decision) {
    const label = { accept: `accepted as ${name}`, reject: `rejected for ${name}`, skip: "skipped" }[candidate.decision] +
      ({ exemplar: " \u2605 exemplar", near_miss: " \u00b7 near miss" }[candidate.decisionFlag] || "");
    return `<div class="triage-bar"><span class="triage-done triage-${candidate.decision}">${label}</span>` +
      `<span class="hint">u to undo the latest decision</span></div>`;
  }
  return `<div class="triage-bar">` +
    `<button type="button" class="triage-btn" data-decision="accept">Accept: is ${name} <kbd>a</kbd></button>` +
    `<button type="button" class="btn-secondary triage-btn" data-decision="reject">Reject: not ${name} <kbd>r</kbd></button>` +
    `<button type="button" class="btn-secondary triage-btn" data-decision="skip">Skip <kbd>s</kbd></button>` +
    `<button type="button" class="btn-secondary triage-btn" data-decision="accept" data-flag="exemplar" ` +
    `title="Accept, and flag as a clear, typical case for the codebook">\u2605 Exemplar <kbd>e</kbd></button>` +
    `<button type="button" class="btn-secondary triage-btn" data-decision="reject" data-flag="near_miss" ` +
    `title="Reject, and flag as a useful contrast: looks like the theme but isn't">Near miss <kbd>m</kbd></button>` +
    `</div>`;
}

function visibleQueueItems() {
  return reviewState.queue ? reviewState.queue.items.filter((c) => (c.word_count || 0) >= reviewState.minWords) : [];
}

async function decideFocused(decision, flag = null) {
  const items = visibleQueueItems();
  const candidate = items[reviewState.focusIndex];
  if (!candidate || candidate.decision) return;
  const res = await fetch("/api/review/decisions", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      segment_id: candidate.segment_id, theme_id: candidate.decisionThemeId, decision,
      mode: reviewState.mode, queue_theme_id: reviewState.queueThemeId, engine: reviewState.engine,
      model_version: reviewState.queue.model_version, score: candidate.decisionScore, oof_score: candidate.decisionOof,
      flag,
    }),
  });
  const data = await res.json();
  if (data.error) {
    document.getElementById("reviewModeSummary").textContent = "Error: " + data.error;
    return;
  }
  candidate.decision = decision;
  candidate.decisionFlag = flag;
  candidate.decisionId = data.decision.decision_id;
  candidate.prevState = data.decision.prev_state;
  candidate.prevFlag = data.decision.prev_flag;
  applyDecisionLocally(candidate, decision);
  if (flag === "exemplar") {
    candidate.exemplar_theme_ids = [...(candidate.exemplar_theme_ids || []).filter((id) => id !== candidate.decisionThemeId),
      candidate.decisionThemeId];
  } else if (decision === "reject") {
    candidate.exemplar_theme_ids = (candidate.exemplar_theme_ids || []).filter((id) => id !== candidate.decisionThemeId);
  }
  reviewState.twinWarning = (data.twins && data.twins.length)
    ? `Possible twin: that passage overlaps ${Math.round(data.twins[0].similarity * 100)}% with one already flagged ` +
      `for this theme. Both are kept; check Train \u2192 Codebook.`
    : "";
  reviewState.undoStack.push(candidate);
  reviewState.round = data.round;
  if (typeof scheduleCycleRefresh === "function") scheduleCycleRefresh(); // cycle.js
  // Move on to the next undecided card.
  const next = items.findIndex((c, i) => i > reviewState.focusIndex && !c.decision);
  if (next >= 0) reviewState.focusIndex = next;
  renderModeQueue();
}

// Mirror the server's label change (codes' exclusivity rule) on the card's chips.
function applyDecisionLocally(candidate, decision) {
  const tid = candidate.decisionThemeId;
  const without = (list) => (list || []).filter((id) => id !== tid);
  if (decision === "accept") {
    candidate.theme_ids = [...without(candidate.theme_ids), tid];
    candidate.rejected_theme_ids = without(candidate.rejected_theme_ids);
  } else if (decision === "reject") {
    candidate.theme_ids = without(candidate.theme_ids);
    candidate.rejected_theme_ids = [...without(candidate.rejected_theme_ids), tid];
  }
}

async function undoLastDecision() {
  const candidate = reviewState.undoStack[reviewState.undoStack.length - 1];
  if (!candidate) return;
  const res = await fetch("/api/review/decisions/undo", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision_id: candidate.decisionId }),
  });
  const data = await res.json();
  if (data.error) {
    document.getElementById("reviewModeSummary").textContent = "Error: " + data.error;
    return;
  }
  reviewState.undoStack.pop();
  const tid = candidate.decisionThemeId;
  const without = (list) => (list || []).filter((id) => id !== tid);
  candidate.theme_ids = candidate.prevState === "coded" ? [...without(candidate.theme_ids), tid] : without(candidate.theme_ids);
  candidate.rejected_theme_ids = candidate.prevState === "rejected"
    ? [...without(candidate.rejected_theme_ids), tid] : without(candidate.rejected_theme_ids);
  // The exemplar star comes back only if the original code row had it (prev_flag).
  const others = (candidate.exemplar_theme_ids || []).filter((id) => id !== tid);
  candidate.exemplar_theme_ids = candidate.prevState === "coded" && candidate.prevFlag ? [...others, tid] : others;
  candidate.decision = null;
  candidate.decisionFlag = null;
  candidate.decisionId = null;
  reviewState.round = data.round;
  if (typeof scheduleCycleRefresh === "function") scheduleCycleRefresh(); // cycle.js
  const shownAt = visibleQueueItems().indexOf(candidate);
  if (shownAt < 0) {
    // Decided before the queue was reloaded, so its card isn't on screen any more:
    // reload, and the undone passage reappears (it's undecided again).
    await loadModeQueue(false);
    return;
  }
  reviewState.focusIndex = shownAt;
  renderModeQueue();
}

function moveQueueFocus(delta) {
  const n = visibleQueueItems().length;
  if (!n) return;
  reviewState.focusIndex = Math.max(0, Math.min(n - 1, reviewState.focusIndex + delta));
  renderModeQueue();
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
  const visible = visibleQueueItems();
  const hiddenCount = q.items.length - visible.length;
  const r = reviewState.round;
  summaryEl.textContent =
    `Showing ${q.items.length} of ${q.total}` + (hiddenCount ? ` (${hiddenCount} hidden by min words)` : "") +
    ` · ${engineLabel} run ${q.model_version}, threshold ${q.threshold.toFixed(3)}` +
    (r ? ` · this round so far: ${r.accept} accepted, ${r.reject} rejected, ${r.skip} skipped` : "") +
    (reviewState.twinWarning ? ` · ${reviewState.twinWarning}` : "");
  reviewState.focusIndex = Math.min(reviewState.focusIndex, Math.max(0, visible.length - 1));
  if (!q.total) {
    listEl.innerHTML = reviewState.mode === "suspect"
      ? '<div class="hint">Nothing to show. If this theme&#39;s latest run is older than out-of-fold scores, retrain it.</div>'
      : '<div class="hint">Nothing to show for this theme in this view.</div>';
  }
  visible.forEach((c, i) => {
    const card = renderReviewCard(c);
    card.dataset.idx = String(i);
    card.insertAdjacentHTML("beforeend", triageBarHtml(c));
    card.classList.toggle("focused", i === reviewState.focusIndex);
    card.classList.toggle("decided", !!c.decision);
    listEl.appendChild(card);
  });
  const focused = listEl.querySelector(".exemplar-card.focused");
  if (focused) focused.scrollIntoView({ block: "nearest" });
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
  const candidates = await res.json();
  // Highlight the selected themes' top terms (from each theme's latest run of this engine).
  const runsRes = await fetch(`/api/coding/model_runs?engine=${encodeURIComponent(reviewState.engine)}`);
  const latest = await runsRes.json();
  const terms = [];
  for (const tid of reviewState.selectedThemeIds) {
    if (latest[tid]) terms.push(...await topTermsFor(tid, latest[tid].model_version));
  }
  candidates.forEach((c) => { c.highlightTerms = terms; });
  await attachScores(candidates);
  reviewState.candidates = candidates;
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
          (candidate.inQueue ? "" : `<button type="button" class="reason-action review-unreject-btn" data-theme="${tid}">Undo</button>`);
      }
      const action = candidate.inQueue || candidate.theme_ids.includes(r.theme_id)
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

  const chips = isInterviewer ? "" : buildChipsHtml(candidate.theme_ids, reviewState.themes, candidate.rejected_theme_ids,
    candidate.exemplar_theme_ids);
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
    `<div class="exemplar-text">${highlightTermsHtml(candidate.text, candidate.highlightTerms)}</div>` +
    scoreBarsHtml(candidate.scores) +
    (isInterviewer ? "" : `<div class="segment-chips">${chips}</div>`) +
    `<button class="btn-secondary review-view-btn">View in Code</button>`;

  div.addEventListener("click", (e) => {
    const triage = e.target.closest(".triage-btn");
    if (candidate.inQueue && div.dataset.idx !== undefined) {
      reviewState.focusIndex = Number(div.dataset.idx);
      document.querySelectorAll("#reviewModeList .exemplar-card").forEach((c) => c.classList.toggle("focused", c === div));
      if (triage) {
        decideFocused(triage.dataset.decision, triage.dataset.flag || null);
        return;
      }
    }
    const star = e.target.closest(".exemplar-star");
    if (star) {
      toggleExemplar(candidate, star.dataset.theme, rerenderReview);
      return;
    }
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
    reviewState.queueMode = e.target.value;
    applyReviewMode();
  });
  setupSubnav("review", document.getElementById("reviewSubnav"), {
    initial: "recode",
    onChange: applyReviewMode,
  });
  document.getElementById("reviewQueueTheme").addEventListener("change", (e) => {
    reviewState.queueThemeId = e.target.value;
    loadModeQueue(false);
  });
  document.getElementById("reviewLoadMore").addEventListener("click", () => loadModeQueue(true));
  document.addEventListener("keydown", (e) => {
    if (document.body.dataset.tab !== "review" || reviewState.mode === "recode") return;
    const tag = document.activeElement.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const actions = { a: "accept", r: "reject", s: "skip" };
    const flagged = { e: ["accept", "exemplar"], m: ["reject", "near_miss"] };
    if (actions[e.key]) {
      e.preventDefault();
      decideFocused(actions[e.key]);
    } else if (flagged[e.key]) {
      e.preventDefault();
      decideFocused(...flagged[e.key]);
    } else if (e.key === "u") {
      e.preventDefault();
      undoLastDecision();
    } else if (e.key === "j" || e.key === "ArrowDown") {
      e.preventDefault();
      moveQueueFocus(1);
    } else if (e.key === "k" || e.key === "ArrowUp") {
      e.preventDefault();
      moveQueueFocus(-1);
    }
  });
  window.onEnterReviewTab = () => loadReviewThemes();
}

function initReview() {
  bindReviewEvents();
}

initReview();
