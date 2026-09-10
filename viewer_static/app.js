const state = {
  datasetId: "primary",
  datasetStatuses: {}, // dataset_id -> "active"|"excluded", from /api/datasets -- see refreshDatasetList()
  all: [],
  filtered: [],
  pos: 0,
  cache: new Map(), // id -> full record (with transcript_text), fetched on demand
  duplicates: {}, // item_id -> {cluster_id, is_canonical, canonical_dataset_id, canonical_item_id,
                   //             size, needs_attention} from /api/duplicates/status, {} if no
                   // scripts/find_duplicates.py run exists yet -- see duplicateSectionHtml() below
  dateBounds: { min: 0, max: 0 }, // ms timestamps spanning the current dataset's publish_dates
  filters: {
    groupName: new Set(),
    personName: new Set(),
    personTitle: new Set(),
    channelSearch: "",
    titleSearch: "",
    minViews: 0,
    minLikes: 0,
    minScore: 0,
    dateFrom: null, // ms timestamp, null = unbounded
    dateTo: null, // ms timestamp, null = unbounded
    transcriptMatchIds: null, // Set of record ids from /api/transcript_search, null = no restriction
  },
};

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

// --- Shared background-job poll loop -----------------------------------------------
// Backs every viewer_server.py job-status endpoint (see jobs.py's JobRegistry and
// docs/DEVELOPMENT.md's "Background jobs" convention) -- Search & Export's query-fetch job
// below, and model.js's training job. Recursively setTimeouts against statusUrl until
// the job's status is no longer "running", calling onRunning(job) on each in-progress
// poll and onDone(job)/onError(job) once. Each job type's payload shape differs (a
// query job has items_fetched/pages_fetched, a training job has themes_done/
// current_theme), so this only owns the poll mechanics -- rendering stays with the
// caller. Returns a handle whose .cancel() stops the loop (mirrors the
// clearTimeout(stateObj.pollTimer) idiom each caller used before this existed, for a
// caller that wants to guard against overlapping poll chains).
function pollBackgroundJob(statusUrl, { onRunning, onDone, onError }, intervalMs = 1000) {
  const handle = { timer: null, cancel: () => clearTimeout(handle.timer) };
  async function tick() {
    const res = await fetch(statusUrl);
    const job = await res.json();
    if (job.status === "running") {
      onRunning(job);
      handle.timer = setTimeout(tick, intervalMs);
      return;
    }
    if (job.status === "done") onDone(job);
    else onError(job);
  }
  handle.timer = setTimeout(tick, intervalMs);
  return handle;
}

// --- Near-duplicate flag (scripts/find_duplicates.py) ------------------------------
// state.duplicates is keyed by item_id (not the dataset-local numeric `id`), populated
// by loadDataset() from /api/duplicates/status -- the automated run's clusters with any
// researcher duplicate_overrides layered on top (see coding_store.py). The detector isn't
// perfect, so every non-compact rendering also offers a correction: "Not a duplicate" on
// a wrongly-flagged item (records an 'exclude' override), "Mark as duplicate…" on an
// unflagged one (records an 'include' override against a chosen canonical item), and
// "Undo" on anything the researcher already corrected by hand. All of it is handled by
// delegated click/submit listeners in bindEvents() -- see submitMarkDuplicate()/
// applyDuplicateOverride()/removeDuplicateOverride() below.

function duplicateListFlagHtml(itemId) {
  const info = state.duplicates[itemId];
  if (!info || info.kind !== "duplicate" || info.is_canonical) return "";
  return `<span class="dup-badge dup-badge-duplicate" title="Near-duplicate of another interview -- excluded from classifier training">⧉ duplicate</span>`;
}

function duplicateCandidateChipsHtml(rec) {
  const candidates = state.all
    .filter((r) => r.item_id !== rec.item_id && r.person_name === rec.person_name)
    .slice(0, 12);
  if (!candidates.length) {
    return `<div class="hint">No other same-person interviews in this dataset -- enter dataset_id:item_id below.</div>`;
  }
  return (
    `<div class="hint">Same person, this dataset -- click to fill in:</div>` +
    `<div class="dup-candidates">` +
    candidates
      .map((r) => {
        const date = r.publish_date ? r.publish_date.slice(0, 10) : "—";
        return (
          `<button type="button" class="dup-candidate-chip" data-target="${escapeHtml(state.datasetId)}:${escapeHtml(r.item_id)}">` +
          `${escapeHtml(date)} · ${escapeHtml(r.item_title || "(untitled)")}</button>`
        );
      })
      .join("") +
    `</div>`
  );
}

