"use strict";

let state = null;
let csrf = "";
let selectedArtifact = "";
let selectedArtifactSession = null;
let settingsInitialized = false;
let lastJobError = "";
let abcInspection = null;
let toastTimer = null;
let artifactRequestSerial = 0;
const artifactGroupState = new Map();
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

const artifactGroupDefaults = {
  reports: { label: "重点报告", description: "建议先看这里，了解当前方案或最终结论。", order: 10 },
  proposal: { label: "测试方案与代码", description: "模型生成并通过固定门禁的测试内容。", order: 20 },
  execution: { label: "SDK 运行结果", description: "真实 SDK 执行的结果、诊断与日志。", order: 30 },
  review: { label: "审查与批准记录", description: "用户意见、模型理解与执行批准记录。", order: 40 },
  details: { label: "技术细节", description: "Harness 内部清单、提示词、事件和完整性记录。", order: 50 },
};

function resetArtifactPreview() {
  artifactRequestSerial += 1;
  $("previewTitle").textContent = "选择一个文件查看";
  $("previewPath").textContent = "完整路径会显示在这里";
  $("previewDescription").textContent = (
    "重点报告会自动打开；技术细节默认收起。"
    + "SGT、PNG 等二进制产物仍保留在 session 目录，不在浏览器中预览。"
  );
  renderRawArtifact("这里会安全显示 Markdown、JSON、C++、Python 和日志文本；二进制产物请从 session 目录查看。");
  $("copyArtifactPath").classList.add("hidden");
}

function renderRawArtifact(content) {
  const preview = $("artifactPreview");
  const raw = document.createElement("pre");
  raw.className = "artifact-raw-preview";
  raw.textContent = String(content || "");
  preview.classList.remove("is-markdown");
  preview.replaceChildren(raw);
}

function renderArtifactContent(item, content) {
  const preview = $("artifactPreview");
  const markdown = String(item?.suffix || "").toLowerCase() === ".md";
  if (markdown && window.HarnessMarkdownPreview?.renderInto) {
    preview.classList.add("is-markdown");
    window.HarnessMarkdownPreview.renderInto(preview, content);
    return;
  }
  renderRawArtifact(content);
}

function artifactByPath(path) {
  return (state?.artifacts || []).find((item) => item.path === path) || null;
}

function updateArtifactSelection() {
  document.querySelectorAll("[data-artifact-path]").forEach((button) => {
    const active = button.dataset.artifactPath === selectedArtifact;
    button.classList.toggle("active", active);
    if (button.classList.contains("artifact-item")) {
      button.setAttribute("aria-current", active ? "true" : "false");
    }
  });
}

function renderArtifactSummary(summary, items) {
  const value = summary || {};
  const tones = new Set(["neutral", "info", "ready", "success", "error"]);
  const tone = tones.has(value.tone) ? value.tone : "neutral";
  $("artifactSummary").className = `artifact-summary tone-${tone}`;
  $("artifactSummaryTitle").textContent = value.title || (items.length ? "测试产物已生成" : "还没有测试产物");
  $("artifactSummaryDetail").textContent = value.detail || (items.length
    ? "从下方分组中选择报告、方案或运行结果。"
    : "开始生成后，这里会告诉你应该先看什么。");

  const error = $("artifactSummaryError");
  error.textContent = value.error || "";
  error.classList.toggle("hidden", !value.error);

  const actions = $("artifactActions");
  actions.replaceChildren();
  (value.actions || []).forEach((action) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `artifact-action role-${action.role || "report"}`;
    button.dataset.artifactPath = action.path;
    button.disabled = action.previewable === false;
    button.title = action.path;
    button.append(
      text("strong", action.label || "查看产物"),
      text("span", action.hint || "打开预览", "artifact-action-hint"),
      text("span", action.path, "artifact-action-path"),
    );
    button.addEventListener("click", () => loadArtifact(action.path));
    actions.append(button);
  });
}

