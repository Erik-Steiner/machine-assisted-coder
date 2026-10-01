// Model tab's Storage panel: see how much of coding.db each training run's
// per-segment scores take, pin runs to protect them, delete the scores of old
// runs, and compact the database so the freed space goes back to the disk.
//
// The rule for what may be deleted lives server-side only
// (coding_store/storage.py's plan_prune()); this file just shows its verdict
// per run and sends back the pairs the researcher confirmed. The server
// re-checks every pair, so a stale view can never delete a protected run.
//
// Shares the Model tab's #modelDetail area with model.js: opening this panel
// hides the theme detail, and model.js's selectModelTheme() hides this panel.
// Reuses escapeHtml/pollBackgroundJob (app.js) and modelState/
// renderModelThemeList/ENGINE_LABELS (model.js). Loaded after model.js.

const storageState = {
  report: null, // last /api/storage/report response
  selected: new Set(), // model_versions ticked for deletion
  pruneTimer: null,
  compactTimer: null,
};

function formatBytes(n) {
  if (!n) return "0 MB";
  if (n >= 1e9) return `${(n / 1e9).toFixed(2)} GB`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(0)} MB`;
  return `${(n / 1e3).toFixed(0)} KB`;
}

const STORAGE_STATUS_LABELS = {
  latest: "latest",
  recent: "recent",
  pinned: "pinned",
  prunable: "can delete",
  pruned: "scores deleted",
};

async function openStoragePanel() {
  modelState.selectedThemeId = null;
  renderModelThemeList();
  document.getElementById("modelDetailEmpty").classList.add("hidden");
  document.getElementById("modelDetailContent").classList.add("hidden");
  document.getElementById("modelStoragePanel").classList.remove("hidden");
  await loadStorageReport();
}

async function loadStorageReport() {
  const keep = Number(document.getElementById("storageKeepLatest").value) || 2;
  const summaryEl = document.getElementById("storageSummary");
  summaryEl.textContent = "Measuring… (this can take a few seconds on a large database)";
  let data;
  try {
    const res = await fetch(`/api/storage/report?keep_latest=${encodeURIComponent(keep)}`);
    data = await res.json();
  } catch (err) {
    summaryEl.textContent = "Request failed — is the server running?";
    return;
  }
  if (data.error) {
    summaryEl.textContent = "Error: " + data.error;
    return;
  }
  storageState.report = data;
  // Default selection = every run the rule says can be deleted. Nothing happens
  // until the researcher clicks Delete and confirms.
  storageState.selected = new Set(data.passes.filter((p) => p.prunable_rows > 0).map((p) => p.model_version));
  renderStorageReport();
}

function renderStorageReport() {
  const r = storageState.report;
  const walText = r.wal_bytes ? ` (+ ${formatBytes(r.wal_bytes)} write-ahead log)` : "";
  document.getElementById("storageSummary").innerHTML =
    `<div>Database file: <strong>${formatBytes(r.db_bytes)}</strong>${walText}</div>` +
    `<div>Model scores: <strong>${r.predictions_rows.toLocaleString()}</strong> rows, ` +
    `about ${formatBytes(r.predictions_rows * r.est_bytes_per_row)}</div>` +
    `<div>Free space inside the file: <strong>${formatBytes(r.free_bytes)}</strong> ` +
    `(reused by future runs; returned to the disk by compacting)</div>`;

  const tbody = document.getElementById("storageRunsTable").querySelector("tbody");
  if (!r.passes.length) {
    tbody.innerHTML = '<tr><td colspan="9" class="hint">No training runs yet.</td></tr>';
  } else {
    tbody.innerHTML = r.passes.map(renderStorageRow).join("");
  }
  renderStorageSelection();

  const emb = r.embedding_files;
  document.getElementById("storageEmbeddings").textContent = emb.length
    ? emb.map((f) => `${f.file}: ${formatBytes(f.bytes)}`).join(" · ") +
      " — can be deleted by hand from the project's embeddings/ folder and rebuilt (at CPU cost) by the next embedding run."
    : "No cached embeddings.";
}

function renderStorageRow(p) {
  const canPrune = p.prunable_rows > 0;
  const counts = {};
  p.themes.forEach((t) => { counts[t.status] = (counts[t.status] || 0) + 1; });
  const statusText = Object.entries(counts)
    .map(([status, n]) => `${n} ${STORAGE_STATUS_LABELS[status] || status}`)
    .join(" · ");
  const themeDetail = p.themes
    .map((t) => `${t.name}: ${STORAGE_STATUS_LABELS[t.status] || t.status}, ${t.rows.toLocaleString()} rows`)
    .join("\n");
  const trainedAt = p.trained_at ? p.trained_at.slice(0, 16).replace("T", " ") : "—";
  const pinTitle = p.pinned && p.pin_note ? ` title="${escapeHtml(p.pin_note)}"` : "";
  return (
    `<tr data-version="${escapeHtml(p.model_version)}"${p.pinned ? ' class="storage-pinned"' : ""}>` +
    `<td><input type="checkbox" class="storage-select" ${canPrune ? "" : "disabled"} ` +
    `${storageState.selected.has(p.model_version) ? "checked" : ""}></td>` +
    `<td>${escapeHtml(trainedAt)}</td>` +
    `<td>${escapeHtml(p.model_version)}</td>` +
    `<td>${escapeHtml(ENGINE_LABELS[p.engine] || p.engine || "?")}</td>` +
    `<td title="${escapeHtml(themeDetail)}">${escapeHtml(statusText)}</td>` +
    `<td>${p.rows.toLocaleString()}</td>` +
    `<td>${formatBytes(p.est_bytes)}</td>` +
    `<td>${canPrune ? formatBytes(p.prunable_est_bytes) : "—"}</td>` +
    `<td><button class="btn-secondary storage-pin-btn"${pinTitle}>${p.pinned ? "Unpin" : "Pin"}</button></td>` +
    `</tr>`
  );
}

function selectedPrunePairs() {
  const pairs = [];
  let rows = 0;
  let bytes = 0;
  storageState.report.passes.forEach((p) => {
    if (!storageState.selected.has(p.model_version)) return;
    p.themes.forEach((t) => {
      if (t.status !== "prunable") return;
      pairs.push({ model_version: p.model_version, theme_id: t.theme_id });
      rows += t.rows;
      bytes += t.est_bytes;
    });
  });
  return { pairs, rows, bytes };
}

function renderStorageSelection() {
  const { pairs, rows, bytes } = selectedPrunePairs();
  document.getElementById("storagePruneBtn").disabled = !pairs.length;
  document.getElementById("storageSelectionText").textContent = pairs.length
    ? `${storageState.selected.size} run(s) selected: ${rows.toLocaleString()} rows, about ${formatBytes(bytes)}`
    : "Nothing selected.";
}

async function togglePin(modelVersion, currentlyPinned) {
  let note = null;
  if (!currentlyPinned) {
    note = prompt(`Pin ${modelVersion}? Optionally say why (e.g. "cited in paper", "exported for comparison"):`, "");
    if (note === null) return; // cancelled
  }
  const res = await fetch("/api/storage/pin", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model_version: modelVersion, pinned: !currentlyPinned, note: note || null }),
  });
  const data = await res.json();
  if (data.error) {
    document.getElementById("storagePruneStatus").textContent = "Error: " + data.error;
    return;
  }
  await loadStorageReport();
}

async function startPrune() {
  const { pairs, rows, bytes } = selectedPrunePairs();
  if (!pairs.length) return;
  const ok = confirm(
    `Delete the per-segment scores of ${storageState.selected.size} run(s) ` +
    `(${rows.toLocaleString()} rows, about ${formatBytes(bytes)})?\n\n` +
    "Each run's metrics, top terms, and settings are kept, and so are all your codes. " +
    "The deleted scores can't be recreated exactly. This can't be undone."
  );
  if (!ok) return;
  const statusEl = document.getElementById("storagePruneStatus");
  const btn = document.getElementById("storagePruneBtn");
  btn.disabled = true;
  statusEl.textContent = "Starting…";
  const keep = Number(document.getElementById("storageKeepLatest").value) || 2;
  let data;
  try {
    const res = await fetch("/api/storage/prune", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pairs, keep_latest: keep }),
    });
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
  storageState.pruneTimer?.cancel();
  storageState.pruneTimer = pollBackgroundJob(
    `/api/storage/prune_status?job_id=${encodeURIComponent(data.job_id)}`,
    {
      onRunning: (job) => {
        statusEl.textContent = `Deleting… ${job.pairs_done}/${job.pairs_total}`;
      },
      onDone: (job) => {
        statusEl.textContent =
          `Deleted ${job.rows_deleted.toLocaleString()} rows; ${formatBytes(job.freed_bytes)} is now free inside ` +
          "the database file. Compact the database (below) to give it back to the disk.";
        loadStorageReport();
      },
      onError: (job) => {
        statusEl.textContent = "Error: " + job.error;
        loadStorageReport();
      },
    },
  );
}

async function startCompact() {
  const statusEl = document.getElementById("storageCompactStatus");
  const btn = document.getElementById("storageCompactBtn");
  let check;
  try {
    check = await (await fetch("/api/storage/compact_check")).json();
  } catch (err) {
    statusEl.textContent = "Request failed — is the server running?";
    return;
  }
  if (!check.ok) {
    statusEl.textContent =
      `Not enough free disk space to compact safely: needs about ${formatBytes(check.required_bytes)}, ` +
      `${formatBytes(check.available_bytes)} available.`;
    return;
  }
  const ok = confirm(
    `Compact the database now? About ${formatBytes(check.reclaimable_bytes)} can be returned to the disk.\n\n` +
    "This can take several minutes. Coding and training are blocked until it finishes, " +
    "and a synced project folder will re-upload the whole database afterwards."
  );
  if (!ok) return;
  btn.disabled = true;
  statusEl.textContent = "Starting…";
  const data = await (await fetch("/api/storage/compact", { method: "POST" })).json();
  if (data.error) {
    statusEl.textContent = "Error: " + data.error;
    btn.disabled = false;
    return;
  }
  const startedAt = Date.now();
  storageState.compactTimer?.cancel();
  storageState.compactTimer = pollBackgroundJob(
    `/api/storage/compact_status?job_id=${encodeURIComponent(data.job_id)}`,
    {
      onRunning: () => {
        statusEl.textContent = `Compacting… (${Math.round((Date.now() - startedAt) / 1000)}s)`;
      },
      onDone: (job) => {
        btn.disabled = false;
        statusEl.textContent =
          `Done: reclaimed ${formatBytes(job.reclaimed_bytes)}; the database is now ${formatBytes(job.db_bytes_after)}.`;
        loadStorageReport();
      },
      onError: (job) => {
        btn.disabled = false;
        statusEl.textContent = "Error: " + job.error;
      },
    },
    2000,
  );
}

function bindStorageEvents() {
  document.getElementById("modelStorageBtn").addEventListener("click", openStoragePanel);
  document.getElementById("storageRefreshBtn").addEventListener("click", loadStorageReport);
  document.getElementById("storageKeepLatest").addEventListener("change", loadStorageReport);
  document.getElementById("storagePruneBtn").addEventListener("click", startPrune);
  document.getElementById("storageCompactBtn").addEventListener("click", startCompact);
  document.getElementById("storageRunsTable").addEventListener("change", (e) => {
    if (!e.target.classList.contains("storage-select")) return;
    const version = e.target.closest("tr").dataset.version;
    if (e.target.checked) storageState.selected.add(version);
    else storageState.selected.delete(version);
    renderStorageSelection();
  });
  document.getElementById("storageRunsTable").addEventListener("click", (e) => {
    if (!e.target.classList.contains("storage-pin-btn")) return;
    const version = e.target.closest("tr").dataset.version;
    const pass = storageState.report.passes.find((p) => p.model_version === version);
    if (pass) togglePin(version, pass.pinned);
  });
}

bindStorageEvents();