function duplicateSectionHtml(rec) {
  const itemId = rec.item_id;
  const datasetId = state.datasetId;
  const info = state.duplicates[itemId];
  const attrs = `data-item-id="${escapeHtml(itemId)}" data-dataset-id="${escapeHtml(datasetId)}"`;

  // Not flagged, or a canonical whose cluster shrank to just itself (every other
  // member got excluded) -- either way, there's nothing to show but the option to
  // flag a duplicate, same as a plain unflagged item.
  if (!info || (info.kind === "duplicate" && info.is_canonical && (!info.size || info.size <= 1))) {
    return (
      `<div class="dup-section" ${attrs}>` +
      `<button class="btn-secondary dup-mark-open-btn">Mark as duplicate…</button>` +
      `<div class="dup-mark-form hidden">` +
      duplicateCandidateChipsHtml(rec) +
      `<input type="text" class="dup-target-input" placeholder="dataset_id:item_id">` +
      `<div class="dup-mark-form-actions">` +
      `<button class="dup-mark-submit-btn">Mark as duplicate</button>` +
      `<button class="btn-secondary dup-mark-cancel-btn">Cancel</button>` +
      `</div>` +
      `<div class="dup-mark-status hint"></div>` +
      `</div></div>`
    );
  }

  if (info.kind === "excluded") {
    return (
      `<div class="dup-section" ${attrs}>` +
      `<span class="dup-badge dup-badge-excluded">You marked this as not a duplicate.</span> ` +
      `<button class="btn-secondary dup-undo-exclude-btn">Undo</button>` +
      `</div>`
    );
  }

  if (info.is_canonical) {
    if (!info.size || info.size <= 1) return "";
    const n = info.size - 1;
    return `<div class="dup-section"><span class="dup-badge dup-badge-canonical">canonical of ${n} duplicate${n === 1 ? "" : "s"}</span></div>`;
  }

  const manual = info.source === "manual";
  return (
    `<div class="dup-section" ${attrs} ` +
    `data-canonical-dataset="${escapeHtml(info.canonical_dataset_id)}" data-canonical-item="${escapeHtml(info.canonical_item_id)}">` +
    `<span class="dup-badge dup-badge-duplicate">⧉ near-duplicate of another interview -- excluded from classifier training` +
    `${manual ? " (manually flagged)" : ""}. <a href="#" class="dup-jump-link">View the canonical copy</a></span> ` +
    `<button class="btn-secondary ${manual ? "dup-undo-include-btn" : "dup-not-duplicate-btn"}">${manual ? "Undo" : "Not a duplicate"}</button>` +
    `</div>`
  );
}

async function refreshDuplicateStatus() {
  const res = await fetch(`/api/duplicates/status?dataset=${encodeURIComponent(state.datasetId)}`);
  state.duplicates = await res.json();
  renderList();
  showCurrent();
  if (document.body.dataset.tab === "coding" && window.onEnterCodingTab) window.onEnterCodingTab();
}

function parseDuplicateTarget(raw) {
  const idx = raw.indexOf(":");
  if (idx <= 0 || idx === raw.length - 1) return null;
  return { datasetId: raw.slice(0, idx), itemId: raw.slice(idx + 1) };
}

async function submitMarkDuplicate(section) {
  const statusEl = section.querySelector(".dup-mark-status");
  const target = parseDuplicateTarget(section.querySelector(".dup-target-input").value.trim());
  if (!target) {
    statusEl.textContent = "Enter as dataset_id:item_id.";
    return;
  }
  statusEl.textContent = "Saving…";
  let data;
  try {
    const res = await fetch("/api/duplicates/override", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        dataset_id: section.dataset.datasetId, item_id: section.dataset.itemId, action: "include",
        canonical_dataset_id: target.datasetId, canonical_item_id: target.itemId,
      }),
    });
    data = await res.json();
  } catch (err) {
    statusEl.textContent = "Request failed — is the server running?";
    return;
  }
  if (data.error) {
    statusEl.textContent = "Error: " + data.error;
    return;
  }
  await refreshDuplicateStatus();
}

async function applyDuplicateOverride(section, action) {
  let data;
  try {
    const res = await fetch("/api/duplicates/override", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dataset_id: section.dataset.datasetId, item_id: section.dataset.itemId, action }),
    });
    data = await res.json();
  } catch (err) {
    flashCopyStatus("Request failed — is the server running?");
    return;
  }
  if (data.error) {
    flashCopyStatus("Error: " + data.error);
    return;
  }
  await refreshDuplicateStatus();
}

async function removeDuplicateOverride(section) {
  let data;
  try {
    const res = await fetch("/api/duplicates/override/remove", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dataset_id: section.dataset.datasetId, item_id: section.dataset.itemId }),
    });
    data = await res.json();
  } catch (err) {
    flashCopyStatus("Request failed — is the server running?");
    return;
  }
  if (data.error) {
    flashCopyStatus("Error: " + data.error);
    return;
  }
  await refreshDuplicateStatus();
}

function uniqueSorted(field) {
  const vals = new Set(
    state.all.map((r) => r[field]).filter((v) => v !== null && v !== undefined && v !== "")
  );
  return [...vals].sort((a, b) => (a > b ? 1 : a < b ? -1 : 0));
}

function renderCheckboxes(containerId, values, filterKey) {
  const container = document.getElementById(containerId);
  container.innerHTML = "";
  values.forEach((v) => {
    const label = document.createElement("label");
    label.className = "checkbox-item";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = v;
    cb.addEventListener("change", () => {
      if (cb.checked) state.filters[filterKey].add(v);
      else state.filters[filterKey].delete(v);
      applyFilters();
    });
    label.appendChild(cb);
    label.appendChild(document.createTextNode(" " + v));
    container.appendChild(label);
  });
}

