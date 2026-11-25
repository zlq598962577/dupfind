/* dupfind web v1：单页交互（原生 JS，无构建步骤） */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const PAGE_SIZE = 20;
  const POLL_INTERVAL = 700;

  const state = {
    view: null,
    status: "all",
    search: "",
    minSavings: 0,
    keep: "shortest",
    visible: PAGE_SIZE,
    openGroups: new Set(),
    taskId: null,
    pollTimer: null,
    volumes: [],
  };

  const STATUS_TEXT = { confirmed: "确认", suspect: "疑似", shared: "硬链接" };
  const PHASE_TEXT = { scanning: "遍历中", grouping: "分组中", verifying: "内容校验中", done: "完成" };
  const KEEP_TEXT = { shortest: "最短名称", newest: "最新修改" };

  // ---------- 工具函数 ----------

  function formatBytes(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let index = 0;
    let current = value;
    while (current >= 1024 && index < units.length - 1) {
      current /= 1024;
      index += 1;
    }
    const digits = current >= 100 || index === 0 ? 0 : 1;
    return `${current.toFixed(digits)} ${units[index]}`;
  }

  function parseSize(text) {
    const raw = String(text == null ? "" : text).trim();
    if (!raw) return 0;
    const match = /^(\d+(?:\.\d+)?)\s*(b|kb|mb|gb|tb|kib|mib|gib|tib)?$/i.exec(raw);
    if (!match) throw new Error(`无法解析大小: ${raw}`);
    const factors = { b: 1, kb: 1024, mb: 1024 ** 2, gb: 1024 ** 3, tb: 1024 ** 4, kib: 1024, mib: 1024 ** 2, gib: 1024 ** 3, tib: 1024 ** 4 };
    const unit = (match[2] || "b").toLowerCase();
    return Math.floor(Number(match[1]) * factors[unit]);
  }

  function formatTime(iso) {
    if (!iso) return "—";
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return iso;
    const pad = (n) => String(n).padStart(2, "0");
    return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
  }

  let toastTimer = null;
  function toast(message, isError) {
    const node = $("toast");
    node.textContent = message;
    node.classList.toggle("error", Boolean(isError));
    node.classList.add("show");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.remove("show"), isError ? 5000 : 2800);
  }

  async function api(path, options) {
    const opts = Object.assign({}, options);
    if (opts.body !== undefined) {
      opts.headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
      opts.body = JSON.stringify(opts.body);
    }
    const response = await fetch(path, opts);
    let data = null;
    try {
      data = await response.json();
    } catch (ignored) {
      data = null;
    }
    if (!response.ok) {
      const detail = data && (data.detail || data.message);
      const text = typeof detail === "string" ? detail : `请求失败（${response.status}）`;
      throw new Error(text);
    }
    return data;
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  // ---------- 主题 ----------

  function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const dark = theme === "dark";
    $("themeIcon").textContent = dark ? "◑" : "◐";
    $("themeLabel").textContent = dark ? "浅色" : "深色";
    $("themeToggle").setAttribute("aria-label", dark ? "切换浅色模式" : "切换深色模式");
    try {
      localStorage.setItem("dupfind-theme", theme);
    } catch (ignored) {
      /* 隐私模式下忽略 */
    }
  }

  function initTheme() {
    let stored = null;
    try {
      stored = localStorage.getItem("dupfind-theme");
    } catch (ignored) {
      stored = null;
    }
    applyTheme(stored === "dark" ? "dark" : "light");
    $("themeToggle").addEventListener("click", () => {
      applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
    });
  }

  // ---------- 汇总栏 ----------

  function setSummary(values) {
    $("statFiles").textContent = values.files;
    $("statCandidates").textContent = values.candidates;
    $("statConfirmed").textContent = values.confirmed;
    $("statSavings").textContent = values.savings;
    $("statExcluded").textContent = values.excluded;
    $("statErrors").textContent = values.errors;
    $("partialFlag").hidden = !values.partial;
  }

  function summaryFromView(view) {
    const scan = view.scan || {};
    const summary = view.summary || {};
    return {
      files: String(scan.filesScanned != null ? scan.filesScanned : "—"),
      candidates: String(summary.groups != null ? summary.groups : "—"),
      confirmed: String(summary.confirmedGroups != null ? summary.confirmedGroups : "—"),
      savings: formatBytes(summary.savingsBytes || 0),
      excluded: `${scan.excludedDirs != null ? scan.excludedDirs : 0} / ${scan.excludedFiles != null ? scan.excludedFiles : 0}`,
      errors: `${scan.accessDenied != null ? scan.accessDenied : 0} / ${scan.errors != null ? scan.errors : 0}`,
      partial: Boolean(view.partial || scan.partial),
    };
  }

  function resetSummary() {
    setSummary({ files: "—", candidates: "—", confirmed: "—", savings: "—", excluded: "—", errors: "—", partial: false });
  }

  // ---------- 扫描 ----------

  function setScanning(running) {
    $("startBtn").disabled = running;
    $("startBtn").textContent = running ? "扫描中…" : "开始扫描";
    $("cancelBtn").hidden = !running;
    $("cancelBtn").disabled = false;
    $("cancelBtn").textContent = "取消扫描";
  }

  function setPhase(phase) {
    $("phaseLabel").textContent = PHASE_TEXT[phase] || phase || "处理中";
  }

  function updateProgress(task) {
    $("progress").hidden = false;
    const phase = task.phase === "done" ? "done" : task.phase || "scanning";
    setPhase(phase);

    const files = task.filesScanned || 0;
    const candidates = task.candidateGroups || 0;
    const confirmed = task.confirmedGroups || 0;
    const processed = task.processedGroups || 0;

    const bar = $("progressBar");
    if (phase === "verifying") {
      bar.classList.remove("indeterminate");
      const percent = candidates > 0 ? Math.min(100, Math.round((processed / candidates) * 100)) : 0;
      bar.firstElementChild.style.width = `${percent}%`;
      $("progressDetail").textContent = `${processed} / ${candidates} 组 · ${files} 个文件`;
    } else if (phase === "done") {
      bar.classList.remove("indeterminate");
      bar.firstElementChild.style.width = "100%";
      $("progressDetail").textContent =
        candidates > 0 ? `${candidates} 个候选组 · ${files} 个文件` : `${files} 个文件`;
    } else {
      bar.classList.add("indeterminate");
      bar.firstElementChild.style.width = "";
      $("progressDetail").textContent = `${files} 个文件`;
    }

    $("liveCandidates").textContent = candidates;
    $("liveConfirmed").textContent = confirmed;
    $("liveExcluded").textContent = (task.excludedDirs || 0) + (task.excludedFiles || 0);
    $("liveDenied").textContent = task.accessDenied || 0;

    setSummary({
      files: String(files),
      candidates: String(candidates),
      confirmed: String(confirmed),
      savings: state.view ? formatBytes(state.view.summary.savingsBytes || 0) : "—",
      excluded: `${task.excludedDirs || 0} / ${task.excludedFiles || 0}`,
      errors: `${task.accessDenied || 0} / ${task.errors || 0}`,
      partial: Boolean(task.partial),
    });
  }

  function stopPolling() {
    if (state.pollTimer) {
      clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
    state.taskId = null;
  }

  function pollTask(taskId) {
    state.pollTimer = setTimeout(async () => {
      try {
        const task = await api(`/api/scan/${encodeURIComponent(taskId)}`);
        updateProgress(task);
        if (task.state === "running") {
          pollTask(taskId);
          return;
        }
        stopPolling();
        setScanning(false);
        await finishScan(task);
      } catch (error) {
        stopPolling();
        setScanning(false);
        toast(`进度查询失败：${error.message}`, true);
      }
    }, POLL_INTERVAL);
  }

  async function finishScan(task) {
    if (task.state === "failed") {
      toast(`扫描失败：${task.error || "未知错误"}`, true);
      setPhase("done");
      return;
    }
    if (task.state === "cancelled") {
      toast("扫描已取消，已保存 partial 报告", false);
    } else {
      toast(`扫描完成：${task.filesScanned} 个文件，${task.confirmedGroups} 个确认组`);
    }
    if (task.reportPath) {
      $("reportPathInput").value = task.reportPath;
      await loadReport(task.reportPath, { silent: true });
    }
  }

  async function startScan(event) {
    event.preventDefault();
    const root = $("rootInput").value.trim();
    if (!root) {
      toast("请填写扫描根路径", true);
      $("rootInput").focus();
      return;
    }
    if (state.taskId) {
      toast("已有扫描在进行中，请先取消", true);
      return;
    }
    let minSize = "1KB";
    let maxDepth = null;
    try {
      minSize = $("minSizeInput").value.trim() || "1KB";
      parseSize(minSize);
      const depthRaw = $("maxDepthInput").value.trim();
      if (depthRaw) {
        maxDepth = Number(depthRaw);
        if (!Number.isInteger(maxDepth) || maxDepth < 0) throw new Error("递归深度必须是非负整数");
      }
    } catch (error) {
      toast(error.message, true);
      return;
    }

    setScanning(true);
    $("progress").hidden = false;
    $("progressBar").classList.add("indeterminate");
    setPhase("scanning");
    setSummary({ files: "0", candidates: "0", confirmed: "0", savings: "—", excluded: "0 / 0", errors: "0 / 0", partial: false });

    try {
      const task = await api("/api/scan", {
        method: "POST",
        body: {
          root,
          minSize,
          noHash: $("noHashInput").checked,
          contentOnly: $("contentOnlyInput").checked,
          maxDepth,
        },
      });
      state.taskId = task.id;
      updateProgress(task);
      pollTask(task.id);
    } catch (error) {
      setScanning(false);
      $("progress").hidden = true;
      toast(`无法启动扫描：${error.message}`, true);
    }
  }

  async function cancelScan() {
    if (!state.taskId) return;
    $("cancelBtn").disabled = true;
    $("cancelBtn").textContent = "取消中…";
    try {
      await api(`/api/scan/${encodeURIComponent(state.taskId)}/cancel`, { method: "POST" });
      $("phaseLabel").textContent = "正在取消…";
    } catch (error) {
      $("cancelBtn").disabled = false;
      $("cancelBtn").textContent = "取消扫描";
      toast(`取消失败：${error.message}`, true);
    }
  }

  // ---------- 报告 ----------

  function resetFilters() {
    state.status = "all";
    state.search = "";
    state.minSavings = 0;
    state.visible = PAGE_SIZE;
    state.openGroups.clear();
    $("searchInput").value = "";
    $("minSavingsInput").value = "";
    document.querySelectorAll("#statusFilter .segment").forEach((button) => {
      button.classList.toggle("active", button.dataset.status === "all");
    });
  }

  function setReportMeta(view) {
    $("metaRoot").textContent = view.root || "—";
    $("metaPath").textContent = view.reportPath || "—";
    const summary = view.summary || {};
    if (summary.confirmedGroups > 0) {
      $("metaHash").textContent = "已启用（采样 + 全量哈希）";
    } else if (summary.suspectGroups > 0) {
      $("metaHash").textContent = "未启用（疑似结论 / --no-hash）";
    } else {
      $("metaHash").textContent = "—";
    }
    $("reportPathInput").value = view.reportPath || "";
  }

  function updateCounts(view) {
    const summary = view.summary || {};
    const counts = {
      all: summary.groups || 0,
      confirmed: summary.confirmedGroups || 0,
      suspect: summary.suspectGroups || 0,
      shared: summary.sharedGroups || 0,
    };
    document.querySelectorAll("#statusFilter [data-count]").forEach((node) => {
      node.textContent = counts[node.dataset.count] != null ? counts[node.dataset.count] : 0;
    });
  }

  function filteredGroups() {
    if (!state.view) return [];
    const keyword = state.search.trim().toLowerCase();
    return state.view.groups.filter((group) => {
      if (state.status !== "all" && group.status !== state.status) return false;
      if (group.savingsBytes < state.minSavings) return false;
      if (!keyword) return true;
      return group.files.some((file) => file.path.toLowerCase().includes(keyword) || file.name.toLowerCase().includes(keyword));
    });
  }

  function buildFileTable(group) {
    const keepers = group.keepers || {};
    const keepPath = keepers[state.keep];

    const table = el("div", "file-table");
    const head = el("div", "file-row head");
    head.append(el("span", "file-cell", "路径"), el("span", "file-cell", "大小"), el("span", "file-cell", "修改时间"), el("span", "file-cell", "状态"));
    table.append(head);

    group.files.forEach((file) => {
      const row = el("div", "file-row");
      row.classList.toggle("keep-row", Boolean(keepPath) && file.path === keepPath);

      const pathCell = el("div", "file-path");
      const code = el("code", null, file.path);
      code.title = file.path;
      pathCell.append(code);

      const tags = el("div", "file-tags");
      if (keepPath && file.path === keepPath) tags.append(el("span", "tag tag-keep", `保留 · ${KEEP_TEXT[state.keep]}`));
      if (file.shared) tags.append(el("span", "tag tag-shared", "硬链接"));
      if (file.unstable) tags.append(el("span", "tag tag-unstable", "不稳定"));
      if (tags.children.length > 0) pathCell.append(tags);

      const sizeCell = el("span", "file-cell", formatBytes(file.sizeOnDisk != null ? file.sizeOnDisk : file.size));
      const timeCell = el("span", "file-cell", formatTime(file.mtime));
      const statusCell = el("span", "file-cell", file.shared ? "共享" : file.unstable ? "不稳定" : "可处置");

      row.append(pathCell, sizeCell, timeCell, statusCell);
      table.append(row);
    });

    return table;
  }

  function buildGroup(group) {
    const card = el("article", "group");
    card.dataset.status = group.status;

    const head = el("button", "group-head");
    head.type = "button";
    head.setAttribute("aria-expanded", state.openGroups.has(group.id) ? "true" : "false");

    head.append(el("span", `badge badge-${group.status}`, STATUS_TEXT[group.status] || group.status));

    const main = el("span", "group-main");
    main.append(el("span", "group-title", `${group.files.length} 个文件 · 每个 ${formatBytes(group.size)}`));
    const sub = el("span", "group-sub");
    sub.append(document.createTextNode("可节省 "));
    const save = el("span", "save" + (group.savingsBytes > 0 ? "" : " zero"), formatBytes(group.savingsBytes));
    sub.append(save);
    main.append(sub);
    head.append(main);

    const metrics = el("span", "group-metrics");
    [
      ["大小", formatBytes(group.size)],
      ["可节省", formatBytes(group.savingsBytes)],
    ].forEach(([label, value]) => {
      const metric = el("span", "metric");
      metric.append(el("span", "metric-label", label));
      metric.append(el("span", "metric-value" + (label === "可节省" ? " save" : ""), value));
      metrics.append(metric);
    });
    head.append(metrics);
    head.append(el("span", "chev", "⌄"));

    const body = el("div", "group-body");
    body.hidden = !state.openGroups.has(group.id);
    body.append(buildFileTable(group));

    const keepPath = Object.prototype.hasOwnProperty.call(group.keepers || {}, state.keep) ? group.keepers[state.keep] : null;
    const note = el("p", "note");
    if (keepPath) {
      note.textContent = `按“${KEEP_TEXT[state.keep]}”策略将保留：${keepPath}（可切换上方“保留策略”）`;
    } else if (group.status === "shared") {
      note.textContent = "该组为硬链接，不占用额外空间，无需处置。";
    } else {
      note.textContent = "该组没有可移动的重复文件（硬链接或不稳定），无需处置。";
    }
    body.append(note);

    head.addEventListener("click", () => {
      const open = !state.openGroups.has(group.id);
      if (open) state.openGroups.add(group.id);
      else state.openGroups.delete(group.id);
      body.hidden = !open;
      card.classList.toggle("open", open);
      head.setAttribute("aria-expanded", open ? "true" : "false");
    });
    if (state.openGroups.has(group.id)) card.classList.add("open");

    card.append(head, body);
    return card;
  }

  function render() {
    const list = $("groupList");
    list.replaceChildren();

    if (!state.view) {
      $("toolbar").hidden = true;
      $("loadMoreWrap").hidden = true;
      $("emptyState").hidden = false;
      $("emptyState").querySelector("h3").textContent = "还没有报告";
      $("emptyState").querySelector("p").textContent = "选择盘符开始扫描，或在上方加载已有报告 JSON。";
      return;
    }

    $("toolbar").hidden = false;
    const groups = filteredGroups();
    $("resultCount").textContent = `${groups.length} 组`;

    const visible = groups.slice(0, state.visible);
    visible.forEach((group) => list.append(buildGroup(group)));

    const empty = groups.length === 0;
    $("emptyState").hidden = !empty;
    if (empty) {
      $("emptyState").querySelector("h3").textContent = "没有匹配的分组";
      $("emptyState").querySelector("p").textContent = "试试放宽状态筛选、关键词或最小可节省空间。";
    }
    $("loadMoreWrap").hidden = groups.length <= state.visible;
    $("loadMoreBtn").textContent = `加载更多（剩余 ${Math.max(0, groups.length - state.visible)} 组）`;
  }

  async function loadReport(path, options) {
    const opts = options || {};
    const target = String(path || $("reportPathInput").value || "").trim();
    if (!target) {
      toast("请填写报告 JSON 路径", true);
      return;
    }
    $("loadReportBtn").disabled = true;
    try {
      const view = await api(`/api/report?path=${encodeURIComponent(target)}`);
      const previousPath = state.view && state.view.reportPath;
      state.view = view;
      if (!opts.keepFilters && previousPath !== view.reportPath) resetFilters();
      $("reportPathInput").value = view.reportPath;
      setReportMeta(view);
      setSummary(summaryFromView(view));
      updateCounts(view);
      render();
      toast(`已加载 ${view.summary.groups} 组：确认 ${view.summary.confirmedGroups} · 疑似 ${view.summary.suspectGroups} · 硬链接 ${view.summary.sharedGroups}`);
    } catch (error) {
      toast(`加载报告失败：${error.message}`, true);
    } finally {
      $("loadReportBtn").disabled = false;
    }
  }

  // ---------- 初始化 ----------

  async function loadMeta() {
    try {
      const meta = await api("/api/meta");
      state.volumes = meta.volumes || [];
      const datalist = $("volumeList");
      datalist.replaceChildren();
      state.volumes.forEach((volume) => {
        const option = document.createElement("option");
        option.value = volume;
        datalist.append(option);
      });
      const preferred = state.volumes.find((volume) => volume.toUpperCase() !== "C:\\") || state.volumes[0];
      if (preferred) {
        $("rootInput").value = preferred;
      }
      if (meta.defaults && meta.defaults.minSize && !$("minSizeInput").value) {
        $("minSizeInput").value = meta.defaults.minSize;
      }
      $("serverPill").textContent = `dupfind ${meta.version || ""} · 只读`.replace("  ·", " ·");
      if (meta.lastReport) {
        $("reportPathInput").value = meta.lastReport;
        await loadReport(meta.lastReport, { silent: true });
      }
    } catch (error) {
      $("serverPill").textContent = "服务未就绪";
      toast(`无法连接本地服务：${error.message}`, true);
    }
  }

  function bindEvents() {
    $("scanForm").addEventListener("submit", startScan);
    $("cancelBtn").addEventListener("click", cancelScan);
    $("loadReportBtn").addEventListener("click", () => loadReport());
    $("reportPathInput").addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        loadReport();
      }
    });

    $("statusFilter").addEventListener("click", (event) => {
      const button = event.target.closest(".segment");
      if (!button) return;
      state.status = button.dataset.status;
      state.visible = PAGE_SIZE;
      document.querySelectorAll("#statusFilter .segment").forEach((node) => {
        node.classList.toggle("active", node === button);
      });
      render();
    });

    let searchTimer = null;
    $("searchInput").addEventListener("input", (event) => {
      const value = event.target.value;
      if (searchTimer) clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        state.search = value;
        state.visible = PAGE_SIZE;
        render();
      }, 200);
    });

    $("minSavingsInput").addEventListener("input", () => {
      try {
        state.minSavings = parseSize($("minSavingsInput").value);
        $("minSavingsInput").classList.remove("invalid");
      } catch (error) {
        state.minSavings = 0;
      }
      state.visible = PAGE_SIZE;
      render();
    });

    $("keepSelect").addEventListener("change", () => {
      state.keep = $("keepSelect").value;
      render();
    });

    $("loadMoreBtn").addEventListener("click", () => {
      state.visible += PAGE_SIZE;
      render();
    });
  }

  function init() {
    initTheme();
    bindEvents();
    resetSummary();
    loadMeta();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