function renderRoundOverview(overview) {
  const root = $("roundOverview");
  root.replaceChildren();
  const value = overview || {};
  // Only show the card when there is a round to summarize or a failure to explain.
  if (!value.available && !value.failure_reason) {
    root.classList.add("hidden");
    return;
  }
  root.className = `round-overview tone-${value.tone || "neutral"}`;

  const head = document.createElement("div");
  head.className = "round-overview-head";
  head.append(
    text("span", "", "round-overview-mark"),
    text("h3", value.headline || "本轮概览", "round-overview-title"),
  );

  const chips = document.createElement("div");
  chips.className = "round-overview-chips";
  const addChip = (label) => {
    if (!label) return;
    chips.append(text("span", label, "round-chip"));
  };
  if (value.round_number) addChip(`第 ${value.round_number} 轮`);
  if (value.candidate_kind) addChip(`类型 ${value.candidate_kind}`);
  if (value.case_count) addChip(`${value.case_count} 个用例`);
  if (value.oracle_count) addChip(`${value.oracle_count} 个 Oracle`);
  if (value.gate_ok === true) addChip("机器门禁通过");
  else if (value.gate_ok === false) addChip("机器门禁未通过");
  if (chips.childNodes.length) head.append(chips);

  root.append(head);

  const fields = [
    { label: "测试思路", value: value.purpose },
    { label: "风险覆盖", value: value.risk },
    { label: "预期行为", value: value.expected },
    { label: "下一步", value: value.next_hint },
  ];
  fields.forEach((field) => {
    if (!field.value) return;
    const row = document.createElement("div");
    row.className = "round-overview-field";
    row.append(text("span", field.label, "round-overview-label"), text("span", field.value, "round-overview-value"));
    root.append(row);
  });

  if (value.failure_reason) {
    const failure = document.createElement("div");
    failure.className = "round-overview-failure";
    failure.append(
      text("strong", "失败原因"),
      text("span", value.failure_reason, "round-overview-failure-text"),
    );
    root.append(failure);
  }

  const actions = document.createElement("div");
  actions.className = "round-overview-actions";
  const addAction = (label, path) => {
    if (!path) return;
    const item = artifactByPath(path);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "round-overview-action";
    button.disabled = item?.previewable === false;
    button.title = path;
    button.append(text("strong", label), text("span", path, "round-overview-action-path"));
    button.addEventListener("click", () => loadArtifact(path));
    actions.append(button);
  };
  if (value.failure_reason && value.tone === "error") {
    addAction("查看失败报告", value.review_report_path || value.fixed_review_report_path);
  } else {
    addAction("查看本轮审查报告", value.review_report_path || value.fixed_review_report_path);
  }
  addAction("查看完整测试方案", value.candidate_path);
  if (actions.childNodes.length) root.append(actions);
}