function buildFilterOptions() {
  renderCheckboxes("groupFilters", uniqueSorted("group_name"), "groupName");
  renderCheckboxes("personFilters", uniqueSorted("person_name"), "personName");
  renderCheckboxes("personTitleFilters", uniqueSorted("person_title"), "personTitle");
  setupDateSlider();
}

function matchesFilters(rec) {
  const f = state.filters;
  if (f.groupName.size && !f.groupName.has(rec.group_name)) return false;
  if (f.personName.size && !f.personName.has(rec.person_name)) return false;
  if (f.personTitle.size && !f.personTitle.has(rec.person_title)) return false;
  if (f.dateFrom !== null || f.dateTo !== null) {
    const ts = rec.publish_date ? Date.parse(rec.publish_date) : NaN;
    if (Number.isNaN(ts)) return false;
    if (f.dateFrom !== null && ts < f.dateFrom) return false;
    // dateTo is a day-aligned (midnight) bound; include the whole selected day.
    if (f.dateTo !== null && ts >= f.dateTo + ONE_DAY_MS) return false;
  }
  if (
    f.channelSearch &&
    !(rec.source_name || "").toLowerCase().includes(f.channelSearch)
  )
    return false;
  if (
    f.titleSearch &&
    !(rec.item_title || "").toLowerCase().includes(f.titleSearch)
  )
    return false;
  if (f.transcriptMatchIds && !f.transcriptMatchIds.has(rec.id)) return false;
  if (f.minViews && (rec.view_count || 0) < f.minViews) return false;
  if (f.minLikes && (rec.like_count || 0) < f.minLikes) return false;
  if (f.minScore && (rec.combined_classifier_score || 0) < f.minScore) return false;
  return true;
}

// --- Date range slider ------------------------------------------------------------

const ONE_DAY_MS = 24 * 60 * 60 * 1000;

function computeDateBounds() {
  const timestamps = state.all
    .map((r) => (r.publish_date ? Date.parse(r.publish_date) : NaN))
    .filter((t) => !Number.isNaN(t));
  if (!timestamps.length) return { min: 0, max: 0 };
  let min = timestamps[0], max = timestamps[0];
  for (const t of timestamps) {
    if (t < min) min = t;
    if (t > max) max = t;
  }
  // Snap both ends to whole UTC days: publish_date carries a time-of-day, so a
  // 1-day slider step anchored at an unaligned min would never land exactly on
  // max, and the browser silently snaps the initial value down a day.
  const floorDay = (ts) => Math.floor(ts / ONE_DAY_MS) * ONE_DAY_MS;
  return { min: floorDay(min), max: floorDay(max) };
}

function formatDate(ts) {
  return new Date(Number(ts)).toISOString().slice(0, 10);
}

function updateDateRangeLabel() {
  const fromEl = document.getElementById("dateFromSlider");
  const toEl = document.getElementById("dateToSlider");
  document.getElementById("dateRangeLabel").textContent =
    state.dateBounds.min === state.dateBounds.max
      ? "no dated interviews"
      : `${formatDate(fromEl.value)} – ${formatDate(toEl.value)}`;
}

function setupDateSlider() {
  const bounds = computeDateBounds();
  state.dateBounds = bounds;
  const fromEl = document.getElementById("dateFromSlider");
  const toEl = document.getElementById("dateToSlider");
  [fromEl, toEl].forEach((el) => {
    el.min = bounds.min;
    el.max = bounds.max || bounds.min; // avoid a degenerate max < min range
    el.step = bounds.max > bounds.min ? ONE_DAY_MS : 1;
  });
  fromEl.value = bounds.min;
  toEl.value = bounds.max;
  state.filters.dateFrom = null;
  state.filters.dateTo = null;
  updateDateRangeLabel();
}

function bindDateSliderEvents() {
  const fromEl = document.getElementById("dateFromSlider");
  const toEl = document.getElementById("dateToSlider");
  fromEl.addEventListener("input", () => {
    if (Number(fromEl.value) > Number(toEl.value)) fromEl.value = toEl.value;
    state.filters.dateFrom = Number(fromEl.value);
    updateDateRangeLabel();
    applyFilters();
  });
  toEl.addEventListener("input", () => {
    if (Number(toEl.value) < Number(fromEl.value)) toEl.value = fromEl.value;
    state.filters.dateTo = Number(toEl.value);
    updateDateRangeLabel();
    applyFilters();
  });
}

// --- Interview (transcript) contains filter ----------------------------------------

let transcriptSearchToken = 0;
let transcriptSearchDebounce = null;

async function runTranscriptSearch(term) {
  const token = ++transcriptSearchToken;
  const statusEl = document.getElementById("transcriptSearchStatus");

  if (!term || term.length < 2) {
    state.filters.transcriptMatchIds = null;
    statusEl.textContent = "";
    applyFilters();
    return;
  }

  statusEl.textContent = "Searching transcripts…";
  let data;
  try {
    const res = await fetch(
      `/api/transcript_search?dataset=${encodeURIComponent(state.datasetId)}&q=${encodeURIComponent(term)}`
    );
    data = await res.json();
  } catch (err) {
    if (token !== transcriptSearchToken) return; // a newer search superseded this one
    statusEl.textContent = "Search failed — is the server running?";
    return;
  }
  if (token !== transcriptSearchToken) return; // stale response, a newer search is in flight

  if (data.error) {
    statusEl.textContent = "Error: " + data.error;
    return;
  }
  const ids = data.ids || [];
  state.filters.transcriptMatchIds = new Set(ids);
  statusEl.textContent = `${ids.length} match${ids.length === 1 ? "" : "es"}`;
  applyFilters();
}

