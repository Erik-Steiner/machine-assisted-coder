// Review tab: recode/refine one or more themes at once. A cross-interview
// queue of every segment already coded under the selected theme(s), plus
// segments a trained model top-scores for them but that aren't coded yet --
// useful when two themes' boundaries turn out to overlap and need
// re-triaging, which is a recurring qualitative-research need, not a
// one-off. Recode actions are tagged source="recoded" (see coding_store.py's
// add_code) so they're distinguishable later from first-pass manual coding.
//
// Reuses coding.js's shared buildChipsHtml/postCode/deleteCode/
// jumpToSegmentInCoding rather than reimplementing segment-card rendering
// and code-toggling from scratch. Shares app.js's escapeHtml/switchTab.

const reviewState = {
  themes: [],
  loadedSchemeId: null, // last scheme these themes/checkboxes were loaded for
  selectedThemeIds: new Set(),
  candidates: [],
  minWords: 0, // live "min word count" filter, same idea as coding.js's -- hides, never deletes
  engine: "tfidf", // whose predictions fill the "model-flagged" list -- one engine at a time,
                   // never mixed (see coding_store/review.py's get_review_candidates)
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
  renderReviewSchemeLabel();
  loadReviewCandidates(); // clears the queue itself when nothing's selected
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

function reasonPillsHtml(candidate) {
  return candidate.reasons
    .map((r) => {
      const theme = reviewState.themes.find((t) => t.theme_id === r.theme_id);
      const name = theme ? theme.name : r.theme_id;
      return r.type === "coded"
        ? `<span class="reason-pill reason-coded">coded: ${escapeHtml(name)}</span>`
        : `<span class="reason-pill reason-predicted" title="${escapeHtml(r.model_version || "")}">` +
          `predicted: ${escapeHtml(name)} (${r.score.toFixed(2)} · ${escapeHtml(ENGINE_LABELS[r.engine] || r.engine || "?")})</span>`;
    })
    .join("");
}

function renderReviewCard(candidate) {
  const div = document.createElement("div");
  const isInterviewer = candidate.speaker_role === "interviewer";
  div.className = "exemplar-card";
  div.dataset.segmentId = candidate.segment_id;
  div.style.marginLeft = Math.min(candidate.depth || 0, 6) * 20 + "px";

  const chips = isInterviewer ? "" : buildChipsHtml(candidate.theme_ids, reviewState.themes);
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
  renderReviewQueue();
  try {
    if (active) await deleteCode(candidate.segment_id, themeId);
    else await postCode(candidate.segment_id, themeId, { source: "recoded" });
  } catch (err) {
    // leave the optimistic UI state as-is
  }
}

function bindReviewEvents() {
  document.getElementById("reviewMinWords").addEventListener("input", (e) => {
    reviewState.minWords = Number(e.target.value) || 0;
    renderReviewQueue();
  });
  document.getElementById("reviewEngine").addEventListener("change", (e) => {
    reviewState.engine = e.target.value;
    loadReviewCandidates();
  });
  window.onEnterReviewTab = () => loadReviewThemes();
}

function initReview() {
  bindReviewEvents();
}

initReview();
