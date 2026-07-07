"use strict";

const app = {
  defaults: {},
  repoRoot: "",
  state: null,
  tasks: [],
  selectedTask: null,
  selectedDetail: null,
};

const el = {};

function $(id) {
  return document.getElementById(id);
}

function bindElements() {
  [
    "repoRoot",
    "refreshBtn",
    "buildBtn",
    "outInput",
    "modelOutputInput",
    "sourceDirInput",
    "sourceJsonlInput",
    "sourceLimitInput",
    "maxCharsInput",
    "runTagInput",
    "totalCount",
    "doneCount",
    "interfaceCount",
    "sourceCount",
    "budgetCount",
    "checkpointText",
    "taskSearch",
    "typeFilter",
    "statusFilter",
    "taskList",
    "taskTitle",
    "taskMeta",
    "copyResumeBtn",
    "copyTaskBtn",
    "copyCombinedBtn",
    "promptPreview",
    "outputPath",
    "copyOutputPathBtn",
    "saveOutputBtn",
    "outputEditor",
    "promptPath",
    "expectedPath",
    "checkpointPath",
    "indexPath",
    "message",
  ].forEach((id) => {
    el[id] = $(id);
  });
}

async function apiJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await response.json();
  if (!response.ok || data.ok === false) {
    const detail = data.detail ? `\n${data.detail}` : "";
    throw new Error(`${data.error || response.statusText}${detail}`);
  }
  return data;
}

function setMessage(text, mode = "") {
  el.message.textContent = text;
  const band = el.message.closest(".log-band");
  band.classList.toggle("ok", mode === "ok");
  band.classList.toggle("error", mode === "error");
}

function collectConfig() {
  return {
    out: el.outInput.value.trim(),
    model_output_root: el.modelOutputInput.value.trim(),
    source_task_dir: el.sourceDirInput.value.trim(),
    source_task_jsonl: el.sourceJsonlInput.value.trim(),
    source_task_limit: Number.parseInt(el.sourceLimitInput.value || "0", 10),
    max_prompt_chars: Number.parseInt(el.maxCharsInput.value || "60000", 10),
    run_tag: el.runTagInput.value.trim(),
  };
}

function applyDefaults(defaults) {
  el.outInput.value = defaults.out || "";
  el.modelOutputInput.value = defaults.model_output_root || "";
  el.sourceDirInput.value = defaults.source_task_dir || "";
  el.sourceJsonlInput.value = defaults.source_task_jsonl || "";
  el.sourceLimitInput.value = String(defaults.source_task_limit ?? 0);
  el.maxCharsInput.value = String(defaults.max_prompt_chars ?? 60000);
  el.runTagInput.value = defaults.run_tag || "";
}

async function loadDefaults() {
  const data = await apiJson("/api/defaults");
  app.defaults = data.defaults;
  app.repoRoot = data.repo_root;
  el.repoRoot.textContent = data.repo_root;
  applyDefaults(data.defaults);
}

async function refreshState(selectFirst = false) {
  const out = encodeURIComponent(el.outInput.value.trim() || app.defaults.out);
  const data = await apiJson(`/api/state?out=${out}`);
  app.state = data.state;
  app.tasks = app.state.tasks || [];
  renderSummary();
  renderTasks();
  renderPaths();
  if (selectFirst && app.tasks.length) {
    await selectTask(app.tasks[0].task_id);
  } else if (app.selectedTask) {
    const stillExists = app.tasks.some((task) => task.task_id === app.selectedTask.task_id);
    if (stillExists) {
      await selectTask(app.selectedTask.task_id);
    }
  }
}

async function buildPack() {
  setBusy(true);
  setMessage("Generating prompt pack...");
  try {
    const data = await apiJson("/api/build-pack", {
      method: "POST",
      body: JSON.stringify(collectConfig()),
    });
    app.state = data.state;
    app.tasks = app.state.tasks || [];
    renderSummary();
    renderTasks();
    renderPaths();
    setMessage(`Generated pack: ${app.tasks.length} tasks.`, "ok");
    if (app.tasks.length) {
      await selectTask(app.tasks[0].task_id);
    }
  } catch (error) {
    setMessage(error.message, "error");
  } finally {
    setBusy(false);
  }
}