function applyFilters() {
  state.filtered = state.all.filter(matchesFilters); // server already sorted by publish_date
  state.pos = 0;
  renderList();
  renderResultCount();
  showCurrent();
}

function renderResultCount() {
  document.getElementById("resultCount").textContent =
    `${state.filtered.length} / ${state.all.length} interviews`;
}

function renderList() {
  const container = document.getElementById("interviewList");
  container.innerHTML = "";
  state.filtered.forEach((rec, idx) => {
    const div = document.createElement("div");
    div.className = "list-item" + (idx === state.pos ? " active" : "");
    div.dataset.idx = String(idx);
    const date = rec.publish_date ? rec.publish_date.slice(0, 10) : "—";
    div.innerHTML =
      `<span class="li-date">${escapeHtml(date)} · ${escapeHtml(rec.group_name)}</span>` +
      `<span class="li-exec">${escapeHtml(rec.person_name)}</span>` +
      `<span class="li-title">${escapeHtml(rec.item_title || "(untitled)")}</span>` +
      duplicateListFlagHtml(rec.item_id);
    div.addEventListener("click", () => {
      state.pos = idx;
      showCurrent();
      // #interviewList is shared between Browse and Coding (see styles.css --
      // only #mainPanel/#codingMain toggle by tab), but showCurrent() only
      // renders Browse's own pane. Sync Coding's pane too when it's the
      // active tab, same hook switchTab("coding") itself uses.
      if (document.body.dataset.tab === "coding" && window.onEnterCodingTab) window.onEnterCodingTab();
    });
    container.appendChild(div);
  });
}

function updateListActive() {
  const container = document.getElementById("interviewList");
  [...container.children].forEach((child, idx) => {
    child.classList.toggle("active", idx === state.pos);
  });
  const active = container.children[state.pos];
  if (active) active.scrollIntoView({ block: "nearest" });
}

function formatDuration(secs) {
  if (secs === null || secs === undefined) return "—";
  secs = Math.round(secs);
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  const s = secs % 60;
  const mm = String(m).padStart(h ? 2 : 1, "0");
  const ss = String(s).padStart(2, "0");
  return h ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function formatNumber(n) {
  if (n === null || n === undefined || n === "") return "—";
  const num = Number(n);
  return Number.isNaN(num) ? String(n) : num.toLocaleString();
}

function renderMeta(rec) {
  document.getElementById("metaExecutive").textContent =
    `${rec.person_name} — ${rec.group_name} (${rec.person_title || "—"})`;

  const itemTitleEl = document.getElementById("metaItemTitle");
  const title = escapeHtml(rec.item_title || "(untitled)");
  itemTitleEl.innerHTML = rec.source_url
    ? `<a href="${escapeHtml(rec.source_url)}" target="_blank" rel="noopener">${title}</a>`
    : title;

  const date = rec.publish_date ? rec.publish_date.slice(0, 10) : "unknown date";
  const parts = [
    date,
    rec.source_name || "unknown source",
    `duration ${formatDuration(rec.duration_secs)}`,
    `${formatNumber(rec.view_count)} views`,
    `${formatNumber(rec.like_count)} likes`,
    rec.combined_classifier_score !== null && rec.combined_classifier_score !== undefined
      ? `score ${Number(rec.combined_classifier_score).toFixed(2)}`
      : null,
  ].filter(Boolean);
  document.getElementById("metaDetails").innerHTML = parts.map(escapeHtml).join(
    '<span> · </span>'
  );
  document.getElementById("metaDupNotice").innerHTML = duplicateSectionHtml(rec);
}

function renderTranscript(text) {
  if (!text) return "<em>No transcript text available for this interview.</em>";
  return text
    .split("\n")
    .map((line) => {
      const m = line.match(/^\[(.*?)\]\s*(.*)$/);
      if (m) {
        return `<p><strong>[${escapeHtml(m[1])}]</strong> ${escapeHtml(m[2])}</p>`;
      }
      return line.trim() ? `<p>${escapeHtml(line)}</p>` : "";
    })
    .join("");
}

function updateNavButtons() {
  document.getElementById("prevBtn").disabled = state.pos <= 0;
  document.getElementById("nextBtn").disabled = state.pos >= state.filtered.length - 1;
}

function updatePositionCounter() {
  document.getElementById("positionCounter").textContent = state.filtered.length
    ? `Interview ${state.pos + 1} of ${state.filtered.length}`
    : "No interviews match the current filters";
}

async function showCurrent() {
  updatePositionCounter();
  updateNavButtons();
  updateListActive();

  if (!state.filtered.length) {
    document.getElementById("metaExecutive").textContent = "";
    document.getElementById("metaItemTitle").textContent = "";
    document.getElementById("metaDetails").textContent = "";
    document.getElementById("transcriptContent").innerHTML =
      "<em>No interviews match the current filters.</em>";
    return;
  }

  const meta = state.filtered[state.pos];
  renderMeta(meta);
  document.getElementById("transcriptContent").innerHTML = "<em>Loading transcript…</em>";

  let rec = state.cache.get(meta.id);
  if (!rec) {
    const res = await fetch(
      `/api/interview/${meta.id}?dataset=${encodeURIComponent(state.datasetId)}`
    );
    rec = await res.json();
    state.cache.set(meta.id, rec);
  }

  // Guard against out-of-order responses if the researcher navigated quickly.
  if (state.filtered[state.pos] && state.filtered[state.pos].id === meta.id) {
    document.getElementById("transcriptContent").innerHTML = renderTranscript(rec.transcript_text);
  }
}

function prev() {
  if (state.pos > 0) {
    state.pos--;
    showCurrent();
  }
}

function next() {
  if (state.pos < state.filtered.length - 1) {
    state.pos++;
    showCurrent();
  }
}

function buildCitationTag(rec) {
  const date = rec.publish_date ? rec.publish_date.slice(0, 10) : "unknown date";
  return `[${rec.group_name} — ${rec.person_name} (${rec.person_title || "—"}) — "${rec.item_title || "untitled"}" — ${date} — ${rec.source_name || "unknown source"} — item_id:${rec.item_id}]`;
}

// --- Web Appendix: filter-snapshot logging -----------------------------------------
// Logged only at deliberate, already-existing actions that "consume" the current
// filter state (a citation copy, a new download) -- not on every filter change,
// which would swamp the Web Appendix's activity log with noise. Best-effort: a
// failed log call never blocks the researcher's actual action.

function serializeFilters() {
  const f = state.filters;
  return {
    datasetId: state.datasetId,
    groupName: [...f.groupName],
    personName: [...f.personName],
    personTitle: [...f.personTitle],
    channelSearch: f.channelSearch,
    titleSearch: f.titleSearch,
    minViews: f.minViews,
    minLikes: f.minLikes,
    minScore: f.minScore,
    dateFrom: f.dateFrom,
    dateTo: f.dateTo,
    transcriptMatchCount: f.transcriptMatchIds ? f.transcriptMatchIds.size : null,
  };
}

function postFilterSnapshot() {
  fetch("/api/appendix/filter_snapshot", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filters: serializeFilters() }),
  }).catch(() => {});
}

