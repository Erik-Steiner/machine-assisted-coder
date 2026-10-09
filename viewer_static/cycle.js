// The cycle strip (docs/CODEBOOK_PLAN.md U3): one line at the top of the Train and Review
// pages with the round, the four steps of the codebook -> train -> review -> decide cycle
// and their state, and the single most important reason as a link to the tab that resolves
// it. Any further reasons sit behind "why?". Advisory only: it never blocks a step.
//
// Every rule lives server-side in coding_store/iteration.py (GET /api/iteration/status);
// this file only draws the answer. Refreshed on entering Train or Review, after a codebook
// save or flag change (codebook.js), shortly after Review decisions (review.js, debounced),
// and when any background job finishes (app.js's window.onBackgroundJobDone).
//
// Shares app.js's goToSubpage()/escapeHtml() and coding.js's codingState (the active
// scheme). Loaded after review.js, since it extends both pages' entry hooks.

const cycleState = {
  status: null, // last GET /api/iteration/status response
  whyOpen: false, // the full reason list is showing
  request: 0, // bumped per load, so a slow response for an old scheme is dropped
  timer: null, // scheduleCycleRefresh()'s debounce
};

const CYCLE_STEPS = ["codebook", "train", "review", "decide"];
const CYCLE_STEP_LABELS = { codebook: "Codebook", train: "Train", review: "Review", decide: "Decide" };
const CYCLE_STATE_MARKS = { done: " ✓", current: " ●", stale: " !", todo: "" };
const CYCLE_STATE_TITLES = {
  done: "Done", current: "Next", stale: "Out of date: something changed since", todo: "Not yet",
};
const CYCLE_STRIP_IDS = ["trainCycleStrip", "reviewCycleStrip"];

async function refreshCycleStrips() {
  clearTimeout(cycleState.timer);
  const schemeId = codingState.activeSchemeId;
  const request = ++cycleState.request;
  if (!schemeId) {
    cycleState.status = null;
    renderCycleStrips();
    return;
  }
  let data = null;
  try {
    data = await (await fetch(`/api/iteration/status?scheme_id=${encodeURIComponent(schemeId)}`)).json();
  } catch (err) {
    data = null; // the strip is advisory: on a failed request it just stays empty
  }
  if (request !== cycleState.request) return;
  cycleState.status = data && !data.error ? data : null;
  renderCycleStrips();
}

// For bursts of changes (triaging a queue): one refresh after they pause.
function scheduleCycleRefresh() {
  clearTimeout(cycleState.timer);
  cycleState.timer = setTimeout(refreshCycleStrips, 1500);
}

function cycleReasonLinkHtml(reason) {
  return `<a class="cycle-reason-link" data-tab="${escapeHtml(reason.target.tab)}" ` +
    `data-subpage="${escapeHtml(reason.target.subpage)}">${escapeHtml(reason.text)} →</a>`;
}

function cycleStripHtml(status) {
  const steps = CYCLE_STEPS.map((step, i) => {
    const state = status.steps[step];
    return `<span class="cycle-step cycle-${state}" title="${escapeHtml(CYCLE_STATE_TITLES[state])}">` +
      `${i + 1} ${CYCLE_STEP_LABELS[step]}${CYCLE_STATE_MARKS[state]}</span>`;
  }).join('<span class="cycle-arrow">→</span>');
  const [top, ...rest] = status.reasons;
  const reason = top
    ? cycleReasonLinkHtml(top)
    : '<span class="hint">Up to date: the latest run reflects the codebook and labels, and you have reviewed it.</span>';
  const why = rest.length
    ? `<button type="button" class="cycle-why">${cycleState.whyOpen ? "hide" : `why? (+${rest.length})`}</button>` : "";
  const more = rest.length && cycleState.whyOpen
    ? `<ul class="cycle-more">${rest.map((r) => `<li>${cycleReasonLinkHtml(r)}</li>`).join("")}</ul>` : "";
  return `<div class="cycle-line"><span class="cycle-round">Round ${status.round}</span>` +
    `<span class="cycle-steps">${steps}</span><span class="cycle-reason">${reason}</span>${why}</div>${more}`;
}

function renderCycleStrips() {
  const status = cycleState.status;
  CYCLE_STRIP_IDS.forEach((id) => {
    const el = document.getElementById(id);
    el.classList.toggle("hidden", !status);
    el.innerHTML = status ? cycleStripHtml(status) : "";
  });
}

function bindCycleEvents() {
  CYCLE_STRIP_IDS.forEach((id) => {
    document.getElementById(id).addEventListener("click", (e) => {
      const link = e.target.closest(".cycle-reason-link");
      if (link) {
        goToSubpage(link.dataset.tab, link.dataset.subpage);
        return;
      }
      if (e.target.closest(".cycle-why")) {
        cycleState.whyOpen = !cycleState.whyOpen;
        renderCycleStrips();
      }
    });
  });
  const enterTrain = window.onEnterModelTab;
  window.onEnterModelTab = () => {
    if (enterTrain) enterTrain();
    refreshCycleStrips();
  };
  const enterReview = window.onEnterReviewTab;
  window.onEnterReviewTab = () => {
    if (enterReview) enterReview();
    refreshCycleStrips();
  };
  window.onBackgroundJobDone = refreshCycleStrips;
}

bindCycleEvents();
