const state = {
  all: [],
  filtered: [],
  pos: 0,
  cache: new Map(), // id -> full record (with transcript_text), fetched on demand
  filters: {
    company: new Set(),
    executive: new Set(),
    title: new Set(),
    year: new Set(),
    channelSearch: "",
    titleSearch: "",
    minViews: 0,
    minLikes: 0,
    minScore: 0,
  },
};

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
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
  renderCheckboxes("companyFilters", uniqueSorted("company"), "company");
  renderCheckboxes("executiveFilters", uniqueSorted("executive"), "executive");
  renderCheckboxes("titleFilters", uniqueSorted("title"), "title");
  renderCheckboxes("yearFilters", uniqueSorted("year"), "year");
}

function matchesFilters(rec) {
  const f = state.filters;
  if (f.company.size && !f.company.has(rec.company)) return false;
  if (f.executive.size && !f.executive.has(rec.executive)) return false;
  if (f.title.size && !f.title.has(rec.title)) return false;
  if (f.year.size && !f.year.has(rec.year)) return false;
  if (
    f.channelSearch &&
    !(rec.channel_name || "").toLowerCase().includes(f.channelSearch)
  )
    return false;
  if (
    f.titleSearch &&
    !(rec.item_title || "").toLowerCase().includes(f.titleSearch)
  )
    return false;
  if (f.minViews && (rec.view_count || 0) < f.minViews) return false;
  if (f.minLikes && (rec.like_count || 0) < f.minLikes) return false;
  if (f.minScore && (rec.combined_classifier_score || 0) < f.minScore) return false;
  return true;
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
      `<span class="li-date">${escapeHtml(date)} · ${escapeHtml(rec.company)}</span>` +
      `<span class="li-exec">${escapeHtml(rec.executive)}</span>` +
      `<span class="li-title">${escapeHtml(rec.item_title || "(untitled)")}</span>`;
    div.addEventListener("click", () => {
      state.pos = idx;
      showCurrent();
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
    `${rec.executive} — ${rec.company} (${rec.title || "—"})`;

  const itemTitleEl = document.getElementById("metaItemTitle");
  const title = escapeHtml(rec.item_title || "(untitled)");
  itemTitleEl.innerHTML = rec.source_url
    ? `<a href="${escapeHtml(rec.source_url)}" target="_blank" rel="noopener">${title}</a>`
    : title;

  const date = rec.publish_date ? rec.publish_date.slice(0, 10) : "unknown date";
  const parts = [
    date,
    rec.channel_name || "unknown channel",
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
    const res = await fetch(`/api/interview/${meta.id}`);
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
  return `[${rec.company} — ${rec.executive} (${rec.title || "—"}) — "${rec.item_title || "untitled"}" — ${date} — ${rec.channel_name || "unknown channel"} — feed_item_id:${rec.feed_item_id}]`;
}

function flashCopyStatus(msg) {
  const el = document.getElementById("copyStatus");
  el.textContent = msg;
  setTimeout(() => {
    if (el.textContent === msg) el.textContent = "";
  }, 2000);
}

function bindEvents() {
  document.getElementById("prevBtn").addEventListener("click", prev);
  document.getElementById("nextBtn").addEventListener("click", next);

  document.getElementById("toggleFilters").addEventListener("click", () => {
    document.getElementById("filterPanel").classList.toggle("hidden");
  });

  document.getElementById("channelSearch").addEventListener("input", (e) => {
    state.filters.channelSearch = e.target.value.toLowerCase();
    applyFilters();
  });
  document.getElementById("titleSearch").addEventListener("input", (e) => {
    state.filters.titleSearch = e.target.value.toLowerCase();
    applyFilters();
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

  document.getElementById("resetFilters").addEventListener("click", () => {
    state.filters = {
      company: new Set(),
      executive: new Set(),
      title: new Set(),
      year: new Set(),
      channelSearch: "",
      titleSearch: "",
      minViews: 0,
      minLikes: 0,
      minScore: 0,
    };
    document
      .querySelectorAll("#filterPanel input[type=checkbox]")
      .forEach((cb) => (cb.checked = false));
    document.querySelectorAll("#filterPanel input[type=text], #filterPanel input[type=number]")
      .forEach((inp) => (inp.value = ""));
    applyFilters();
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

async function init() {
  const res = await fetch("/api/index");
  state.all = await res.json();
  buildFilterOptions();
  bindEvents();
  applyFilters();
}

init();
