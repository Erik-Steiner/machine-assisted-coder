// Web Appendix tab: a chronological log of research actions -- theme
// create/edit/archive/restore/merge, classifier training runs (with their
// hyperparameters), Search & Export downloads, codebook exports, and Browse
// filter snapshots taken at a citation-copy or download moment (see
// app.js's postFilterSnapshot) -- assembled server-side by
// coding_store.get_appendix_feed() and downloadable as a self-contained
// HTML file for a manuscript's supplementary material (appendix_export.py).
//
// Loaded last, after review.js. Shares app.js's escapeHtml/switchTab; adds
// no new shared state of its own since this tab is read-mostly.

const ACTION_LABELS = {
  theme_create: "Theme created",
  theme_update: "Theme edited",
  theme_archive: "Theme archived",
  theme_restore: "Theme restored",
  theme_merge: "Themes merged",
  training_run: "Classifier training run",
  query_download: "Data downloaded",
  interview_import: "Transcript imported",
  reddit_import: "Reddit/Arctic Shift imported",
  duplicate_detection_run: "Duplicate detection run",
  duplicate_canonical_override: "Duplicate canonical override",
  duplicate_override: "Duplicate manually flagged/unflagged",
  duplicate_override_removed: "Duplicate override removed",
  codebook_export: "Codebook exported",
  filter_snapshot: "Browse filter snapshot",
};

function summarizeAppendixRow(actionType, details) {
  switch (actionType) {
    case "theme_create":
      return `Created theme "${details.name || "?"}"`;
    case "theme_update": {
      const fields = Object.keys(details.fields || {}).join(", ") || "no fields";
      return `Edited theme "${details.theme_id_name || details.theme_id || "?"}" (${fields})`;
    }
    case "theme_archive":
      return `Archived theme "${details.theme_id_name || details.theme_id || "?"}"`;
    case "theme_restore":
      return `Restored theme "${details.theme_id_name || details.theme_id || "?"}"`;
    case "theme_merge":
      return `Merged "${details.source_theme_id_name || details.source_theme_id || "?"}" into "${details.target_theme_id_name || details.target_theme_id || "?"}"`;
    case "training_run": {
      const trained = (details.trained || []).map((t) => t.name).join(", ") || "none";
      return `Trained model ${details.model_version || "?"} — themes: ${trained}`;
    }
    case "query_download":
      return `Downloaded "${details.label || "?"}" (${details.count ?? "?"} interviews)`;
    case "interview_import":
      return details.appended
        ? `Added ${details.count ?? "?"} record(s) from ${details.filename || "?"} to "${details.label || "?"}"`
        : `Imported "${details.label || "?"}" from ${details.filename || "?"} (${details.count ?? "?"} record(s))`;
    case "reddit_import":
      return `Imported r/${details.label || "?"} from ${details.filename || "?"} (${details.count ?? "?"} record(s))`;
    case "duplicate_detection_run":
      return `Scanned ${details.n_items_scanned ?? "?"} item(s), found ${details.n_clusters ?? "?"} duplicate cluster(s) ` +
        `(${details.needs_attention ?? "?"} needing attention)`;
    case "duplicate_canonical_override":
      return `Marked ${details.dataset_id || "?"}:${details.item_id || "?"} as the canonical copy in cluster ${details.cluster_id ?? "?"}`;
    case "duplicate_override":
      return details.action === "exclude"
        ? `Marked ${details.dataset_id || "?"}:${details.item_id || "?"} as NOT a duplicate`
        : `Marked ${details.dataset_id || "?"}:${details.item_id || "?"} as a duplicate of ${details.canonical_dataset_id || "?"}:${details.canonical_item_id || "?"}`;
    case "duplicate_override_removed":
      return `Removed manual duplicate override on ${details.dataset_id || "?"}:${details.item_id || "?"}`;
    case "codebook_export":
      return `Exported codebook (${(details.files || []).join(", ")})`;
    case "filter_snapshot":
      return "Browse filters active at export/citation time";
    default:
      return ACTION_LABELS[actionType] || actionType;
  }
}

function renderAppendixTable(rows) {
  const body = document.getElementById("appendixTableBody");
  const empty = document.getElementById("appendixEmpty");
  body.innerHTML = "";
  empty.classList.toggle("hidden", rows.length > 0);

  rows.forEach((row) => {
    const tr = document.createElement("tr");
    const label = ACTION_LABELS[row.action_type] || row.action_type;
    const summary = summarizeAppendixRow(row.action_type, row.details || {});
    tr.innerHTML =
      `<td class="appendix-ts">${escapeHtml(row.ts)}</td>` +
      `<td>${escapeHtml(row.actor)}</td>` +
      `<td><span class="appendix-badge">${escapeHtml(label)}</span></td>` +
      `<td>${escapeHtml(summary)}</td>`;
    body.appendChild(tr);
  });
}

async function loadAppendixLog() {
  const res = await fetch("/api/appendix/log");
  const rows = await res.json();
  renderAppendixTable(rows);
}

function bindAppendixEvents() {
  document.getElementById("appendixExportBtn").addEventListener("click", () => {
    window.location.href = "/api/appendix/export";
  });

  document.getElementById("appendixExportCodebookBtn").addEventListener("click", async () => {
    const statusEl = document.getElementById("appendixExportStatus");
    statusEl.textContent = "Exporting…";
    try {
      const res = await fetch("/api/export/codebook");
      const data = await res.json();
      statusEl.textContent = `Saved ${data.files.join(", ")} to ${data.exported_to}/`;
      loadAppendixLog(); // the export itself is now a logged action
    } catch (err) {
      statusEl.textContent = "Export failed — is the server running?";
    }
  });

  window.onEnterAppendixTab = () => loadAppendixLog();
}

function initAppendix() {
  bindAppendixEvents();
}

initAppendix();