function renderArtifacts(items, summary = {}) {
  const root = $("artifactList");
  const scrollTop = root.scrollTop;
  root.replaceChildren();
  renderArtifactSummary(summary, items);

  const previewable = items.filter((item) => item.previewable !== false).length;
  $("artifactCount").textContent = previewable === items.length
    ? `${items.length} 个可查看文件`
    : `${previewable} / ${items.length} 个可直接预览文件`;

  if (selectedArtifact && !items.some((item) => item.path === selectedArtifact)) {
    selectedArtifact = "";
    resetArtifactPreview();
  }

  const summaryGroups = new Map((summary.groups || []).map((group) => [group.id, group]));
  const groups = new Map();
  items.forEach((item) => {
    const id = item.group || "details";
    if (!groups.has(id)) {
      const fallback = artifactGroupDefaults[id] || artifactGroupDefaults.details;
      const metadata = summaryGroups.get(id) || {};
      groups.set(id, {
        id,
        label: metadata.label || item.group_label || fallback.label,
        description: metadata.description || fallback.description,
        order: Number(metadata.order || item.group_order || fallback.order),
        items: [],
      });
    }
    groups.get(id).items.push(item);
  });

  [...groups.values()]
    .sort((left, right) => left.order - right.order)
    .forEach((group) => {
      group.items.sort((left, right) => {
        if (Boolean(left.featured) !== Boolean(right.featured)) return left.featured ? -1 : 1;
        return String(left.label || left.name).localeCompare(String(right.label || right.name), "zh-CN");
      });

      const section = document.createElement("details");
      section.className = `artifact-group group-${group.id}`;
      section.dataset.group = group.id;
      // Keep the first view compact: the user-facing report is open, while
      // proposal, execution, review and host details remain one click away.
      section.open = artifactGroupState.has(group.id) ? artifactGroupState.get(group.id) : group.id === "reports";
      section.addEventListener("toggle", () => artifactGroupState.set(group.id, section.open));

      const heading = document.createElement("summary");
      const headingCopy = document.createElement("span");
      headingCopy.className = "artifact-group-copy";
      headingCopy.append(
        text("strong", group.label),
        text("small", group.description),
      );
      heading.append(headingCopy, text("span", String(group.items.length), "artifact-group-count"));
      section.append(heading);

      const files = document.createElement("div");
      files.className = "artifact-group-files";
      group.items.forEach((item) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "artifact-item" + (item.path === selectedArtifact ? " active" : "");
        button.dataset.artifactPath = item.path;
        button.disabled = item.previewable === false;
        button.title = `${item.description || item.path}\n${item.path}`;
        button.setAttribute("aria-current", item.path === selectedArtifact ? "true" : "false");

        const top = document.createElement("span");
        top.className = "artifact-item-top";
        top.append(
          text("span", item.kind || item.suffix?.slice(1).toUpperCase() || "文件", "artifact-kind"),
          text("strong", item.label || item.name || item.path, "artifact-item-label"),
          text("span", formatBytes(item.bytes), "artifact-size"),
        );
        const slash = item.path.lastIndexOf("/");
        const folder = slash >= 0 ? item.path.slice(0, slash) : "会话根目录";
        button.append(top, text("span", folder, "artifact-item-path"));
        button.addEventListener("click", () => loadArtifact(item.path));
        files.append(button);
      });
      section.append(files);
      root.append(section);
    });

  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "artifact-empty";
    empty.append(
      text("strong", "等待测试产物"),
      text(
        "span",
        "会话开始后，可查看的文本报告、方案与运行结果会按用途出现在这里；二进制产物仍保留在 session 目录。",
      ),
    );
    root.append(empty);
  }

  root.scrollTop = scrollTop;
  updateArtifactSelection();
  if (!selectedArtifact && items.length) {
    const actionPath = (summary.actions || []).find((action) => action.previewable !== false)?.path;
    const preferred = items.find((item) => item.path === actionPath)
      || items.find((item) => item.featured && item.previewable !== false)
      || items.find((item) => item.group === "reports" && item.previewable !== false)
      || items.find((item) => item.previewable !== false);
    if (preferred) {
      selectedArtifact = preferred.path;
      updateArtifactSelection();
      queueMicrotask(() => loadArtifact(preferred.path));
    }
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

function setJobControlsBusy(busy) {
  document.querySelectorAll("[data-job-action]").forEach((button) => { button.disabled = busy; });
  $("startButton").disabled = busy || !Boolean(state?.ready_to_start);
  $("buildButton").disabled = busy || !Boolean(state?.ready_to_build);
  $("buildButton").title = state?.ready_to_build
    ? "使用自动检测到的 MSVC/CMake 工具链构建 Runner"
    : "需要有效的 SDK、CMake，以及 VS 2022 或 VS 2026 C++ 工具链";
  $("nxProbeButton").disabled = busy;
  const canApprove = state?.session?.state === "awaiting_comment";
  $("approveButton").disabled = busy || !canApprove;
  $("approveButton").title = canApprove
    ? "批准当前不可变候选并开始 SDK 实测"
    : "只有处于待审查状态的新候选才能批准；执行失败后请先修订或按规则重试。";
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
  if (selectedArtifactSession !== session.session_id) {
    selectedArtifactSession = session.session_id;
    selectedArtifact = "";
    artifactGroupState.clear();
    resetArtifactPreview();
  }
  $("taskTitle").textContent = session.public_function || "等待输入公开接口";
  $("taskMeta").textContent = session.session_id
    ? `会话 ${session.session_id} · 第 ${session.current_round} 轮`
    : "配置 SiliconFlow、SGGK SDK 与 Runner 后即可开始。";
  $("sessionBadge").textContent = `会话 · ${session.state || "idle"}`;
  const busy = next.job.status === "running";
  $("repairButton").hidden = session.state !== "execution_failed";
  window.HarnessJobStatus.sync(next.job, next.settings, next.stages);

  renderStages(next.stages);
  renderReadiness(next.readiness);
  renderEvents(next.events);
  renderRoundOverview(next.round_overview);
  renderArtifacts(next.artifacts, next.artifact_summary);
  renderABC(next.abc);
  renderNX(next.nx);
  if (!settingsInitialized) {
    fillSettings(next.settings);
    settingsInitialized = true;
  }

  setJobControlsBusy(busy);
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
  const item = artifactByPath(path);
  if (item?.previewable === false) {
    toast("该文件过大，无法在页面中安全预览。", true);
    return;
  }
  const requestSession = state?.session?.session_id || "";
  const requestSerial = ++artifactRequestSerial;
  try {
    const value = await request(`/api/artifact?path=${encodeURIComponent(path)}`);
    if (
      requestSerial !== artifactRequestSerial
      || requestSession !== (state?.session?.session_id || "")
    ) return;
    selectedArtifact = path;
    $("previewTitle").textContent = item?.label || item?.name || path;
    $("previewPath").textContent = path;
    $("previewDescription").textContent = [
      item?.description || "当前会话中的文本产物。",
      `${item?.kind || "文本"} · ${formatBytes(value.bytes)}`,
    ].join(" · ");
    renderArtifactContent(item, value.content);
    $("copyArtifactPath").classList.remove("hidden");
    updateArtifactSelection();
  } catch (error) {
    if (
      requestSerial === artifactRequestSerial
      && requestSession === (state?.session?.session_id || "")
    ) toast(error.message, true);
  }
}

async function copySelectedArtifactPath() {
  if (!selectedArtifact) return;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(selectedArtifact);
    } else {
      const area = document.createElement("textarea");
      area.value = selectedArtifact;
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.append(area);
      area.select();
      const copied = document.execCommand("copy");
      area.remove();
      if (!copied) throw new Error("copy unavailable");
    }
    toast("产物路径已复制");
  } catch (_error) {
    toast("无法自动复制，请从预览标题栏手动复制路径。", true);
  }
}

