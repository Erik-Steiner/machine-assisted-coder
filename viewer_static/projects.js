// Project picker: switch which project's queries/coding.db is active, live,
// without restarting the server -- see viewer_server.py's switch_project()
// and project_registry.py. Header-level, like app.js, not tab-specific --
// loaded right after it, before any tab-specific file.
//
// On a successful switch the page just reloads (window.location.reload()) --
// simplest way to reset every tab's in-memory state cleanly, rather than
// hand-writing a reset path for state/codingState/modelState/reviewState.

const projectState = {
  projects: [],
  activePath: null,
};

async function loadProjects() {
  const res = await fetch("/api/projects");
  const data = await res.json();
  projectState.projects = data.projects || [];
  projectState.activePath = data.active_path;
  renderProjectButton();
  renderProjectList();
}

function renderProjectButton() {
  const active = projectState.projects.find((p) => p.path === projectState.activePath);
  document.getElementById("projectBtnName").textContent = active ? active.name : "(unknown)";
}

function renderProjectList() {
  const container = document.getElementById("projectList");
  container.innerHTML = "";
  if (!projectState.projects.length) {
    container.innerHTML = '<div class="hint">No projects yet.</div>';
    return;
  }
  projectState.projects.forEach((p) => {
    const isActive = p.path === projectState.activePath;
    const row = document.createElement("div");
    row.className = "project-row" + (isActive ? " active" : "");
    row.innerHTML =
      `<span class="project-row-name">${escapeHtml(p.name)}</span>` +
      `<span class="project-row-path">${escapeHtml(p.path)}</span>` +
      (isActive
        ? '<span class="project-row-badge">current</span>'
        : `<button type="button" class="btn-secondary project-open-btn" data-path="${escapeHtml(p.path)}">Open</button>` +
          `<button type="button" class="btn-secondary project-remove-btn" data-path="${escapeHtml(p.path)}">Remove</button>`);
    container.appendChild(row);
  });
}

async function openProject(path, name) {
  const statusEl = document.getElementById("projectFormStatus");
  statusEl.textContent = "Opening…";
  try {
    const res = await fetch("/api/projects/open", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, name: name || "" }),
    });
    const data = await res.json();
    if (data.error) {
      statusEl.textContent = "Error: " + data.error;
      return;
    }
    window.location.reload();
  } catch (err) {
    statusEl.textContent = "Request failed — is the server running?";
  }
}

async function removeProject(path) {
  if (!confirm("Remove this project from the list? Its data is not touched.")) return;
  const res = await fetch("/api/projects/remove", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  const data = await res.json();
  if (data.error) {
    alert(data.error);
    return;
  }
  await loadProjects();
}

async function browseForFolder() {
  const input = document.getElementById("projectPathInput");
  try {
    const res = await fetch("/api/projects/browse_folder", { method: "POST" });
    const data = await res.json();
    if (data.path) input.value = data.path;
    // No native dialog available (headless env, etc.) -- silently fall back
    // to the text field already being there, per the design.
  } catch (err) {
    // same fallback
  }
}

function bindProjectEvents() {
  document.getElementById("projectBtn").addEventListener("click", async () => {
    await loadProjects();
    document.getElementById("projectPanel").classList.toggle("hidden");
  });
  document.getElementById("projectPanelCloseBtn").addEventListener("click", () => {
    document.getElementById("projectPanel").classList.add("hidden");
  });
  document.getElementById("projectBrowseBtn").addEventListener("click", browseForFolder);
  document.getElementById("projectOpenBtn").addEventListener("click", () => {
    const path = document.getElementById("projectPathInput").value.trim();
    const name = document.getElementById("projectNameInput").value.trim();
    if (!path) {
      document.getElementById("projectFormStatus").textContent = "Enter or browse for a folder path first.";
      return;
    }
    openProject(path, name);
  });
  document.getElementById("projectList").addEventListener("click", (e) => {
    if (e.target.classList.contains("project-open-btn")) {
      openProject(e.target.dataset.path);
    } else if (e.target.classList.contains("project-remove-btn")) {
      removeProject(e.target.dataset.path);
    }
  });
}

function initProjects() {
  bindProjectEvents();
  loadProjects();
}

initProjects();
