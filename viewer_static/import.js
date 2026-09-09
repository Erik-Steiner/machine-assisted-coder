// Search & Export tab's "Import your own data" section: upload an interview
// transcript (.docx/.txt/.json) or a Reddit/Arctic Shift export (submissions
// +comments JSONL) with no command line needed, as a browser-based
// alternative to scripts/import_interview_transcript.py and
// scripts/import_reddit.py (same server-side logic either way -- see
// viewer_server.py's /api/import/transcript/* and /api/import/reddit/*).
//
// Loaded after app.js; shares its escapeHtml/refreshDatasetList/loadDataset/
// switchTab/postFilterSnapshot. Both importers are two-step (parse then
// commit): parse() reads the file(s) into server-side memory and returns a
// preview -- for a transcript, the speaker labels found, so the researcher
// can assign interviewer/respondent/other roles here in the browser (the
// same decision the CLI makes via interactive prompts); for Reddit, just
// item/turn counts, since there's no per-source role decision to make.
// Nothing is written to queries/ until commit().

const importState = {
  token: null,
  kind: null,
  filename: null,
};

function arrayBufferToBase64(buf) {
  let binary = "";
  const bytes = new Uint8Array(buf);
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunkSize));
  }
  return btoa(binary);
}

// Posts a File directly as the request body (POST /api/import/raw_upload?filename=...)
// so the browser streams it to the network instead of materializing it as a JS string --
// unlike arrayBufferToBase64() above, which is fine for one interview transcript but
// blows past a tab's memory budget for a multi-hundred-MB Reddit/Arctic Shift export
// (base64 string + JSON.stringify copy, on top of the original bytes). Used by the
// Reddit import flow below; returns the upload_id /api/import/reddit/parse expects.
async function uploadFileRaw(file) {
  const res = await fetch(`/api/import/raw_upload?filename=${encodeURIComponent(file.name)}`, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: file,
  });
  const data = await res.json();
  if (data.error) throw new Error(data.error);
  return data.upload_id;
}

function resetImportUI() {
  importState.token = null;
  importState.kind = null;
  importState.filename = null;
  document.getElementById("importRolesForm").classList.add("hidden");
  document.getElementById("importJsonForm").classList.add("hidden");
  document.getElementById("importResult").classList.add("hidden");
  document.getElementById("importLabelRoles").innerHTML = "";
  document.querySelectorAll("#importMetaFields input").forEach((inp) => (inp.value = ""));
  document.getElementById("importTargetDataset").value = "";
}

// Populates an "Add to" dropdown with existing datasets of one kind (see
// query_api.append_to_dataset()) so a researcher can file a new upload into
// an existing bucket (e.g. every transcript from one research project, or
// every submission from one subreddit-tracking project) instead of every
// import creating its own one-item dataset. Shared by both import panels --
// selectId/kind differ (importTargetDataset/"interview_import" for
// transcripts, importRedditTargetDataset/"reddit_import" for Reddit) but the
// logic is identical. Live-download/company datasets are never offered --
// appending into those would conflate a hand-uploaded/imported batch with a
// live query result.
async function populateImportTargetOptions(selectId, kind) {
  const select = document.getElementById(selectId);
  select.innerHTML = '<option value="">a new dataset</option>';
  let datasets;
  try {
    const res = await fetch("/api/datasets");
    datasets = await res.json();
  } catch (err) {
    return; // not critical -- "a new dataset" still works
  }
  (datasets || [])
    .filter((d) => d.kind === kind)
    .forEach((d) => {
      const opt = document.createElement("option");
      opt.value = d.id;
      opt.textContent = `${d.label} (${d.count})`;
      select.appendChild(opt);
    });
}

function roleSelectHtml(selected) {
  const opts = [
    ["other", "Other / exclude"],
    ["interviewer", "Interviewer"],
    ["subject", "Respondent"],
  ];
  return (
    '<select class="import-role-select">' +
    opts
      .map(([val, label]) => `<option value="${val}"${val === selected ? " selected" : ""}>${label}</option>`)
      .join("") +
    "</select>"
  );
}

function renderImportRolesForm(data) {
  const container = document.getElementById("importLabelRoles");
  container.innerHTML = "";
  data.labels.forEach((label) => {
    const row = document.createElement("div");
    row.className = "import-role-row";
    row.dataset.label = label;
    row.innerHTML = `<span>${escapeHtml(label)}</span>` + roleSelectHtml("other");
    container.appendChild(row);
  });
  if (data.unlabeled_count) {
    const row = document.createElement("div");
    row.className = "import-role-row";
    row.dataset.unlabeled = "1";
    row.innerHTML = `<span>Unlabeled turns (${data.unlabeled_count})</span>` + roleSelectHtml("other");
    container.appendChild(row);
  }
  document.getElementById("importItemTitle").placeholder = data.suggested_item_title || "optional";
}

