"use strict";

let state = null;
let csrf = "";
let selectedArtifact = "";
let settingsInitialized = false;
let lastJobError = "";
let abcInspection = null;
let toastTimer = null;
const $ = (id) => document.getElementById(id);

function toast(message, error = false) {
  const element = $("toast");
  element.textContent = message;
  element.className = "toast show" + (error ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { element.className = "toast"; }, 4200);
}

async function request(path, options = {}) {
  const response = await fetch(path, options);
  const value = await response.json();
  if (!response.ok || !value.ok) throw new Error(value.error || `HTTP ${response.status}`);
  return value;
}

async function post(path, payload) {
  return request(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
    body: JSON.stringify(payload),
  });
}

function text(tag, value, className) {
  const element = document.createElement(tag);
  element.textContent = value;
  if (className) element.className = className;
  return element;
}

function formatBytes(raw) {
  const value = Number(raw || 0);
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  const number = value / (1024 ** index);
  return `${number >= 100 || index === 0 ? number.toFixed(0) : number.toFixed(1)} ${units[index]}`;
}

function renderStages(items) {
  const root = $("stages");
  root.replaceChildren();
  items.forEach((item, index) => {
    const li = document.createElement("li");
    li.className = `stage ${item.status}`;
    li.append(
      text("span", item.status === "done" ? "✓" : String(index + 1), "stage-number"),
      text("h3", item.title),
      text("p", item.detail),
    );
    root.append(li);
  });
}

function renderReadiness(items) {
  const root = $("readiness");
  root.replaceChildren();
  items.forEach((item) => {
    const li = document.createElement("li");
    li.className = item.ok ? "ok" : "";
    const body = document.createElement("div");
    body.append(text("div", item.label, "check-label"), text("div", item.detail, "check-detail"));
    li.append(text("span", "", "check-dot"), body);
    root.append(li);
  });
}

function renderEvents(items) {
  const root = $("events");
  root.replaceChildren();
  [...items].reverse().forEach((item) => {
    const li = document.createElement("li");
    li.append(
      text("strong", `${String(item.sequence || "").padStart(3, "0")} · ${item.type}`),
      text("span", item.timestamp || ""),
    );
    root.append(li);
  });
  if (!items.length) root.append(text("li", "尚无事件"));
}

function renderArtifacts(items) {
  const root = $("artifactList");
  root.replaceChildren();
  $("artifactCount").textContent = `${items.length} 个可预览文件`;
  items.forEach((item) => {
    const button = text("button", item.path, "artifact-item" + (item.path === selectedArtifact ? " active" : ""));
    button.type = "button";
    button.title = `${item.bytes} bytes`;
    button.addEventListener("click", () => loadArtifact(item.path));
    root.append(button);
  });
  if (!items.length) root.append(text("div", "会话开始后，输出文件将在这里出现。", "muted-text"));
  if (!selectedArtifact && items.length) {
    const preferred = [...items].reverse().find((item) => item.suffix === ".md") || items[items.length - 1];
    selectedArtifact = preferred.path;
    queueMicrotask(() => loadArtifact(preferred.path));
  }
}

function fillSettings(settings) {
  const form = $("settingsForm");
  for (const [name, value] of Object.entries(settings)) {
    const input = form.elements.namedItem(name);
    if (input) input.value = value ?? "";
  }
  if (!$("abcOutRoot").value) $("abcOutRoot").value = "artifacts/abc_dataset_full";
  if (settings.campaign_dataset && !$("abcExistingPath").value) $("abcExistingPath").value = settings.campaign_dataset;
}

function badge(element, label, kind = "muted") {
  element.textContent = label;
  element.className = "badge" + (kind ? ` ${kind}` : "");
}

function renderABC(snapshot) {
  const value = snapshot || { status: "idle", progress: {} };
  const progress = value.progress || {};
  const download = progress.download || {};
  const status = String(value.status || progress.status || "idle");
  const running = ["running", "cancelling"].includes(status);
  const failed = status === "failed";
  badge($("abcStatusBadge"), status, failed ? "error" : (running ? "" : "muted"));

  const percent = Math.max(0, Math.min(100, Number(download.percent || (status === "completed" ? 100 : 0))));
  $("abcProgress").value = percent;
  $("abcProgressText").textContent = `${percent.toFixed(1)}%`;
  $("abcPhase").textContent = progress.message || progress.phase || value.operation || "尚未开始";
  $("abcBytes").textContent = `${formatBytes(download.completed_bytes)} / ${formatBytes(download.total_bytes)}`;
  $("abcArchives").textContent = `${download.archives_completed || 0} / ${download.archives_total || 0} archives`;
  const current = download.current;
  $("abcCurrent").textContent = current
    ? `${current.chunk || ""} ${current.format || ""} · ${current.archive || ""}`.trim()
    : (value.error || progress.error || "—");

  $("abcPlanButton").disabled = running;
  $("abcSampleButton").disabled = running;
  $("abcFetchButton").disabled = running;
  $("abcCancelButton").disabled = !running;
  if (progress.dataset_index && !$("abcExistingPath").value) $("abcExistingPath").value = progress.dataset_index;
}

