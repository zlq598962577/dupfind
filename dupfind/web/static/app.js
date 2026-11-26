/* dupfind web v1：单页交互（原生 JS，无构建步骤） */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const PAGE_SIZE = 20;
  const POLL_INTERVAL = 700;

  const state = {
    view: null,
    status: "all",
    category: "user",
    disposal: "pending",
    search: "",
    minSavings: 0,
    keep: "shortest",
    visible: PAGE_SIZE,
    openGroups: new Set(),
    selected: new Set(),
    taskId: null,
    pollTimer: null,
    volumes: [],
    confirmThreshold: { files: 20, bytes: 1 << 30, groups: 100 },
    reportStale: false,
    disposalTaskId: null,
    disposalTimer: null,
    lastDisposal: null,
    userCategories: new Set(["image", "video", "audio", "document", "archive", "installer", "font", "other"]),
    categoryLabels: {},
    ignorePath: "",
  };

  // 报告加载请求序号：忽略/处置后的多次 reloadReport 可能乱序返回，
  // 旧响应绝不能覆盖新视图
  let loadSeq = 0;
  // 还原预览请求序号：连续点不同组的「还原此组」时，旧预览不能覆盖新确认框
  let restoreSeq = 0;
  // 从处置进度框打开还原确认框时，取消后要把进度框（含「清空隔离区」）还回去
  let restoreReturnProgress = false;

  const STATUS_TEXT = { confirmed: "确认", suspect: "疑似", shared: "硬链接" };
  const PHASE_TEXT = { scanning: "遍历中", grouping: "分组中", verifying: "内容校验中", done: "完成" };
  const KEEP_TEXT = { shortest: "最短名称", newest: "最新修改" };
  const DISPOSAL_PHASE_TEXT = { planning: "准备中", applying: "移动中", restoring: "还原中", emptying: "清空中", done: "完成" };
  const DISPOSAL_TEXT = { pending: "待处理", handled: "已处置", partial: "部分处置", ignored: "已忽略" };
  // 弹窗标题只说明「在做什么」，状态由 #disposalPhase 表达，避免完成后标题还停在「移动中」
  const DISPOSAL_TITLE_TEXT = { apply: "移入隔离区", restore: "还原", empty: "清空隔离区" };

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

  function formatDuration(ms) {
    const seconds = Math.max(0, Math.round(Number(ms) / 1000));
    if (seconds < 60) return `${seconds} 秒`;
    const minutes = Math.round(seconds / 60);
    if (minutes < 60) return `约 ${minutes} 分钟`;
    return `约 ${(minutes / 60).toFixed(1)} 小时`;
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
    state.category = "user";
    state.disposal = "pending";
    state.search = "";
    state.minSavings = 0;
    state.visible = PAGE_SIZE;
    state.openGroups.clear();
    state.selected.clear();
    $("searchInput").value = "";
    $("minSavingsInput").value = "";
    document.querySelectorAll("#statusFilter .segment").forEach((button) => {
      button.classList.toggle("active", button.dataset.status === "all");
    });
    syncSegment("#typeFilter", "category", state.category);
    syncSegment("#disposalFilter", "disposal", state.disposal);
    updateActionBar();
  }

  function syncSegment(selector, dataKey, value) {
    document.querySelectorAll(`${selector} .segment`).forEach((button) => {
      button.classList.toggle("active", button.dataset[dataKey] === value);
    });
  }

  function setReportMeta(view) {
    // 换了报告就是新的基准：上一次处置留下的「可能已过期」标记必须清掉，
    // 否则新报告一直被标成过期，这个提示就失去意义
    $("reportMeta").classList.remove("stale");
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

    const disposal = summary.disposal || {};
    const disposalCounts = {
      // 部分处置的组仍可继续处置，归入「待处理」，靠卡片徽章区分
      pending: (disposal.pending || 0) + (disposal.partial || 0),
      handled: disposal.handled || 0,
      ignored: disposal.ignored || 0,
      disposalAll: summary.groups || 0,
    };
    document.querySelectorAll("#disposalFilter [data-count]").forEach((node) => {
      const value = disposalCounts[node.dataset.count];
      node.textContent = value != null ? value : 0;
    });

    const typeCounts = { user: summary.userGroups || 0, typeAll: summary.groups || 0 };
    document.querySelectorAll("#typeFilter [data-count]").forEach((node) => {
      const value = typeCounts[node.dataset.count];
      node.textContent = value != null ? value : 0;
    });
  }

  function updateBanners(view) {
    const summary = view.summary || {};
    const hidden = summary.hiddenByTypeGroups || 0;
    if (hidden > 0 && state.category === "user") {
      $("typeBannerText").textContent = `当前只显示用户文件，已隐藏 ${hidden} 组代码 / 配置 / 程序类文件（共 ${summary.groups} 组）`;
      $("typeBanner").hidden = false;
    } else {
      $("typeBanner").hidden = true;
    }

    const disposal = summary.disposal || {};
    const handled = disposal.handled || 0;
    const partial = disposal.partial || 0;
    const ignored = disposal.ignored || 0;
    // 文本无论如何都更新：隐藏时留着旧值，下次显示会先闪一帧陈旧数字
    $("stateBannerText").textContent = `已处置 ${handled} 组 · 部分处置 ${partial} 组 · 已忽略 ${ignored} 组`;
    $("stateBanner").hidden = handled + partial + ignored === 0;
  }

  function groupMatchesCategory(group) {
    if (state.category === "all") return true;
    return state.userCategories.has(group.category);
  }

  function groupMatchesDisposal(group) {
    const value = group.disposalState || "pending";
    if (state.disposal === "all") return true;
    if (state.disposal === "pending") return value === "pending" || value === "partial";
    return value === state.disposal;
  }

  function filteredGroups() {
    if (!state.view) return [];
    const keyword = state.search.trim().toLowerCase();
    return state.view.groups.filter((group) => {
      if (state.status !== "all" && group.status !== state.status) return false;
      if (!groupMatchesCategory(group)) return false;
      if (!groupMatchesDisposal(group)) return false;
      if (group.savingsBytes < state.minSavings) return false;
      if (!keyword) return true;
      return group.files.some((file) => file.path.toLowerCase().includes(keyword) || file.name.toLowerCase().includes(keyword));
    });
  }

  function setCategory(value) {
    state.category = value;
    state.visible = PAGE_SIZE;
    syncSegment("#typeFilter", "category", value);
    pruneSelection();
    render();
  }

  function setDisposal(value) {
    state.disposal = value;
    state.visible = PAGE_SIZE;
    syncSegment("#disposalFilter", "disposal", value);
    pruneSelection();
    render();
  }

  /** 勾选当前筛选结果中所有可处置的组；limit 用于「释放最多的 100 组」。 */
  function selectFiltered(limit) {
    if (!state.view) return;
    // 先过滤再截断：已处置/已忽略/非确认组不能占用「前 N 组」的名额，
    // 否则点「释放最多的 100 组」可能只勾到 95 组甚至 0 组
    const pool = filteredGroups().filter(isDisposable);
    const targets = limit ? pool.slice(0, limit) : pool;
    let added = 0;
    targets.forEach((group) => {
      if (!state.selected.has(group.id)) {
        state.selected.add(group.id);
        added += 1;
      }
    });
    render();
    if (added === 0) {
      toast("当前筛选结果里没有可勾选的组（确认组、未处置、未忽略）");
    } else {
      toast(`已勾选 ${added} 组`);
    }
  }

  function applyMinSavingsPreset() {
    // 一键「只看大件」：设 1MB 阈值后勾选全部筛选结果，省掉「先输入再全选」两步
    $("minSavingsInput").value = "1MB";
    state.minSavings = 1024 * 1024;
    state.visible = PAGE_SIZE;
    pruneSelection();
    render();
    selectFiltered(null);
  }

  // ---------- 忽略清单 ----------

  async function reloadReport(options) {
    if (!state.view) return;
    await loadReport(state.view.reportPath, Object.assign({ silent: true, keepFilters: true }, options));
  }

  async function addIgnoredDir(path) {
    try {
      const result = await api("/api/ignore", { method: "POST", body: { path } });
      state.ignorePath = result.path || state.ignorePath;
      toast(`已忽略目录：${path}（之后的扫描也会跳过）`);
      await reloadReport();
    } catch (error) {
      toast(`忽略失败：${error.message}`, true);
    }
  }

  async function removeIgnoredDir(path) {
    try {
      const result = await api(`/api/ignore?path=${encodeURIComponent(path)}`, { method: "DELETE" });
      state.ignorePath = result.path || state.ignorePath;
      toast(`已取消忽略：${path}`);
      await reloadReport();
    } catch (error) {
      toast(`取消失败：${error.message}`, true);
    }
  }

  async function setGroupState(groupId, nextState) {
    if (!state.view) return;
    try {
      await api("/api/report/state", {
        method: "POST",
        body: { reportPath: state.view.reportPath, groupId, state: nextState },
      });
      toast(nextState === "ignored" ? `第 ${groupId} 组已忽略` : `第 ${groupId} 组已恢复为待处理`);
      await reloadReport();
    } catch (error) {
      toast(`状态更新失败：${error.message}`, true);
    }
  }

  function renderDirectoryPanel(view) {
    const panel = $("dirPanel");
    const topDirs = view.topDirs || [];
    const ignored = (view.ignore && view.ignore.directories) || [];
    if (topDirs.length === 0 && ignored.length === 0) {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;

    const list = $("dirList");
    list.replaceChildren();
    const header = el("div", "dir-row dir-head");
    header.append(el("span", "dir-name", "目录"), el("span", "dir-meta", "规模"), el("span", "dir-actions", ""));
    list.append(header);

    topDirs.forEach((entry) => {
      const row = el("div", "dir-row");
      row.append(el("span", "dir-name", entry.dir));
      const meta = el("span", "dir-meta");
      meta.append(document.createTextNode(`${entry.groups} 组 · 可节省 ${formatBytes(entry.savingsBytes)}`));
      if (entry.suggestIgnore) meta.append(el("span", "tag tag-suggest", "建议忽略"));
      row.append(meta);
      const actions = el("span", "dir-actions");
      // 盘符根不能加入忽略清单（会连整个卷一起排除），按钮直接不出现
      if (!/^[A-Za-z]:\\?$/.test(entry.path || "")) {
        actions.append(actionButton(entry.suggestIgnore ? "忽略此目录" : "忽略", () => addIgnoredDir(entry.path)));
      }
      row.append(actions);
      list.append(row);
    });

    const ignoredList = $("ignoredList");
    ignoredList.replaceChildren();
    ignoredList.hidden = ignored.length === 0;
    if (ignored.length === 0) return;
    ignoredList.append(el("h4", null, `已忽略的目录（${ignored.length}）`));
    ignored.forEach((entry) => {
      const row = el("div", "dir-row dir-ignored-row");
      row.append(el("span", "dir-name", entry.path));
      row.append(
        el(
          "span",
          "dir-meta",
          entry.groups ? `${entry.groups} 组 · 隐藏 ${formatBytes(entry.savingsBytes)}` : "当前报告中没有命中"
        )
      );
      const actions = el("span", "dir-actions");
      actions.append(actionButton("取消忽略", () => removeIgnoredDir(entry.path), "ghost-btn small"));
      row.append(actions);
      ignoredList.append(row);
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

  /** 该组是否可勾选处置：确认组、存在多个物理簇、且尚未被处置/忽略。
   *
   * 「部分处置」仍可继续勾选：组里还有剩余副本要清。
   */
  function isDisposable(group) {
    const disposalState = group.disposalState || "pending";
    if (disposalState === "handled" || disposalState === "ignored") return false;
    return group.status === "confirmed" && group.physicalCount >= 2;
  }

  function selectedGroups() {
    if (!state.view) return [];
    return state.view.groups.filter((group) => state.selected.has(group.id));
  }

  /** 过滤条件变化后，把已经看不见的组从选择里摘掉。
   *
   * 不这么做的话，用户勾选若干组、再切到别的筛选，这些卡片虽然从列表消失，
   * 但仍在 state.selected 里 —— 点「预览并处置」会把屏幕上根本看不到、也无从
   * 取消的组真的移入隔离区。宁可让用户重新勾，也不能处置看不见的东西。
   */
  function pruneSelection() {
    if (state.selected.size === 0) return;
    const visible = new Set(filteredGroups().map((group) => group.id));
    let dropped = 0;
    state.selected.forEach((id) => {
      if (!visible.has(id)) {
        state.selected.delete(id);
        dropped += 1;
      }
    });
    if (dropped > 0) toast(`已取消 ${dropped} 个不在当前筛选内的勾选`);
  }

  function selectionStats() {
    const groups = selectedGroups();
    let files = 0;
    let bytes = 0;
    groups.forEach((group) => {
      // 后端会再算一遍精确值；这里按当前保留策略下的物理簇计算移动数，
      // 不能用 files.length - 1（保留簇可能含多个硬链接路径，会偏大）
      const counts = group.removalCounts || {};
      files += counts[state.keep] != null ? counts[state.keep] : group.files.length - 1;
      bytes += group.savingsBytes;
    });
    return { groups: groups.length, files, bytes };
  }

  function updateActionBar() {
    const stats = selectionStats();
    const bar = $("actionBar");
    bar.hidden = stats.groups === 0;
    $("actionGroups").textContent = `已选 ${stats.groups} 组`;
    $("actionDetail").textContent = `预计移动 ${stats.files} 个文件 · 释放 ${formatBytes(stats.bytes)}`;
  }

  function toggleSelection(groupId, checked) {
    if (checked) state.selected.add(groupId);
    else state.selected.delete(groupId);
    updateActionBar();
  }

  function actionButton(label, handler, className) {
    const btn = el("button", className || "secondary-btn small", label);
    btn.type = "button";
    btn.addEventListener("click", (event) => {
      event.stopPropagation();
      handler();
    });
    return btn;
  }

  function buildGroup(group) {
    const card = el("article", "group");
    card.dataset.status = group.status;
    const disposalState = group.disposalState || "pending";
    card.dataset.disposal = disposalState;
    if (disposalState === "handled" || disposalState === "ignored") card.classList.add("disposed");

    const head = el("button", "group-head");
    head.type = "button";
    head.setAttribute("aria-expanded", state.openGroups.has(group.id) ? "true" : "false");

    // 勾选框：只有可处置组才有；点击不能顺带展开卡片
    if (isDisposable(group)) {
      const check = el("input", "group-check");
      check.type = "checkbox";
      check.checked = state.selected.has(group.id);
      check.setAttribute("aria-label", `选择第 ${group.id} 组`);
      check.addEventListener("click", (event) => event.stopPropagation());
      check.addEventListener("change", () => toggleSelection(group.id, check.checked));
      head.append(check);
    } else {
      const spacer = el("span", "group-check-spacer");
      head.append(spacer);
    }

    head.append(el("span", `badge badge-${group.status}`, STATUS_TEXT[group.status] || group.status));
    head.append(el("span", "tag tag-category", state.categoryLabels[group.category] || group.category));
    if (disposalState !== "pending") {
      head.append(el("span", `badge badge-${disposalState}`, DISPOSAL_TEXT[disposalState] || disposalState));
    }

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
    if (disposalState === "handled") {
      note.textContent = "该组已处置：文件已在隔离区，清空隔离区后才真正释放空间。";
    } else if (disposalState === "ignored") {
      note.textContent = "该组已被忽略，不会进入批量处置；点「恢复为待处理」可撤销。";
    } else if (keepPath) {
      note.textContent = `按“${KEEP_TEXT[state.keep]}”策略将保留：${keepPath}（可切换上方“保留策略”）`;
    } else if (group.status === "shared") {
      note.textContent = "该组为硬链接，不占用额外空间，无需处置。";
    } else {
      note.textContent = "该组没有可移动的重复文件（硬链接或不稳定），无需处置。";
    }
    body.append(note);

    if (group.disposal) {
      const record = group.disposal;
      const parts = [DISPOSAL_TEXT[disposalState] || disposalState];
      if (record.handledAt) parts.push(formatTime(record.handledAt));
      if (record.movedFiles) parts.push(`移动 ${record.movedFiles} 个文件 · ${formatBytes(record.movedBytes || 0)}`);
      body.append(el("p", "note disposal-note", `处置记录：${parts.join(" · ")}`));
    }
    if (group.ignoredFiles > 0) {
      body.append(
        el("p", "note", `另有 ${group.ignoredFiles} 个副本位于已忽略的目录，未计入可节省空间，也不会被处置。`)
      );
    }

    const actions = el("div", "group-actions");
    if (disposalState === "ignored") {
      actions.append(actionButton("恢复为待处理", () => setGroupState(group.id, "pending")));
    } else {
      // 已处置 / 部分处置的组可以精确还原这一组，不必动整个隔离区
      if (disposalState === "handled" || disposalState === "partial") {
        actions.append(actionButton("还原此组", () => confirmRestore([group.id])));
      }
      if (disposalState !== "handled") {
        actions.append(actionButton("忽略此组", () => setGroupState(group.id, "ignored")));
      }
    }
    if (actions.children.length > 0) body.append(actions);

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
      if (state.reportStale) {
        $("emptyState").querySelector("h3").textContent = "处置完成，报告已过期";
        $("emptyState").querySelector("p").textContent = "磁盘内容已变化，请重新扫描后再继续处置。";
      } else {
        $("emptyState").querySelector("h3").textContent = "还没有报告";
        $("emptyState").querySelector("p").textContent = "选择盘符开始扫描，或在上方加载已有报告 JSON。";
      }
      updateActionBar();
      return;
    }

    $("toolbar").hidden = false;
    updateBanners(state.view);
    renderDirectoryPanel(state.view);
    const groups = filteredGroups();
    $("resultCount").textContent = `${groups.length} 组`;

    const visible = groups.slice(0, state.visible);
    visible.forEach((group) => list.append(buildGroup(group)));

    const empty = groups.length === 0;
    $("emptyState").hidden = !empty;
    if (empty) {
      $("emptyState").querySelector("h3").textContent = "没有匹配的分组";
      $("emptyState").querySelector("p").textContent =
        "试试切换处置状态 / 类型、放宽结论筛选，或调低最小可节省空间。";
    }
    $("loadMoreWrap").hidden = groups.length <= state.visible;
    $("loadMoreBtn").textContent = `加载更多（剩余 ${Math.max(0, groups.length - state.visible)} 组）`;
    updateActionBar();
  }

  async function loadReport(path, options) {
    const opts = options || {};
    const target = String(path || $("reportPathInput").value || "").trim();
    if (!target) {
      toast("请填写报告 JSON 路径", true);
      return;
    }
    // 切换报告后，旧报告的还原预览/确认框必须失效，否则按钮会对着新报告执行
    restoreSeq += 1;
    if (!opts.silent && !$("modal").hidden) {
      restoreReturnProgress = false;
      closeModal();
    }
    $("loadReportBtn").disabled = true;
    const seq = ++loadSeq;
    try {
      const view = await api(`/api/report?path=${encodeURIComponent(target)}`);
      if (seq !== loadSeq) return; // 已有更新的加载请求，旧响应直接丢弃
      const previousPath = state.view && state.view.reportPath;
      state.view = view;
      state.reportStale = false;
      if (view.categoryLabels) state.categoryLabels = view.categoryLabels;
      if (Array.isArray(view.userCategories) && view.userCategories.length > 0) {
        state.userCategories = new Set(view.userCategories);
      }
      if (view.ignore && view.ignore.path) state.ignorePath = view.ignore.path;
      if (!opts.keepFilters && previousPath !== view.reportPath) resetFilters();
      $("reportPathInput").value = view.reportPath;
      setReportMeta(view);
      setSummary(summaryFromView(view));
      updateCounts(view);
      render();
      if (!opts.silent) {
        toast(
          `已加载 ${view.summary.groups} 组：确认 ${view.summary.confirmedGroups} · 疑似 ${view.summary.suspectGroups} · 硬链接 ${view.summary.sharedGroups}` +
            (view.summary.ignoredGroups ? ` · 已忽略目录隐藏 ${view.summary.ignoredGroups} 组` : "")
        );
        if (view.state && view.state.identityOk === false) {
          toast("报告已重新扫描，旧的处置记录不再适用，相关组已回到待处理", true);
        }
      }
    } catch (error) {
      if (seq === loadSeq) toast(`加载报告失败：${error.message}`, true);
    } finally {
      $("loadReportBtn").disabled = false;
    }
  }

  // ---------- 处置 ----------

  function trashForView() {
    // 每次都从当前报告的根路径现算，不能缓存：换一份别的盘的报告后若还沿用
    // 上一次的隔离区，跨卷移动会让每个文件都被跳过，用户看到「处置完成、0 个移动」。
    const root = (state.view && state.view.root) || $("rootInput").value || "";
    const match = /^([A-Za-z]:)/.exec(root);
    return match ? `${match[1].toUpperCase()}\\_dupfind_trash` : "";
  }

  function openModal(title, bodyNodes, footNodes) {
    $("modalTitle").textContent = title;
    const body = $("modalBody");
    body.replaceChildren();
    (bodyNodes || []).forEach((node) => body.append(node));
    const foot = $("modalFoot");
    foot.replaceChildren();
    (footNodes || []).forEach((node) => foot.append(node));
    $("modal").hidden = false;
  }

  function closeModal() {
    $("modal").hidden = true;
    if (restoreReturnProgress) {
      restoreReturnProgress = false;
      $("progressModal").hidden = false;
    }
  }

  function previewNode(label, value) {
    const row = el("div", "preview-row");
    row.append(el("span", "preview-label", label));
    row.append(el("span", "preview-value", value));
    return row;
  }

  function previewTable(title, rows, keyName) {
    const wrap = el("div", "preview-block");
    wrap.append(el("h3", null, title));
    if (!rows || rows.length === 0) {
      wrap.append(el("p", "hint", "无"));
      return wrap;
    }
    const table = el("div", "preview-table");
    rows.forEach((row) => {
      const line = el("div", "preview-line");
      line.append(el("span", "preview-key", String(row[keyName])));
      line.append(el("span", "preview-num", `${row.files} 个 · ${row.text}`));
      table.append(line);
    });
    wrap.append(table);
    return wrap;
  }

  async function previewDisposal() {
    if (!state.view) return;
    const ids = Array.from(state.selected);
    if (ids.length === 0) return;
    $("actionPreviewBtn").disabled = true;
    try {
      const preview = await api("/api/apply/preview", {
        method: "POST",
        body: {
          reportPath: state.view.reportPath,
          groupIds: ids,
          keep: state.keep,
          trash: trashForView(),
          minSavings: $("minSavingsInput").value.trim() || "0",
          verifyContent: $("verifyContentInput").checked,
        },
      });
      showPreview(preview, ids);
    } catch (error) {
      toast(`无法生成预览：${error.message}`, true);
    } finally {
      $("actionPreviewBtn").disabled = false;
    }
  }

  function showPreview(preview, ids) {
    const nodes = [];
    const summary = el("div", "preview-summary");
    summary.append(el("strong", null, `${preview.groupCount} 组 · ${preview.fileCount} 个文件`));
    summary.append(el("span", "preview-big", `释放 ${preview.freedText}`));
    nodes.push(summary);

    nodes.push(previewNode("保留策略", KEEP_TEXT[preview.keepMode] || preview.keepMode));
    nodes.push(previewNode("隔离区", preview.trash));
    nodes.push(previewNode("说明", "文件先移入隔离区，磁盘空间要清空隔离区后才释放"));
    if (preview.estimatedSeconds > 0) {
      nodes.push(previewNode("预计耗时", formatDuration(preview.estimatedSeconds * 1000)));
    }

    if (preview.missingFiles > 0) {
      nodes.push(
        el(
          "p",
          "hint warn-text",
          `有 ${preview.missingFiles} 个待移动文件已不在磁盘上（可能被其他程序移动或删除），它们会被跳过，不影响其余文件。`
        )
      );
    }

    if (preview.skippedByReason && preview.skippedByReason.length > 0) {
      const block = el("div", "preview-block");
      block.append(el("h3", null, "未纳入处置的组"));
      const table = el("div", "preview-table");
      preview.skippedByReason.forEach((item) => {
        const line = el("div", "preview-line");
        line.append(el("span", "preview-key", item.reason));
        line.append(el("span", "preview-num", `${item.groups} 组`));
        table.append(line);
      });
      block.append(table);
      nodes.push(block);
    }

    nodes.push(previewTable("按目录", preview.byDirectory, "directory"));
    nodes.push(previewTable("按扩展名", preview.byExtension, "extension"));

    if (preview.skipped && preview.skipped.length > 0) {
      const block = el("div", "preview-block");
      block.append(el("h3", null, `未纳入处置的组（${preview.skipped.length}）`));
      const table = el("div", "preview-table");
      preview.skipped.slice(0, 12).forEach((item) => {
        const line = el("div", "preview-line");
        line.append(el("span", "preview-key", `#${item.groupId}`));
        line.append(el("span", "preview-num", item.reason));
        table.append(line);
      });
      if (preview.skipped.length > 12) {
        table.append(el("p", "hint", `…… 另有 ${preview.skipped.length - 12} 组`));
      }
      block.append(table);
      nodes.push(block);
    }

    const foot = [];
    const cancel = el("button", "ghost-btn", "取消");
    cancel.type = "button";
    cancel.addEventListener("click", closeModal);
    foot.push(cancel);

    if (preview.fileCount === 0) {
      const empty = el("p", "hint", "没有可处置的文件。");
      nodes.push(empty);
      openModal("预览", nodes, foot);
      return;
    }

    if (preview.requiresConfirmation) {
      const confirmRow = el("label", "confirm-row");
      confirmRow.append(el("span", null, `本次规模较大，请输入 DELETE 确认：`));
      const input = document.createElement("input");
      input.id = "confirmInput";
      input.autocomplete = "off";
      input.placeholder = "DELETE";
      confirmRow.append(input);
      nodes.push(confirmRow);
      foot.push(
        (() => {
          const btn = el("button", "primary-btn danger", "移入隔离区");
          btn.type = "button";
          btn.disabled = true;
          input.addEventListener("input", () => {
            btn.disabled = input.value.trim() !== "DELETE";
          });
          btn.addEventListener("click", () => startDisposal(ids, true));
          return btn;
        })()
      );
      openModal("确认处置", nodes, foot);
      input.focus();
      return;
    }

    const btn = el("button", "primary-btn danger", "移入隔离区");
    btn.type = "button";
    btn.addEventListener("click", () => startDisposal(ids, false));
    foot.push(btn);
    openModal("确认处置", nodes, foot);
  }

  async function startDisposal(ids, confirm) {
    if (!state.view) return;
    closeModal();
    showProgressModal("apply");
    try {
      const task = await api("/api/apply", {
        method: "POST",
        body: {
          reportPath: state.view.reportPath,
          groupIds: ids,
          keep: state.keep,
          trash: trashForView(),
          minSavings: $("minSavingsInput").value.trim() || "0",
          verifyContent: $("verifyContentInput").checked,
          confirm,
        },
      });
      state.disposalTaskId = task.id;
      renderDisposalProgress(task);
      pollDisposal(task.id);
    } catch (error) {
      $("progressModal").hidden = true;
      toast(`处置失败：${error.message}`, true);
    }
  }

  function showProgressModal(kind) {
    // 上一次任务若还在轮询（例如刚点完「还原」又点「清空」），它的回调会把
    // 新任务的进度覆盖成旧状态，所以先停掉旧定时器
    if (state.disposalTimer) {
      clearTimeout(state.disposalTimer);
      state.disposalTimer = null;
    }
    state.disposalTaskId = null;
    $("progressTitle").textContent = DISPOSAL_TITLE_TEXT[kind] || "处置中";
    $("disposalPhase").textContent = "准备中";
    $("disposalDetail").textContent = "0 / 0 组";
    $("disposalBar").firstElementChild.style.width = "0%";
    $("disposalStats").replaceChildren();
    $("progressFoot").replaceChildren();
    if (kind === "apply") {
      const stop = el("button", "secondary-btn", "停止");
      stop.type = "button";
      stop.id = "stopDisposalBtn";
      stop.addEventListener("click", cancelDisposal);
      $("progressFoot").append(stop);
    }
    $("progressModal").hidden = false;
  }

  async function cancelDisposal() {
    const taskId = state.disposalTaskId;
    const button = $("stopDisposalBtn");
    if (!taskId) return;
    if (button) {
      button.disabled = true;
      button.textContent = "停止中…";
    }
    try {
      const result = await api(`/api/disposal/${encodeURIComponent(taskId)}/cancel`, { method: "POST" });
      if (result.cancelled) {
        $("disposalPhase").textContent = "正在停止…（当前组完成后生效）";
      } else {
        toast("任务已经结束，无需停止");
      }
    } catch (error) {
      if (button) {
        button.disabled = false;
        button.textContent = "停止";
      }
      toast(`停止失败：${error.message}`, true);
    }
  }

  function renderDisposalProgress(task) {
    $("disposalPhase").textContent = DISPOSAL_PHASE_TEXT[task.phase] || task.phase || "处理中";
    // 只有 apply 有逐组进度；还原/清空是按 manifest 整体执行，显示「0 / 0 组」只会误导
    const isApply = task.kind === "apply";
    const total = task.totalGroups || 0;
    const done = task.processedGroups || 0;
    $("disposalDetail").textContent = isApply ? `${done} / ${total} 组` : "";
    const percent = !isApply || total === 0 ? (task.state === "running" ? 0 : 100)
      : Math.min(100, Math.round((done / total) * 100));
    $("disposalBar").firstElementChild.style.width = `${percent}%`;

    const stats = $("disposalStats");
    stats.replaceChildren();
    if (!isApply) {
      if (task.kind === "restore") {
        if (task.state === "running") {
          stats.append(el("span", "hint", "正在按 manifest 还原文件…"));
          return;
        }
        const moved = Math.max(0, (task.restoredFiles || 0) - (task.alreadyRestored || 0));
        [
          ["已还原", `${moved} 个`],
          ["已在原处", String(task.alreadyRestored || 0)],
          ["总大小", task.restoredText || "0 B"],
          ["缺失", String(task.missingFiles || 0)],
          ["失败", String(task.failedFiles || 0)],
        ].forEach(([label, value]) => {
          const node = el("span");
          node.append(document.createTextNode(`${label} `));
          node.append(el("b", null, value));
          stats.append(node);
        });
        if (task.stateSynced === false) {
          stats.append(
            el("p", "hint warn-text", "处置状态未能同步：页面可能仍把这些组显示为已处置。")
          );
        }
      }
      return;
    }
    [
      ["已移动", `${task.movedFiles} 个`],
      ["释放", task.movedText || "0 B"],
      ["跳过", String(task.skippedFiles || 0)],
      ["失败", String(task.failedFiles || 0)],
    ].forEach(([label, value]) => {
      const node = el("span");
      node.append(document.createTextNode(`${label} `));
      const bold = el("b", null, value);
      node.append(bold);
      stats.append(node);
    });
    if (isApply && task.state === "running" && done > 0 && total > done && task.durationMs > 0) {
      // 用已完成的平均速度外推；组间大小差异会让它不准，但总比「0/500 组」没有预期好
      const remaining = Math.round((task.durationMs / done) * (total - done));
      stats.append(el("span", "hint", `预计剩余 ${formatDuration(remaining)}`));
    }
  }

  function pollDisposal(taskId, attempt) {
    const tries = attempt || 0;
    state.disposalTimer = setTimeout(async () => {
      // 期间可能已经开始了新任务；旧回调直接退出，别把新进度覆盖掉
      if (state.disposalTaskId !== taskId) return;
      try {
        const task = await api(`/api/disposal/${encodeURIComponent(taskId)}`);
        if (state.disposalTaskId !== taskId) return;
        renderDisposalProgress(task);
        if (task.state === "running") {
          pollDisposal(taskId, 0);
          return;
        }
        await finishDisposal(task);
      } catch (error) {
        // 服务端任务很可能还在跑：不能立刻关掉进度框、丢掉任务 id，否则用户
        // 以为失败、再点一次就会对同一隔离区并发启动第二个 apply（后端现在会
        // 用隔离区锁挡住，但前端也不该主动制造这种情况）。
        if (tries >= 10) {
          $("disposalPhase").textContent = "进度未知";
          const note = el(
            "p",
            "hint error-text",
            `无法获取任务进度（${error.message}）。任务可能仍在后台执行，请稍后刷新页面查看。`,
          );
          $("disposalStats").append(note);
          const closeBtn = el("button", "secondary-btn", "关闭");
          closeBtn.type = "button";
          closeBtn.addEventListener("click", () => {
            $("progressModal").hidden = true;
          });
          $("progressFoot").replaceChildren(closeBtn);
          state.disposalTaskId = null;
          return;
        }
        toast(`处置进度查询失败，正在重试…`, true);
        pollDisposal(taskId, tries + 1);
      }
    }, POLL_INTERVAL);
  }

  async function finishDisposal(task) {
    state.disposalTaskId = null;
    state.lastDisposal = task;
    const foot = $("progressFoot");
    foot.replaceChildren();
    const movedAnything = task.movedFiles > 0;
    if (task.state === "failed") {
      $("disposalPhase").textContent = "失败";
      $("disposalStats").append(el("p", "hint error-text", task.error || task.message || "未知错误"));
    } else {
      $("disposalPhase").textContent = task.state === "cancelled" ? "已停止" : task.message || "完成";
      $("disposalBar").firstElementChild.style.width = "100%";
    }

    if (task.kind === "apply" && !task.stateWritten) {
      $("disposalStats").append(
        el(
          "p",
          "hint error-text",
          "处置状态未能写入磁盘：下次加载报告时这些组可能仍显示为待处理，可在卡片上手动标记「已忽略」修正。"
        )
      );
    }

    const close = el("button", "secondary-btn", "关闭");
    close.type = "button";
    close.addEventListener("click", () => {
      $("progressModal").hidden = true;
    });
    foot.append(close);

    // 只要有文件进了隔离区就提供恢复入口 —— 包括「部分失败」的任务。
    // 条件写成 state !== failed 会让「移动 2 个、失败 1 个」的任务失去还原入口，
    // 已移动的文件只能靠 CLI 捞回来。
    if (movedAnything) {
      // 只还原本任务实际处置过的组；包含该组更早批次的文件（组级语义），
      // 确认框会把实际文件数列出来。
      const restoreIds = (task.handledGroupIds || []).concat(task.partialGroupIds || []);
      const restore = el("button", "secondary-btn", "还原本次处置");
      restore.type = "button";
      restore.addEventListener("click", () => confirmRestore(restoreIds.length ? restoreIds : null));
      foot.append(restore);

      const purge = el("button", "primary-btn danger", "清空隔离区");
      purge.type = "button";
      purge.addEventListener("click", () => confirmEmpty(task.trash));
      foot.append(purge);
    }

    if (task.kind === "apply") {
      if (movedAnything) toast(`已移动 ${task.movedFiles} 个文件到隔离区（清空后才释放空间）`);
      state.selected.clear();
      // 报告不再「过期」：处置结果已写进 <报告>.state.json / manifest，
      // 重新加载后这些组会带着处置记录出现，而不是又重新冒出来。
      if (state.view) {
        await reloadReport({ keepFilters: true });
        if (task.remainingGroupIds && task.remainingGroupIds.length > 0) {
          toast(`${task.remainingGroupIds.length} 组仍未处理，可继续勾选处置`);
        }
      }
      return;
    }

    if (task.kind === "restore") {
      // 还原会撤销对应组的「已处置」标记（sync_after_restore），失败也可能是
      // 部分成功，两种情况都要刷新视图
      if (state.view) await reloadReport({ keepFilters: true });
      // 失败原因已经写在弹窗里；不能再提示「已还原」，否则用户会以为文件
      // 回到了原位，实际还在隔离区
      if (task.state === "failed") return;
      const moved = Math.max(0, (task.restoredFiles || 0) - (task.alreadyRestored || 0));
      const parts = [`已还原 ${moved} 个文件`];
      if (task.alreadyRestored) parts.push(`其中 ${task.alreadyRestored} 个已在原处`);
      parts.push(task.restoredText || "0 B");
      if (task.missingFiles) parts.push(`${task.missingFiles} 个缺失`);
      toast(parts.join(" · "));
      return;
    }

    if (task.state === "failed") return;
    toast(task.message || "隔离区已清空");
    state.selected.clear();
    render();
    updateActionBar();
  }

  /** 还原预览 + 确认框。groupIds 为 null 时作用于本报告全部已处置组。 */
  async function confirmRestore(groupIds) {
    if (!state.view) return;
    const seq = ++restoreSeq;
    const reportPath = state.view.reportPath;
    const params = new URLSearchParams({ reportPath });
    const trash = trashForView();
    if (trash) params.set("trash", trash);
    (groupIds || []).forEach((id) => params.append("groupIds", String(id)));
    let preview;
    try {
      preview = await api(`/api/restore/preview?${params.toString()}`);
    } catch (error) {
      if (seq !== restoreSeq) return;
      toast(`无法预览还原：${error.message}`, true);
      return;
    }
    if (seq !== restoreSeq) return;

    const unsafe = new Set(preview.unsafeGroupIds || []);
    const requested = groupIds || null;
    const previewGroups = Array.isArray(preview.groupIds) ? preview.groupIds : [];
    // 无论组级还是报告级，执行都只发「预览里真正统计过、且不是 unsafe」的
    // 组：报告级若留空 groupIds，服务端会用另一个隔离区重新推导状态，可能
    // 还原预览从未展示过的批次。
    const baseIds = requested || previewGroups;
    const effectiveIds = baseIds.filter((id) => !unsafe.has(id));
    const allRequestedUnsafe = baseIds.length > 0 && effectiveIds.length === 0;
    const totalAlready = preview.totalAlready || 0;
    const totalRejected = preview.totalRejected || 0;
    const usable = (preview.trashes || []).filter(
      (item) => item.exists && (item.files > 0 || item.alreadyRestored > 0)
    );
    const detailBuckets = (preview.trashes || []).filter(
      (item) =>
        item.exists &&
        (item.files > 0 ||
          item.alreadyRestored > 0 ||
          item.missing > 0 ||
          item.conflicts > 0 ||
          item.rejected > 0 ||
          item.skippedPending > 0 ||
          item.unreadable > 0)
    );

    const nodes = [];
    const scopeText = requested && requested.length ? `选中的 ${requested.length} 组` : "本报告全部已处置组";
    nodes.push(previewNode("范围", scopeText));
    nodes.push(
      previewNode(
        "将还原",
        preview.totalFiles > 0 ? `${preview.totalFiles} 个文件 · ${preview.totalText}` : "0 个文件"
      )
    );
    if (totalAlready > 0) {
      nodes.push(previewNode("已在原处", `${totalAlready} 个（会写回记录并把组恢复为待处理）`));
    }

    detailBuckets.forEach((item) => {
      nodes.push(previewNode("隔离区", item.path));
      const details = [];
      if (item.alreadyRestored) details.push(`${item.alreadyRestored} 个已在原处`);
      if (item.missing) details.push(`${item.missing} 个缺失`);
      if (item.conflicts) details.push(`${item.conflicts} 个原位置已有文件（默认跳过）`);
      if (item.skippedPending) details.push(`${item.skippedPending} 个未实际移动`);
      if (item.rejected) details.push(`${item.rejected} 个记录不合法（会被拒绝）`);
      if (item.unreadable) details.push(`${item.unreadable} 份记录无法读取`);
      if (details.length) nodes.push(el("p", "hint", `另有：${details.join(" · ")}。`));
    });

    if (preview.unscopedManifests) {
      nodes.push(
        el(
          "p",
          "hint warn-text",
          `隔离区里还有 ${preview.unscopedManifests} 份记录无法归属到本报告（旧版本、相对路径或仅 journal），Web 不会动它们；可用 dupfind restore <隔离区> --report <本报告> 或整区还原。`
        )
      );
    }
    if (preview.totalOtherReportManifests) {
      nodes.push(
        el(
          "p",
          "hint",
          `隔离区里有 ${preview.totalOtherReportManifests} 份其他报告的批次，不受本次还原影响。`
        )
      );
    }
    if (totalRejected) {
      nodes.push(
        el("p", "hint warn-text", `有 ${totalRejected} 个动作记录不合法（路径越界/镜像不符），已被排除。`)
      );
    }
    if (preview.staleGroupIds && preview.staleGroupIds.length) {
      nodes.push(
        el(
          "p",
          "hint warn-text",
          `状态里有 ${preview.staleGroupIds.length} 个组号不在当前报告中（报告可能已重新扫描），已忽略。`
        )
      );
    }
    if (unsafe.size > 0) {
      let detail;
      if (!requested) {
        detail = `第 ${[...unsafe].join("、")} 组已从本次还原中排除。`;
      } else if (allRequestedUnsafe) {
        detail = "选中的组全部被排除，没有可还原的组。";
      } else {
        detail = `已从本次还原中排除 ${[...unsafe].join("、")} 组。`;
      }
      nodes.push(
        el("p", "hint error-text", `隔离区记录与当前报告不一致（报告可能已重新扫描）：${detail}`)
      );
    }
    if (preview.crossTrashGroupIds && preview.crossTrashGroupIds.length) {
      nodes.push(
        el(
          "p",
          "hint error-text",
          `第 ${preview.crossTrashGroupIds.join("、")} 组分布在多个隔离区；Web 暂不支持跨隔离区合并还原，请用 CLI 分别还原。`
        )
      );
    }
    if (preview.truncated) {
      nodes.push(
        el(
          "p",
          "hint error-text",
          "隔离区记录数量或体积超出 Web 预览上限，无法安全统计；请用 CLI 整区还原。"
        )
      );
    }
    if (preview.totalUnreadable) {
      nodes.push(
        el("p", "hint warn-text", `另有 ${preview.totalUnreadable} 份隔离区记录无法读取，未计入统计。`)
      );
    }
    if (preview.missingGroupIds && preview.missingGroupIds.length) {
      nodes.push(el("p", "hint", `第 ${preview.missingGroupIds.join("、")} 组在隔离区里没有找到相关记录。`));
    }

    const foot = [];
    const cancel = el("button", "ghost-btn", "取消");
    cancel.type = "button";
    cancel.addEventListener("click", closeModal);
    foot.push(cancel);

    if (usable.length === 0) {
      nodes.push(el("p", "hint", "没有可还原的文件。"));
    } else if (usable.length > 1) {
      nodes.push(
        el("p", "hint error-text", "检测到多个隔离区都有可还原文件，Web 一次只能还原一个；请用 CLI 分别还原。")
      );
    }
    const blocked =
      usable.length !== 1 ||
      (preview.crossTrashGroupIds || []).length > 0 ||
      allRequestedUnsafe ||
      Boolean(preview.truncated);
    const btn = el("button", "primary-btn", "还原");
    btn.type = "button";
    btn.disabled =
      blocked ||
      effectiveIds.length === 0 ||
      (preview.totalFiles === 0 && totalAlready === 0);
    btn.addEventListener("click", () => startRestore(usable[0].path, effectiveIds, reportPath));
    foot.push(btn);
    // 隐藏进度框放到确认框真正打开时：预览失败或取消不能把进度框弄丢
    restoreReturnProgress = !$("progressModal").hidden;
    $("progressModal").hidden = true;
    openModal("确认还原", nodes, foot);
  }

  async function startRestore(trash, groupIds, reportPath) {
    if (!state.view || state.view.reportPath !== reportPath) {
      closeModal();
      toast("报告已切换，请重新打开还原预览", true);
      return;
    }
    if (!groupIds || groupIds.length === 0) {
      closeModal();
      toast("没有可还原的组", true);
      return;
    }
    closeModal();
    showProgressModal("restore");
    try {
      // 报告级也显式带 groupIds：执行范围必须与刚才的预览完全一致
      const body = { trash, reportPath, groupIds, dryRun: false };
      const task = await api("/api/restore", { method: "POST", body });
      state.disposalTaskId = task.id;
      renderDisposalProgress(task);
      pollDisposal(task.id);
    } catch (error) {
      $("progressModal").hidden = true;
      toast(`还原失败：${error.message}`, true);
    }
  }

  function confirmEmpty(trash) {
    // 「清空隔离区」按钮长在进度弹窗里；不先关掉它，确认框会被进度弹窗的
    // 全屏遮罩盖住（两者同 z-index，进度弹窗在 DOM 里更靠后），用户看不到
    // 输入框、点不到「永久删除」，看起来就像按钮失灵。
    $("progressModal").hidden = true;
    const nodes = [
      el("p", null, "清空隔离区会永久删除其中全部文件，不可恢复。"),
      previewNode("隔离区", trash),
    ];
    const input = document.createElement("input");
    input.id = "emptyConfirmInput";
    input.autocomplete = "off";
    input.placeholder = "DELETE";
    const row = el("label", "confirm-row");
    row.append(el("span", null, "输入 DELETE 确认："));
    row.append(input);
    nodes.push(row);

    const cancel = el("button", "ghost-btn", "取消");
    cancel.type = "button";
    cancel.addEventListener("click", closeModal);
    const btn = el("button", "primary-btn danger", "永久删除");
    btn.type = "button";
    btn.disabled = true;
    input.addEventListener("input", () => {
      btn.disabled = input.value.trim() !== "DELETE";
    });
    btn.addEventListener("click", async () => {
      closeModal();
      showProgressModal("empty");
      try {
        const task = await api("/api/empty", { method: "POST", body: { trash, confirm: true } });
        state.disposalTaskId = task.id;
        renderDisposalProgress(task);
        pollDisposal(task.id);
      } catch (error) {
        $("progressModal").hidden = true;
        toast(`清空失败：${error.message}`, true);
      }
    });
    openModal("清空隔离区", nodes, [cancel, btn]);
    input.focus();
  }

  function selectVisible() {
    const groups = filteredGroups().slice(0, state.visible);
    let added = 0;
    groups.forEach((group) => {
      if (isDisposable(group) && !state.selected.has(group.id)) {
        state.selected.add(group.id);
        added += 1;
      }
    });
    render();
    if (added === 0) toast("本页没有可勾选的确认组");
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
      if (meta.confirmThreshold) state.confirmThreshold = meta.confirmThreshold;
      if (meta.ignorePath) state.ignorePath = meta.ignorePath;
      $("serverPill").textContent = `dupfind ${meta.version || ""} · 本地`.replace("  ·", " ·");
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
      pruneSelection();
      render();
    });

    $("disposalFilter").addEventListener("click", (event) => {
      const button = event.target.closest(".segment");
      if (button) setDisposal(button.dataset.disposal);
    });

    $("typeFilter").addEventListener("click", (event) => {
      const button = event.target.closest(".segment");
      if (button) setCategory(button.dataset.category);
    });

    $("typeShowAllBtn").addEventListener("click", () => setCategory("all"));
    $("stateShowHandledBtn").addEventListener("click", () => setDisposal("handled"));
    $("stateShowIgnoredBtn").addEventListener("click", () => setDisposal("ignored"));
    $("stateRestoreBtn").addEventListener("click", () => confirmRestore(null));

    let searchTimer = null;
    $("searchInput").addEventListener("input", (event) => {
      const value = event.target.value;
      if (searchTimer) clearTimeout(searchTimer);
      searchTimer = setTimeout(() => {
        state.search = value;
        state.visible = PAGE_SIZE;
        pruneSelection();
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
      pruneSelection();
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

    $("selectPageBtn").addEventListener("click", selectVisible);
    $("selectAllBtn").addEventListener("click", () => selectFiltered(null));
    $("selectTop100Btn").addEventListener("click", () => selectFiltered(100));
    $("minSavingsPresetBtn").addEventListener("click", applyMinSavingsPreset);
    $("clearSelectionBtn").addEventListener("click", () => {
      state.selected.clear();
      render();
      updateActionBar();
    });
    $("actionPreviewBtn").addEventListener("click", previewDisposal);
    $("actionCancelBtn").addEventListener("click", () => {
      state.selected.clear();
      render();
      updateActionBar();
    });
    $("modalCloseBtn").addEventListener("click", closeModal);
    $("modalBackdrop").addEventListener("click", closeModal);
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (!$("modal").hidden) closeModal();
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