function setBusy(value) {
  el.buildBtn.disabled = value;
  el.refreshBtn.disabled = value;
}

function renderSummary() {
  const summary = app.state?.summary || {};
  el.totalCount.textContent = String(summary.total || 0);
  el.doneCount.textContent = String(summary.done || 0);
  el.interfaceCount.textContent = String(summary.interface || 0);
  el.sourceCount.textContent = String(summary.source || 0);
  el.budgetCount.textContent = String(summary.over_budget || 0);

  if (!app.state?.exists) {
    el.checkpointText.textContent = "No checkpoint. Generate a prompt pack to start.";
    return;
  }
  el.checkpointText.textContent = `${app.state.checkpoint_path} | generated ${app.state.generated_at || "-"}`;
}

function renderPaths() {
  el.checkpointPath.textContent = app.state?.checkpoint_path || "-";
  el.indexPath.textContent = app.state?.index_path || "-";
}

function filteredTasks() {
  const query = el.taskSearch.value.trim().toLowerCase();
  const type = el.typeFilter.value;
  const status = el.statusFilter.value;
  return app.tasks.filter((task) => {
    if (type !== "all" && task.task_type !== type) {
      return false;
    }
    if (status === "todo" && task.output_exists) {
      return false;
    }
    if (status === "done" && !task.output_exists) {
      return false;
    }
    if (status === "over" && !task.over_safe_budget) {
      return false;
    }
    if (!query) {
      return true;
    }
    const haystack = [
      task.task_id,
      task.task_type,
      task.target_api,
      task.geometry_family,
      task.risk_family,
      task.prompt_path,
      task.expected_output_path,
    ]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return haystack.includes(query);
  });
}

function renderTasks() {
  const tasks = filteredTasks();
  el.taskList.replaceChildren();
  if (!tasks.length) {
    const empty = document.createElement("div");
    empty.className = "task-row";
    empty.textContent = app.tasks.length ? "No tasks match filters." : "No tasks loaded.";
    el.taskList.append(empty);
    return;
  }

  tasks.forEach((task) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "task-row";
    row.setAttribute("role", "option");
    row.classList.toggle("active", app.selectedTask?.task_id === task.task_id);
    row.addEventListener("click", () => selectTask(task.task_id));

    const main = document.createElement("div");
    main.className = "task-main";

    const id = document.createElement("div");
    id.className = "task-id";
    id.textContent = task.task_id || "(unknown task)";

    const sub = document.createElement("div");
    sub.className = "task-sub";
    const type = task.task_type === "source_attack" ? "source" : "interface";
    sub.textContent = `${type} | ~${task.estimated_tokens || 0} tokens | ${task.chars || 0} chars`;

    main.append(id, sub);

    const badge = document.createElement("span");
    badge.className = "badge";
    if (task.over_safe_budget) {
      badge.classList.add("over");
      badge.textContent = "over";
    } else if (task.output_exists) {
      badge.classList.add("done");
      badge.textContent = "done";
    } else {
      badge.classList.add("todo");
      badge.textContent = "todo";
    }

    row.append(main, badge);
    el.taskList.append(row);
  });
}

async function selectTask(taskId) {
  if (!taskId) {
    return;
  }
  setMessage(`Loading task ${taskId}...`);
  try {
    const out = encodeURIComponent(el.outInput.value.trim() || app.defaults.out);
    const data = await apiJson(`/api/task?out=${out}&task_id=${encodeURIComponent(taskId)}`);
    app.selectedTask = data.task;
    app.selectedDetail = data;
    renderSelectedTask();
    renderTasks();
    setMessage(`Loaded ${taskId}.`, "ok");
  } catch (error) {
    setMessage(error.message, "error");
  }
}