async function action(path, payload, message, operation = "") {
  if (operation) {
    window.HarnessJobStatus.begin(operation, state?.settings);
    setJobControlsBusy(true);
  }
  try {
    const result = await post(path, payload);
    if (operation && result.job) window.HarnessJobStatus.sync(result.job, state?.settings);
    toast(message);
    await refresh();
    return result;
  } catch (error) {
    if (operation) {
      window.HarnessJobStatus.stop({ status: "failed" });
      setJobControlsBusy(false);
      await refresh();
    }
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
  action(
    "/api/start",
    {
      public_function: $("publicFunction").value.trim(),
      use_memory: !$("startNoMemory").checked,
    },
    "已开始生成，页面会自动更新",
    "start",
  );
});
$("commentForm").addEventListener("submit", (event) => {
  event.preventDefault();
  action("/api/comment", { comment: $("comment").value.trim() }, "审查意见已提交", "comment");
});
$("approveButton").addEventListener("click", () => action("/api/approve", {}, "已批准，开始 SDK 实测", "approve"));
$("repairButton").addEventListener("click", () => action(
  "/api/comment",
  {
    comment: (
      "请根据失败诊断修改测试方案，结合上一轮已绑定的执行证据修复候选代码或测试 oracle 的问题，"
      + "保留真实语义检查且不要掩盖 SDK 缺陷，生成完整的新候选供我重新审查。"
    ),
  },
  "已提交失败诊断，正在生成可重新审查的修复版",
  "comment",
));
$("retryButton").addEventListener("click", () => action("/api/retry", {}, "已提交重试", "retry"));
$("buildButton").addEventListener("click", () => action("/api/build", {}, "已开始构建 Runner", "build"));
$("refreshButton").addEventListener("click", refresh);
$("copyArtifactPath").addEventListener("click", copySelectedArtifactPath);
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
$("nxProbeButton").addEventListener("click", () => (
  action("/api/nx/probe", {}, "NX Python 探针已启动", "nx_probe")
));

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