async function handleImportFile(file) {
  resetImportUI();
  const statusEl = document.getElementById("importStatus");
  statusEl.textContent = "Parsing…";

  let content_base64;
  try {
    content_base64 = arrayBufferToBase64(await file.arrayBuffer());
  } catch (err) {
    statusEl.textContent = "Couldn't read that file in the browser.";
    return;
  }

  let data;
  try {
    const res = await fetch("/api/import/transcript/parse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: file.name, content_base64 }),
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

  importState.token = data.upload_token;
  importState.kind = data.kind;
  importState.filename = file.name;

  if (data.kind === "json") {
    statusEl.textContent = "";
    document.getElementById("importJsonSummary").textContent =
      `${data.n_records} record(s) parsed from "${data.label || file.name}". Ready to import.`;
    document.getElementById("importJsonForm").classList.remove("hidden");
  } else {
    // format_detected: "timestamped" (Word Transcribe-style) or "labeled" ("Speaker: text",
    // hand-typed or LLM-formatted, no timestamp) -- see parse_transcript_turns() in
    // scripts/import_interview_transcript.py. Surfaced here so the researcher can confirm
    // detection worked as expected before assigning roles.
    statusEl.textContent =
      data.format_detected === "labeled" ? "Detected format: speaker-labeled transcript (no timestamps)." : "";
    renderImportRolesForm(data);
    populateImportTargetOptions("importTargetDataset", "interview_import");
    document.getElementById("importRolesForm").classList.remove("hidden");
  }
}

async function goToDataset(datasetId, tab) {
  await refreshDatasetList(datasetId);
  await loadDataset(datasetId);
  switchTab(tab);
}

function showImportResult(data) {
  document.getElementById("importRolesForm").classList.add("hidden");
  document.getElementById("importJsonForm").classList.add("hidden");
  document.getElementById("importStatus").textContent = "";
  document.getElementById("importResult").classList.remove("hidden");
  const verb = data.appended
    ? `Added <strong>${data.count}</strong> interview(s) to <strong>${escapeHtml(data.dataset_label)}</strong>`
    : `Saved <strong>${escapeHtml(data.dataset_label)}</strong>`;
  document.getElementById("importResultText").innerHTML =
    `${verb} in <code>queries/${escapeHtml(data.json_file)}</code> and <code>queries/${escapeHtml(data.csv_file)}</code>. ` +
    `${data.segments_added} segment(s) are already in coding.db -- ready to code, no extra step needed.`;
  document.getElementById("importViewDatasetBtn").onclick = () => goToDataset(data.dataset_id, "browse");
  document.getElementById("importStartCodingBtn").onclick = () => goToDataset(data.dataset_id, "coding");
  postFilterSnapshot();
  document.getElementById("importFileInput").value = "";
}

async function commitImport(extraBody) {
  const statusEl = document.getElementById("importStatus");
  const btns = [document.getElementById("importCommitBtn"), document.getElementById("importJsonCommitBtn")];
  btns.forEach((b) => (b.disabled = true));
  statusEl.textContent = "Importing…";

  let data;
  try {
    const res = await fetch("/api/import/transcript/commit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ upload_token: importState.token, ...extraBody }),
    });
    data = await res.json();
  } catch (err) {
    statusEl.textContent = "Request failed — is the server running?";
    btns.forEach((b) => (b.disabled = false));
    return;
  }
  btns.forEach((b) => (b.disabled = false));

  if (data.error) {
    statusEl.textContent = "Error: " + data.error;
    return;
  }
  showImportResult(data);
}

function commitTranscriptImport() {
  const personName = document.getElementById("importPersonName").value.trim();
  if (!personName) {
    document.getElementById("importStatus").textContent = "Subject's name is required.";
    return;
  }
  const label_roles = {};
  let unlabeled_role = "other";
  document.querySelectorAll("#importLabelRoles .import-role-row").forEach((row) => {
    const val = row.querySelector(".import-role-select").value;
    if (row.dataset.unlabeled) unlabeled_role = val;
    else label_roles[row.dataset.label] = val;
  });

  commitImport({
    label_roles,
    unlabeled_role,
    group_name: document.getElementById("importGroupName").value.trim() || null,
    person_name: personName,
    person_title: document.getElementById("importPersonTitle").value.trim() || null,
    item_title: document.getElementById("importItemTitle").value.trim() || null,
    publish_date: document.getElementById("importPublishDate").value || null,
    target_dataset_id: document.getElementById("importTargetDataset").value || null,
  });
}

function commitJsonImport() {
  commitImport({});
}

function bindImportEvents() {
  document.getElementById("importFileInput").addEventListener("change", (e) => {
    const file = e.target.files[0];
    if (file) handleImportFile(file);
  });
  document.getElementById("importCommitBtn").addEventListener("click", commitTranscriptImport);
  document.getElementById("importJsonCommitBtn").addEventListener("click", commitJsonImport);
}

// --- Reddit / Arctic Shift import ---------------------------------------------------
// Simpler two-file flow: no role-assignment step, since Reddit data has no
// interviewer/subject structure to decide (see scripts/import_reddit.py).
// Still parse-then-commit, so the researcher sees item/turn counts (and any
// orphaned-comment warning) before anything is written.

const redditImportState = { token: null };

function resetRedditImportUI() {
  redditImportState.token = null;
  document.getElementById("importRedditPreview").classList.add("hidden");
  document.getElementById("importRedditResult").classList.add("hidden");
  document.getElementById("importRedditStatus").textContent = "";
  document.getElementById("importRedditTargetDataset").value = "";
}

async function parseRedditImport() {
  const subFile = document.getElementById("importRedditSubmissions").files[0];
  const comFile = document.getElementById("importRedditComments").files[0];
  const statusEl = document.getElementById("importRedditStatus");
  resetRedditImportUI();

  if (!subFile) {
    statusEl.textContent = "Choose a submissions .jsonl file first.";
    return;
  }

  statusEl.textContent = "Uploading…";
  let body;
  try {
    body = {
      submissions_filename: subFile.name,
      submissions_upload_id: await uploadFileRaw(subFile),
    };
    if (comFile) {
      body.comments_filename = comFile.name;
      body.comments_upload_id = await uploadFileRaw(comFile);
    }
  } catch (err) {
    statusEl.textContent = "Couldn't upload that file: " + err.message;
    return;
  }

  statusEl.textContent = "Parsing…";
  let data;
  try {
    const res = await fetch("/api/import/reddit/parse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
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

  redditImportState.token = data.upload_token;
  statusEl.textContent = "";

  const subredditsText =
    data.subreddits.length === 1 ? `r/${data.subreddits[0]}` : `${data.subreddits.length} subreddits`;
  let summary = `${data.n_records} item(s), ${data.n_turns} turn(s) total, from ${subredditsText}. Ready to import.`;
  if (data.orphaned_comments) {
    summary += ` (${data.orphaned_comments} comment(s) skipped — no matching submission.)`;
  }
  document.getElementById("importRedditSummary").textContent = summary;
  populateImportTargetOptions("importRedditTargetDataset", "reddit_import");
  document.getElementById("importRedditPreview").classList.remove("hidden");
}

async function commitRedditImport() {
  const btn = document.getElementById("importRedditCommitBtn");
  const statusEl = document.getElementById("importRedditStatus");
  btn.disabled = true;
  statusEl.textContent = "Importing…";

  let data;
  try {
    const res = await fetch("/api/import/reddit/commit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        upload_token: redditImportState.token,
        target_dataset_id: document.getElementById("importRedditTargetDataset").value || null,
      }),
    });
    data = await res.json();
  } catch (err) {
    statusEl.textContent = "Request failed — is the server running?";
    btn.disabled = false;
    return;
  }
  btn.disabled = false;

  if (data.error) {
    statusEl.textContent = "Error: " + data.error;
    return;
  }

  document.getElementById("importRedditPreview").classList.add("hidden");
  statusEl.textContent = "";
  document.getElementById("importRedditResult").classList.remove("hidden");
  const verb = data.appended
    ? `Added <strong>${data.count}</strong> submission(s) to <strong>${escapeHtml(data.dataset_label)}</strong>`
    : `Saved <strong>${escapeHtml(data.dataset_label)}</strong>`;
  document.getElementById("importRedditResultText").innerHTML =
    `${verb} in <code>queries/${escapeHtml(data.json_file)}</code> and <code>queries/${escapeHtml(data.csv_file)}</code>. ` +
    `${data.segments_added} segment(s) are already in coding.db -- ready to code, no extra step needed.`;
  document.getElementById("importRedditViewDatasetBtn").onclick = () => goToDataset(data.dataset_id, "browse");
  document.getElementById("importRedditStartCodingBtn").onclick = () => goToDataset(data.dataset_id, "coding");
  postFilterSnapshot();
  document.getElementById("importRedditSubmissions").value = "";
  document.getElementById("importRedditComments").value = "";
}

function bindRedditImportEvents() {
  document.getElementById("importRedditParseBtn").addEventListener("click", parseRedditImport);
  document.getElementById("importRedditCommitBtn").addEventListener("click", commitRedditImport);
}

function initImport() {
  bindImportEvents();
  bindRedditImportEvents();
}

initImport();