function flashCopyStatus(msg) {
  const el = document.getElementById("copyStatus");
  el.textContent = msg;
  setTimeout(() => {
    if (el.textContent === msg) el.textContent = "";
  }, 2000);
}

function resetFilters() {
  state.filters = {
    groupName: new Set(),
    personName: new Set(),
    personTitle: new Set(),
    channelSearch: "",
    titleSearch: "",
    minViews: 0,
    minLikes: 0,
    minScore: 0,
    dateFrom: null,
    dateTo: null,
    transcriptMatchIds: null,
  };
  document
    .querySelectorAll("#filterPanel input[type=checkbox]")
    .forEach((cb) => (cb.checked = false));
  document.querySelectorAll("#filterPanel input[type=text], #filterPanel input[type=number]")
    .forEach((inp) => (inp.value = ""));
  document.getElementById("transcriptSearchStatus").textContent = "";
  transcriptSearchToken++; // discard any in-flight transcript search response
  const fromEl = document.getElementById("dateFromSlider");
  const toEl = document.getElementById("dateToSlider");
  fromEl.value = state.dateBounds.min;
  toEl.value = state.dateBounds.max;
  updateDateRangeLabel();
  applyFilters();
}

// Follows a "duplicate of ..." badge's link (app.js/coding.js) to its canonical copy --
// switches dataset first if the canonical item lives in a different one (cross-dataset
// duplicates are real, e.g. the same download re-run into a second queries/*.json file),
// then clears filters so the target can't be hidden by whatever filter state was active.
async function jumpToDuplicateCanonical(canonicalDatasetId, canonicalItemId) {
  if (canonicalDatasetId !== state.datasetId) {
    await refreshDatasetList(canonicalDatasetId);
    await loadDataset(canonicalDatasetId);
  }
  resetFilters();
  const idx = state.filtered.findIndex((r) => r.item_id === canonicalItemId);
  if (idx === -1) {
    flashCopyStatus("Couldn't locate the canonical record.");
    return;
  }
  state.pos = idx;
  showCurrent();
  if (document.body.dataset.tab === "coding" && window.onEnterCodingTab) window.onEnterCodingTab();
}