function renderABCInspection(report) {
  const root = $("abcInspection");
  if (!report) {
    root.textContent = "尚未检查已有索引或 fetch 根目录。";
    $("abcUseButton").disabled = true;
    return;
  }
  const pieces = [report.kind || "unknown"];
  if (report.total_files) pieces.push(`${report.total_files} files`);
  if (report.archive_count) pieces.push(`${report.archive_count} archives`);
  if (report.needs_index) pieces.push("需要先生成 dataset_index.json");
  const issues = [...(report.errors || []), ...(report.warnings || [])].slice(0, 3);
  root.textContent = `${report.ready ? "可用于 Harness" : "尚未就绪"} · ${pieces.join(" · ")}${issues.length ? ` · ${issues.join("；")}` : ""}`;
  root.style.color = report.ready ? "var(--accent-strong)" : "var(--muted)";
  $("abcUseButton").disabled = !report.ready;
}

function selectedInstallation(detection) {
  const installations = Array.isArray(detection.installations) ? detection.installations : [];
  return installations.find((item) => item.root === detection.selected_root) || installations[0] || null;
}

function nxInstallationIdentity(detection) {
  const installation = selectedInstallation(detection || {});
  const root = String(detection?.selected_root || installation?.root || "").toLowerCase();
  const journal = String(installation?.paths?.run_journal || "").toLowerCase();
  return root ? `${root}|${journal}` : "";
}

function renderNX(nx) {
  const detection = nx?.detection || {};
  const probe = nx?.probe || { status: "not_run" };
  const installation = selectedInstallation(detection);
  const detectionOk = Boolean(detection.ok);
  const probeMatchesDetection = Boolean(probe.ok)
    && nxInstallationIdentity(probe.environment || {}) === nxInstallationIdentity(detection);
  badge($("nxStatusBadge"), detection.status || (detectionOk ? "ready" : "not found"), detectionOk ? "" : "muted");
  $("nxSummary").textContent = detectionOk
    ? `已找到 NX：${detection.selected_root || installation?.root || "安装路径已确认"}`
    : (detection.diagnostics?.[0]?.message || "未找到可用于 journal 的 NX 安装。请点击环境检查旁的配置并指定路径。");

  const capabilities = installation?.capabilities || {};
  const labels = [
    ["nx_installed", "NX 安装"],
    ["gui_executable", "NX GUI"],
    ["journal_runner", "Journal runner"],
    ["python_runtime_evidence", "Python 运行时线索"],
    ["python_api_verified", "NXOpen 已验证"],
  ];
  const root = $("nxCapabilities");
  root.replaceChildren();
  labels.forEach(([key, label]) => {
    const ok = Boolean(capabilities[key]) || (key === "python_api_verified" && probeMatchesDetection);
    root.append(text("li", `${ok ? "✓" : "○"} ${label}`, ok ? "ok" : ""));
  });

  const probeStatus = probe.status || "not_run";
  const detail = probe.error || probe.probe?.error || probe.diagnostics?.[0]?.message || "";
  $("nxProbeSummary").textContent = probeStatus === "not_run"
    ? "尚未运行真实探针。"
    : `${probe.ok ? "NX Python API 验证通过" : `探针状态：${probeStatus}`}${detail ? ` · ${detail}` : ""}`;
}

function render(next) {
  state = next;
  csrf = next.csrf_token;
  const session = next.session;
  $("taskTitle").textContent = session.public_function || "等待输入公开接口";
  $("taskMeta").textContent = session.session_id
    ? `会话 ${session.session_id} · 第 ${session.current_round} 轮`
    : "配置 SiliconFlow、SGGK SDK 与 Runner 后即可开始。";
  $("sessionBadge").textContent = `会话 · ${session.state || "idle"}`;
  const busy = next.job.status === "running";
  $("jobBadge").textContent = busy ? `任务 · ${next.job.operation} 运行中` : `任务 · ${next.job.status}`;
  $("jobBadge").className = "badge" + (busy ? "" : " muted");

  renderStages(next.stages);
  renderReadiness(next.readiness);
  renderEvents(next.events);
  renderArtifacts(next.artifacts);
  renderABC(next.abc);
  renderNX(next.nx);
  if (!settingsInitialized) {
    fillSettings(next.settings);
    settingsInitialized = true;
  }

  document.querySelectorAll("[data-job-action]").forEach((button) => { button.disabled = busy; });
  $("startButton").disabled = busy || !next.ready_to_start;
  $("nxProbeButton").disabled = busy;
  if (next.job.status === "failed" && next.job.error && next.job.error !== lastJobError) {
    lastJobError = next.job.error;
    toast(next.job.error, true);
  }
  if (next.job.status !== "failed") lastJobError = "";
}