function renderSelectedTask() {
  const task = app.selectedTask;
  const detail = app.selectedDetail;
  if (!task || !detail) {
    return;
  }
  el.taskTitle.textContent = task.task_id || "Selected task";
  const type = task.task_type === "source_attack" ? "source attack" : "interface form";
  const status = task.output_exists ? "saved" : "todo";
  el.taskMeta.textContent = `${type} | ${status} | ~${task.estimated_tokens || 0} tokens | ${task.chars || 0} chars`;
  el.promptPreview.value = detail.task_prompt || "";
  el.outputEditor.value = detail.output_text || "";
  el.outputPath.textContent = task.expected_output_path || "";
  el.promptPath.textContent = task.prompt_path || "-";
  el.expectedPath.textContent = task.expected_output_path || "-";
  el.copyResumeBtn.disabled = !detail.resume_prompt;
  el.copyTaskBtn.disabled = !detail.task_prompt;
  el.copyCombinedBtn.disabled = !(detail.resume_prompt && detail.task_prompt);
  el.copyOutputPathBtn.disabled = !task.expected_output_path;
  el.saveOutputBtn.disabled = !task.expected_output_path;
}

async function copyText(text, label) {
  if (!text) {
    return;
  }
  try {
    await navigator.clipboard.writeText(text);
    setMessage(`Copied ${label}.`, "ok");
  } catch (_error) {
    const scratch = document.createElement("textarea");
    scratch.value = text;
    scratch.style.position = "fixed";
    scratch.style.left = "-10000px";
    document.body.append(scratch);
    scratch.focus();
    scratch.select();
    document.execCommand("copy");
    scratch.remove();
    setMessage(`Copied ${label}.`, "ok");
  }
}

async function saveOutput() {
  if (!app.selectedTask) {
    return;
  }
  const content = el.outputEditor.value.trim();
  try {
    const parsed = JSON.parse(content);
    if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
      throw new Error("Qoder output must be one JSON object.");
    }
  } catch (error) {
    setMessage(`Invalid JSON: ${error.message}`, "error");
    return;
  }

  try {
    const data = await apiJson("/api/save-output", {
      method: "POST",
      body: JSON.stringify({
        expected_output_path: app.selectedTask.expected_output_path,
        content,
      }),
    });
    setMessage(`Saved ${data.saved_path}.`, "ok");
    await refreshState(false);
  } catch (error) {
    setMessage(error.message, "error");
  }
}

function setActiveTab(name) {
  document.querySelectorAll(".tab").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === name);
  });
  document.querySelectorAll(".tab-panel").forEach((panel) => {
    panel.classList.toggle("active", panel.id === `${name}Tab`);
  });
}

function bindActions() {
  el.buildBtn.addEventListener("click", buildPack);
  el.refreshBtn.addEventListener("click", async () => {
    try {
      await refreshState(false);
      setMessage("State refreshed.", "ok");
    } catch (error) {
      setMessage(error.message, "error");
    }
  });
  [el.taskSearch, el.typeFilter, el.statusFilter].forEach((node) => {
    node.addEventListener("input", renderTasks);
    node.addEventListener("change", renderTasks);
  });
  el.copyResumeBtn.addEventListener("click", () => copyText(app.selectedDetail?.resume_prompt || "", "resume prompt"));
  el.copyTaskBtn.addEventListener("click", () => copyText(app.selectedDetail?.task_prompt || "", "task prompt"));
  el.copyCombinedBtn.addEventListener("click", () => {
    const combined = `${app.selectedDetail?.resume_prompt || ""}\n\n${app.selectedDetail?.task_prompt || ""}`;
    copyText(combined.trim(), "resume and task prompt");
  });
  el.copyOutputPathBtn.addEventListener("click", () => copyText(app.selectedTask?.expected_output_path || "", "output path"));
  el.saveOutputBtn.addEventListener("click", saveOutput);
  document.querySelectorAll(".tab").forEach((button) => {
    button.addEventListener("click", () => setActiveTab(button.dataset.tab));
  });
}

async function init() {
  bindElements();
  bindActions();
  try {
    await loadDefaults();
    await refreshState(true);
    setMessage("Ready.", "ok");
  } catch (error) {
    setMessage(error.message, "error");
  }
}

document.addEventListener("DOMContentLoaded", init);