function bindEvents() {
  document.getElementById("prevBtn").addEventListener("click", prev);
  document.getElementById("nextBtn").addEventListener("click", next);

  document.getElementById("toggleFilters").addEventListener("click", () => {
    document.getElementById("filterPanel").classList.toggle("hidden");
  });

  bindDateSliderEvents();

  document.getElementById("channelSearch").addEventListener("input", (e) => {
    state.filters.channelSearch = e.target.value.toLowerCase();
    applyFilters();
  });
  document.getElementById("titleSearch").addEventListener("input", (e) => {
    state.filters.titleSearch = e.target.value.toLowerCase();
    applyFilters();
  });
  document.getElementById("transcriptSearch").addEventListener("input", (e) => {
    clearTimeout(transcriptSearchDebounce);
    const term = e.target.value.trim();
    transcriptSearchDebounce = setTimeout(() => runTranscriptSearch(term), 400);
  });
  document.getElementById("minViews").addEventListener("input", (e) => {
    state.filters.minViews = Number(e.target.value) || 0;
    applyFilters();
  });
  document.getElementById("minLikes").addEventListener("input", (e) => {
    state.filters.minLikes = Number(e.target.value) || 0;
    applyFilters();
  });
  document.getElementById("minScore").addEventListener("input", (e) => {
    state.filters.minScore = Number(e.target.value) || 0;
    applyFilters();
  });

  document.getElementById("resetFilters").addEventListener("click", resetFilters);

  document.addEventListener("click", (e) => {
    const link = e.target.closest(".dup-jump-link");
    if (link) {
      e.preventDefault();
      const section = link.closest(".dup-section");
      jumpToDuplicateCanonical(section.dataset.canonicalDataset, section.dataset.canonicalItem);
      return;
    }

    const openBtn = e.target.closest(".dup-mark-open-btn");
    if (openBtn) {
      openBtn.closest(".dup-section").querySelector(".dup-mark-form").classList.remove("hidden");
      openBtn.classList.add("hidden");
      return;
    }

    const cancelBtn = e.target.closest(".dup-mark-cancel-btn");
    if (cancelBtn) {
      const section = cancelBtn.closest(".dup-section");
      section.querySelector(".dup-mark-form").classList.add("hidden");
      section.querySelector(".dup-mark-open-btn").classList.remove("hidden");
      return;
    }

    const chip = e.target.closest(".dup-candidate-chip");
    if (chip) {
      chip.closest(".dup-section").querySelector(".dup-target-input").value = chip.dataset.target;
      return;
    }

    const submitBtn = e.target.closest(".dup-mark-submit-btn");
    if (submitBtn) {
      submitMarkDuplicate(submitBtn.closest(".dup-section"));
      return;
    }

    const notDupBtn = e.target.closest(".dup-not-duplicate-btn");
    if (notDupBtn) {
      applyDuplicateOverride(notDupBtn.closest(".dup-section"), "exclude");
      return;
    }

    const undoBtn = e.target.closest(".dup-undo-include-btn, .dup-undo-exclude-btn");
    if (undoBtn) {
      removeDuplicateOverride(undoBtn.closest(".dup-section"));
      return;
    }
  });

  document.getElementById("copyBtn").addEventListener("click", async () => {
    const sel = window.getSelection().toString().trim();
    if (!sel || !state.filtered.length) return;
    const meta = state.filtered[state.pos];
    const rec = state.cache.get(meta.id) || meta;
    const tag = buildCitationTag(rec);
    try {
      await navigator.clipboard.writeText(`${tag}\n${sel}`);
      flashCopyStatus("Copied with citation");
      postFilterSnapshot();
    } catch (err) {
      flashCopyStatus("Copy failed — clipboard permission?");
    }
  });

  document.addEventListener("selectionchange", () => {
    const sel = window.getSelection();
    const content = document.getElementById("transcriptContent");
    const btn = document.getElementById("copyBtn");
    btn.disabled = !(sel && sel.toString().trim() && content.contains(sel.anchorNode));
  });

  document.addEventListener("keydown", (e) => {
    const tag = document.activeElement.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA") return;
    if (e.key === "ArrowLeft") prev();
    else if (e.key === "ArrowRight") next();
  });
}

// --- Dataset loading (Browse tab) --------------------------------------------------

async function loadDataset(datasetId) {
  state.datasetId = datasetId;
  state.cache.clear(); // record ids are only unique within a dataset
  state.filters.transcriptMatchIds = null; // ids from the old dataset don't apply here
  transcriptSearchToken++; // discard any in-flight transcript search response
  document.getElementById("transcriptSearch").value = "";
  document.getElementById("transcriptSearchStatus").textContent = "";
  const [indexRes, dupRes] = await Promise.all([
    fetch(`/api/index?dataset=${encodeURIComponent(datasetId)}`),
    fetch(`/api/duplicates/status?dataset=${encodeURIComponent(datasetId)}`),
  ]);
  state.all = await indexRes.json();
  state.duplicates = await dupRes.json();
  buildFilterOptions();
  applyFilters();
  renderDatasetStatusHint();
  if (window.onDatasetLoaded) window.onDatasetLoaded();
}

async function refreshDatasetList(selectId) {
  const res = await fetch("/api/datasets");
  const datasets = await res.json();
  state.datasetStatuses = {};
  datasets.forEach((d) => { state.datasetStatuses[d.id] = d.status; });
  const select = document.getElementById("datasetSelect");
  const previous = selectId || select.value || state.datasetId;
  select.innerHTML = "";
  datasets.forEach((d) => {
    const opt = document.createElement("option");
    opt.value = d.id;
    opt.textContent = `${d.label} (${d.count})`;
    select.appendChild(opt);
  });
  if (datasets.some((d) => d.id === previous)) {
    select.value = previous;
  } else if (datasets.length) {
    select.value = datasets[0].id;
  }
  renderDatasetStatusHint();
  return select.value;
}