async function refresh() {
  try { render(await request("/api/state")); }
  catch (error) { toast(error.message, true); }
}

async function loadArtifact(path) {
  try {
    const value = await request(`/api/artifact?path=${encodeURIComponent(path)}`);
    selectedArtifact = path;
    $("previewPath").textContent = path;
    $("artifactPreview").textContent = value.content;
    renderArtifacts(state.artifacts);
  } catch (error) { toast(error.message, true); }
}

async function action(path, payload, message) {
  try {
    const result = await post(path, payload);
    toast(message);
    await refresh();
    return result;
  } catch (error) {
    toast(error.message, true);
    return null;
  }
}

async function startABC(mode) {
  const outRoot = $("abcOutRoot").value.trim();
  if (!outRoot) return toast("请先填写 ABC 数据工作目录。", true);
  if (mode === "full" && !window.confirm("全量 ABC 压缩包约 100 GiB，完整解压需要更多磁盘空间。确认开始？")) return;
  const payload = { mode, out_root: outRoot };
  const downloadRoot = $("abcDownloadRoot").value.trim();
  if (downloadRoot) payload.download_root = downloadRoot;
  if (mode === "sample") Object.assign(payload, { smallest_step: 1, sample_count: 50 });
  await action("/api/abc/fetch", payload, mode === "plan" ? "已开始生成全量下载计划" : "ABC 拉取已启动");
}

$("startForm").addEventListener("submit", (event) => {
  event.preventDefault();
  action("/api/start", { public_function: $("publicFunction").value.trim() }, "已开始生成，页面会自动更新");
});
$("commentForm").addEventListener("submit", (event) => {
  event.preventDefault();
  action("/api/comment", { comment: $("comment").value.trim() }, "审查意见已提交");
});
$("approveButton").addEventListener("click", () => action("/api/approve", {}, "已批准，开始 SDK 实测"));
$("retryButton").addEventListener("click", () => action("/api/retry", {}, "已提交重试"));
$("buildButton").addEventListener("click", () => action("/api/build", {}, "已开始构建 Runner"));
$("refreshButton").addEventListener("click", refresh);
$("settingsToggle").addEventListener("click", () => $("settingsPanel").classList.toggle("hidden"));

$("abcPlanButton").addEventListener("click", () => startABC("plan"));
$("abcSampleButton").addEventListener("click", () => startABC("sample"));
$("abcFetchButton").addEventListener("click", () => startABC("full"));
$("abcCancelButton").addEventListener("click", () => action("/api/abc/cancel", {}, "正在取消 ABC 拉取"));
$("abcValidateButton").addEventListener("click", async () => {
  const path = $("abcExistingPath").value.trim();
  if (!path) return toast("请填写含 dataset_index.json 的 fetch 根目录或索引文件。", true);
  try {
    const result = await post("/api/abc/validate", { path });
    abcInspection = result.inspection;
    renderABCInspection(abcInspection);
  } catch (error) { toast(error.message, true); }
});
$("abcUseButton").addEventListener("click", async () => {
  const path = abcInspection?.dataset_index || abcInspection?.root || $("abcExistingPath").value.trim();
  const result = await action("/api/abc/use-existing", { path }, "ABC 数据集已绑定到 Harness");
  if (result?.settings) {
    const input = $("settingsForm").elements.namedItem("campaign_dataset");
    if (input) input.value = result.settings.campaign_dataset || "";
  }
});

$("nxRefreshButton").addEventListener("click", async () => {
  try {
    const result = await request("/api/nx/environment");
    if (state) state.nx = result.nx;
    renderNX(result.nx);
    toast("NX 静态检测已刷新");
  } catch (error) { toast(error.message, true); }
});
$("nxProbeButton").addEventListener("click", () => action("/api/nx/probe", {}, "NX Python 探针已启动"));

$("settingsForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  const numeric = new Set([
    "candidate_count",
    "candidate_parallelism",
    "jobs",
    "execution_timeout_seconds",
    "nx_probe_timeout_seconds",
  ]);
  const settings = {};
  for (const [key, value] of form.entries()) {
    if (key !== "api_key") settings[key] = numeric.has(key) ? Number(value) : String(value);
  }
  const result = await action(
    "/api/settings",
    { settings, api_key: String(form.get("api_key") || "") },
    "配置已保存到本机",
  );
  if (result) event.currentTarget.elements.namedItem("api_key").value = "";
});

renderABCInspection(null);
refresh();
setInterval(refresh, 2000);
