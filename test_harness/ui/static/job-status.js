"use strict";

window.HarnessJobStatus = (() => {
  const TERMINAL_LABELS = {
    idle: "空闲",
    completed: "已完成",
    failed: "失败",
  };

  let ticker = null;
  let activeKey = "";
  let activeOperation = "";
  let startedAt = 0;
  let currentSettings = {};
  let currentStages = [];

  const element = (id) => document.getElementById(id);

  function modelName(settings) {
    const configured = String(settings?.model || "GLM-5.2").trim();
    return configured.split("/").filter(Boolean).pop() || "GLM-5.2";
  }

  function operationCopy(operation, settings) {
    const model = modelName(settings);
    const copies = {
      start: {
        kicker: model,
        title: "模型处理中",
        detail: "正在等待模型生成并校验测试代码。首次生成可能需要数分钟，请保持页面开启。",
      },
      comment: {
        kicker: model,
        title: "模型处理中",
        detail: "正在等待模型根据审查意见修改测试方案，请保持页面开启。",
      },
      approve: {
        kicker: "REVIEW + SDK",
        title: "模型与 SDK 处理中",
        detail: "正在确认批准决定并执行 SDK 测试。",
      },
      retry: {
        kicker: "SDK TEST",
        title: "SDK 测试运行中",
        detail: "正在重新执行当前测试，请等待结果。",
      },
      build: {
        kicker: "LOCAL BUILD",
        title: "Runner 构建中",
        detail: "正在构建本地 SGGK Runner。",
      },
      nx_probe: {
        kicker: "NX PYTHON API",
        title: "NX 探针运行中",
        detail: "正在通过 journal runner 验证 NXOpen，请勿关闭 NX 相关进程。",
      },
    };
    return copies[operation] || {
      kicker: "HARNESS",
      title: "后台任务运行中",
      detail: "Harness 正在处理当前请求，请保持页面开启。",
    };
  }

  function parseStartedAt(value) {
    if (typeof value === "number" && Number.isFinite(value)) {
      return value < 1e12 ? value * 1000 : value;
    }
    const parsed = Date.parse(String(value || ""));
    return Number.isFinite(parsed) ? parsed : 0;
  }

  function formatElapsed(milliseconds) {
    const totalSeconds = Math.max(0, Math.floor(milliseconds / 1000));
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    const shortClock = `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
    return hours ? `${String(hours).padStart(2, "0")}:${shortClock}` : shortClock;
  }

  function activeStageLine(stages) {
    if (!Array.isArray(stages) || !stages.length) return "";
    const active = stages.find((stage) => stage && stage.status === "active");
    if (!active || !active.title) return "";
    return `当前阶段：${active.title}${active.detail ? `（${active.detail}）` : ""}`;
  }

  function paint() {
    if (!startedAt) return;
    const copy = operationCopy(activeOperation, currentSettings);
    const elapsed = formatElapsed(Date.now() - startedAt);
    const stageLine = activeStageLine(currentStages);
    element("jobProgressKicker").textContent = copy.kicker;
    element("jobProgressTitle").textContent = copy.title;
    element("jobProgressDetail").textContent = stageLine ? `${copy.detail} ${stageLine}` : copy.detail;
    element("jobElapsed").textContent = elapsed;
    element("jobBadge").textContent = `任务 · ${copy.title} · ${elapsed}`;
    element("jobBadge").className = "badge";
  }

  function stop(job = null) {
    if (ticker !== null) {
      clearInterval(ticker);
      ticker = null;
    }
    activeKey = "";
    activeOperation = "";
    startedAt = 0;
    const progress = element("jobProgress");
    progress.hidden = true;
    progress.setAttribute("aria-busy", "false");
    if (job) {
      const status = String(job.status || "idle");
      element("jobBadge").textContent = `任务 · ${TERMINAL_LABELS[status] || status}`;
      element("jobBadge").className = "badge" + (status === "failed" ? " error" : " muted");
    }
  }

  function sync(job, settings = {}, stages = []) {
    currentSettings = settings || {};
    currentStages = Array.isArray(stages) ? stages : [];
    if (!job || job.status !== "running") {
      stop(job);
      return;
    }

    const operation = String(job.operation || "");
    const authoritativeStart = parseStartedAt(job.started_at);
    const nextStart = authoritativeStart || (operation === activeOperation && startedAt ? startedAt : Date.now());
    const nextKey = `${operation}:${nextStart}`;
    if (nextKey !== activeKey) {
      if (ticker !== null) clearInterval(ticker);
      activeKey = nextKey;
      activeOperation = operation;
      startedAt = nextStart;
      ticker = setInterval(paint, 1000);
    }

    const progress = element("jobProgress");
    progress.hidden = false;
    progress.setAttribute("aria-busy", "true");
    paint();
  }

  function begin(operation, settings = {}) {
    sync({ status: "running", operation, started_at: new Date().toISOString() }, settings);
  }

  return Object.freeze({ begin, formatElapsed, stop, sync });
})();