// Small non-blocking hint next to the topbar dataset selector for a dataset flagged
// "excluded" from classifier training (see coding.js's Datasets panel) -- otherwise
// that status would be invisible while browsing/coding it, since Browse/Coding
// don't filter it out at all. "unloaded" datasets never reach this select in the
// first place (see viewer_server.list_datasets()), so there's nothing to show for them.
function renderDatasetStatusHint() {
  const el = document.getElementById("datasetStatusHint");
  const excluded = state.datasetStatuses[state.datasetId] === "excluded";
  el.textContent = excluded ? "Excluded from classifier training" : "";
  el.classList.toggle("hidden", !excluded);
}

// --- Tabs -----------------------------------------------------------------------

function switchTab(tab) {
  document.body.dataset.tab = tab;
  document.getElementById("tabBrowseBtn").classList.toggle("active", tab === "browse");
  document.getElementById("tabSearchBtn").classList.toggle("active", tab === "search");
  const analyticsBtn = document.getElementById("tabAnalyticsBtn");
  if (analyticsBtn) analyticsBtn.classList.toggle("active", tab === "analytics");
  const codingBtn = document.getElementById("tabCodingBtn");
  if (codingBtn) codingBtn.classList.toggle("active", tab === "coding");
  const modelBtn = document.getElementById("tabModelBtn");
  if (modelBtn) modelBtn.classList.toggle("active", tab === "model");
  const reviewBtn = document.getElementById("tabReviewBtn");
  if (reviewBtn) reviewBtn.classList.toggle("active", tab === "review");
  const appendixBtn = document.getElementById("tabAppendixBtn");
  if (appendixBtn) appendixBtn.classList.toggle("active", tab === "appendix");
  if (tab === "analytics" && window.onEnterAnalyticsTab) window.onEnterAnalyticsTab();
  if (tab === "coding" && window.onEnterCodingTab) window.onEnterCodingTab();
  if (tab === "model" && window.onEnterModelTab) window.onEnterModelTab();
  if (tab === "review" && window.onEnterReviewTab) window.onEnterReviewTab();
  if (tab === "appendix" && window.onEnterAppendixTab) window.onEnterAppendixTab();
}

// --- Search / Import tab -----------------------------------------------------------

const searchState = {
  selected: null, // {kind, id, label, companyName}
  jobId: null,
  pollTimer: null,
};

function renderCompanyResults(results) {
  const container = document.getElementById("companyResults");
  container.innerHTML = "";
  if (!results.length) {
    container.innerHTML = '<div class="hint">No matches</div>';
    return;
  }
  results.forEach((c) => {
    const name = c.name || c.full_name || `Company ${c.company_id}`;
    const div = document.createElement("div");
    div.className = "result-item";
    div.innerHTML =
      `<span class="ri-name">${escapeHtml(name)}</span>` +
      `<span class="ri-sub">${c.ticker ? escapeHtml(c.ticker) + " · " : ""}company_id ${c.company_id}</span>`;
    div.addEventListener("click", () => {
      selectResult({ kind: "company", id: c.company_id, label: name, companyName: name }, div);
    });
    container.appendChild(div);
  });
}

function renderEntityResults(results) {
  const container = document.getElementById("entityResults");
  container.innerHTML = "";
  if (!results.length) {
    container.innerHTML = '<div class="hint">No matches</div>';
    return;
  }
  results.forEach((e) => {
    const companyName = e.institution || (e.company && e.company.full_name) || "";
    const div = document.createElement("div");
    div.className = "result-item";
    div.innerHTML =
      `<span class="ri-name">${escapeHtml(e.name)}</span>` +
      `<span class="ri-sub">${e.title ? escapeHtml(e.title) + " · " : ""}${escapeHtml(companyName)} · entity_id ${e.id}</span>`;
    div.addEventListener("click", () => {
      selectResult({ kind: "entity", id: e.id, label: e.name, companyName }, div);
    });
    container.appendChild(div);
  });
}

async function fetchSearch(kind, keyword) {
  try {
    const res = await fetch(`/api/search/${kind}?keyword=${encodeURIComponent(keyword)}`);
    return await res.json();
  } catch (err) {
    return { error: "request failed — is the server running?" };
  }
}

async function runSearch() {
  const keyword = document.getElementById("searchKeyword").value.trim();
  if (!keyword) return;

  const statusEl = document.getElementById("searchStatus");
  statusEl.textContent = "Searching…";
  const companyContainer = document.getElementById("companyResults");
  const entityContainer = document.getElementById("entityResults");
  companyContainer.innerHTML = "";
  entityContainer.innerHTML = "";

  // Run both lookups independently — one side erroring (e.g. an upstream API
  // hiccup) shouldn't blank out results the other side already found.
  const [companies, entities] = await Promise.all([
    fetchSearch("companies", keyword),
    fetchSearch("entities", keyword),
  ]);

  statusEl.textContent = "";
  if (companies.error) {
    companyContainer.innerHTML =
      `<div class="hint error">Company search failed: ${escapeHtml(companies.error)}<br>` +
      `Try searching for one of the company's executives instead — their result card shows the company too.</div>`;
  } else {
    renderCompanyResults(companies.results || []);
  }

  if (entities.error) {
    entityContainer.innerHTML = `<div class="hint error">Executive search failed: ${escapeHtml(entities.error)}</div>`;
  } else {
    renderEntityResults(entities.results || []);
  }
}

function selectResult(sel, itemEl) {
  searchState.selected = sel;
  document.querySelectorAll(".result-item").forEach((el) => el.classList.remove("selected"));
  if (itemEl) itemEl.classList.add("selected");

  document.getElementById("exportPanel").classList.remove("hidden");
  document.getElementById("selectedLabel").textContent =
    sel.kind === "company"
      ? `Company: ${sel.label}  (company_id ${sel.id})`
      : `Executive: ${sel.label} — ${sel.companyName || "unknown company"}  (entity_id ${sel.id})`;
  document.getElementById("jobStatus").textContent = "";
  document.getElementById("jobResult").classList.add("hidden");
  document.getElementById("fetchExportBtn").disabled = false;
}

async function startExportJob() {
  if (!searchState.selected) return;
  const after = document.getElementById("afterDate").value || null;
  const before = document.getElementById("beforeDate").value || null;
  const btn = document.getElementById("fetchExportBtn");
  btn.disabled = true;
  document.getElementById("jobResult").classList.add("hidden");
  document.getElementById("jobStatus").textContent = "Starting…";

  let data;
  try {
    const res = await fetch("/api/query/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        kind: searchState.selected.kind,
        id: searchState.selected.id,
        label: searchState.selected.label,
        company_name: searchState.selected.companyName,
        after,
        before,
      }),
    });
    data = await res.json();
  } catch (err) {
    document.getElementById("jobStatus").textContent = "Request failed — is the server running?";
    btn.disabled = false;
    return;
  }

  if (data.error) {
    document.getElementById("jobStatus").textContent = "Error: " + data.error;
    btn.disabled = false;
    return;
  }
  searchState.jobId = data.job_id;
  pollJob();
}

function pollJob() {
  searchState.pollTimer?.cancel();
  searchState.pollTimer = pollBackgroundJob(
    `/api/query/status?job_id=${encodeURIComponent(searchState.jobId)}`,
    {
      onRunning: (job) => {
        document.getElementById("jobStatus").textContent =
          `Fetching… ${job.items_fetched} interviews so far (page ${job.pages_fetched})`;
      },
      onDone: (job) => {
        document.getElementById("fetchExportBtn").disabled = false;
        document.getElementById("jobStatus").textContent = "Done.";
        document.getElementById("jobResult").classList.remove("hidden");
        postFilterSnapshot();
        document.getElementById("jobResultText").innerHTML =
          `Saved <strong>${escapeHtml(job.dataset_label)}</strong> to ` +
          `<code>queries/${escapeHtml(job.json_file)}</code> and <code>queries/${escapeHtml(job.csv_file)}</code>.`;
        document.getElementById("viewDatasetBtn").onclick = async () => {
          await refreshDatasetList(job.dataset_id);
          await loadDataset(job.dataset_id);
          switchTab("browse");
        };
      },
      onError: (job) => {
        document.getElementById("fetchExportBtn").disabled = false;
        document.getElementById("jobStatus").textContent = "Error: " + job.error;
      },
    },
  );
}

function bindSearchEvents() {
  document.getElementById("searchBtn").addEventListener("click", runSearch);
  document.getElementById("searchKeyword").addEventListener("keydown", (e) => {
    if (e.key === "Enter") runSearch();
  });
  document.getElementById("fetchExportBtn").addEventListener("click", startExportJob);

  document.getElementById("tabBrowseBtn").addEventListener("click", () => switchTab("browse"));
  document.getElementById("tabSearchBtn").addEventListener("click", () => switchTab("search"));
  const analyticsBtn = document.getElementById("tabAnalyticsBtn");
  if (analyticsBtn) analyticsBtn.addEventListener("click", () => switchTab("analytics"));
  const codingBtn = document.getElementById("tabCodingBtn");
  if (codingBtn) codingBtn.addEventListener("click", () => switchTab("coding"));
  const modelBtn = document.getElementById("tabModelBtn");
  if (modelBtn) modelBtn.addEventListener("click", () => switchTab("model"));
  const reviewBtn = document.getElementById("tabReviewBtn");
  if (reviewBtn) reviewBtn.addEventListener("click", () => switchTab("review"));
  const appendixBtn = document.getElementById("tabAppendixBtn");
  if (appendixBtn) appendixBtn.addEventListener("click", () => switchTab("appendix"));

  document.getElementById("datasetSelect").addEventListener("change", (e) => {
    loadDataset(e.target.value);
  });
}

async function init() {
  bindEvents();
  bindSearchEvents();
  const selected = await refreshDatasetList();
  if (selected) await loadDataset(selected);
}

init();
