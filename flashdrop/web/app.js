(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const app = {
    data: null,
    refreshing: false,
    refreshQueued: false,
    pollTimer: null,
    settingsReady: false,
    lastMode: null,
    lastSendStatus: null,
    lastReceiveStatus: null,
    lastReceiveRevision: null,
    lastSendListSignature: "",
    lastRecvListSignature: "",
    helpSeenKey: "flashdrop.helpSeen",
    networkToastShown: false,
  };

  const caches = {
    send: { revision: null, items: [], total: 0, loading: false, errorFor: null, token: 0, endpoint: "/api/send/files" },
    receive: { revision: null, items: [], total: 0, loading: false, errorFor: null, token: 0, endpoint: "/api/receive/files" },
  };

  const SEND_PHASES = {
    idle: ["待选择", "neutral"],
    preparing: ["正在准备", "busy"],
    listening: ["等待连接", "busy"],
    negotiating: ["对方已连接", "busy"],
    starting: ["建立通道", "busy"],
    transferring: ["传输中", "busy"],
    finishing: ["等待保存", "busy"],
    completed: ["已完成", "good"],
    error: ["已中断", "error"],
    stopped: ["已停止", "error"],
    cancelled: ["已取消", "error"],
  };

  const RECEIVE_PHASES = {
    idle: ["未连接", "neutral"],
    connecting: ["连接中", "busy"],
    review: ["待确认", "busy"],
    preparing: ["准备接收", "busy"],
    starting: ["建立通道", "busy"],
    transferring: ["接收中", "busy"],
    verifying: ["正在保存", "busy"],
    completed: ["已完成", "good"],
    error: ["已中断", "error"],
    cancelled: ["已取消", "error"],
  };

  document.addEventListener("DOMContentLoaded", init);

  async function init() {
    bindEvents();
    try {
      const data = await api("/api/state");
      app.data = data;
      initSettings(data);
      render();
      if (!localStorage.getItem(app.helpSeenKey)) {
        localStorage.setItem(app.helpSeenKey, "1");
      }
    } catch (error) {
      showToast(`无法连接本地控制台：${error.message}`, "error", 7000);
    }
    schedulePoll();
  }

  // ------------------------------------------------------------------
  // Data / polling
  // ------------------------------------------------------------------
  async function api(path, options = {}) {
    const init = { method: options.method || "GET", headers: {} };
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    let response;
    try {
      response = await fetch(path, init);
    } catch (error) {
      throw new Error("本地控制台连接失败");
    }
    let payload = {};
    try {
      payload = await response.json();
    } catch (error) {
      if (!response.ok) throw new Error(`请求失败（HTTP ${response.status}）`);
      throw new Error("本地控制台返回了无法解析的数据");
    }
    if (!response.ok) {
      throw new Error(payload.error || `请求失败（HTTP ${response.status}）`);
    }
    return payload;
  }

  function schedulePoll() {
    if (app.pollTimer) window.clearTimeout(app.pollTimer);
    const delay = document.hidden ? 2600 : (app.data && app.data.busy ? 650 : 1200);
    app.pollTimer = window.setTimeout(() => refreshState(), delay);
  }

  async function refreshState(force = false) {
    if (app.refreshing) {
      app.refreshQueued = true;
      return;
    }
    app.refreshing = true;
    try {
      const data = await api("/api/state");
      app.data = data;
      app.networkToastShown = false;
      if (!app.settingsReady) initSettings(data);
      render();
    } catch (error) {
      if (!app.networkToastShown) {
        app.networkToastShown = true;
        showToast(`本地服务连接异常：${error.message}`, "error", 6000);
      }
    } finally {
      app.refreshing = false;
      schedulePoll();
      if (app.refreshQueued) {
        app.refreshQueued = false;
        refreshState(true);
      }
    }
  }

  function initSettings(data) {
    if (app.settingsReady) return;
    app.settingsReady = true;
    const savedPort = localStorage.getItem("flashdrop.controlPort");
    const savedConnections = localStorage.getItem("flashdrop.connections");
    const savedHost = localStorage.getItem("flashdrop.receiveHost");
    const savedReceivePort = localStorage.getItem("flashdrop.receivePort");
    $("send-port-input").value = savedPort || String(data.defaults.control_port);
    if (savedConnections && [...$("send-connections-select").options].some((o) => o.value === savedConnections)) {
      $("send-connections-select").value = savedConnections;
    } else {
      $("send-connections-select").value = String(data.defaults.connections);
    }
    $("receive-host-input").value = savedHost || data.receive.host || "";
    $("receive-port-input").value = savedReceivePort || (data.receive.port ? String(data.receive.port) : String(data.defaults.control_port));
    updateSettingsSummary();
  }

  // ------------------------------------------------------------------
  // Rendering
  // ------------------------------------------------------------------
  function render() {
    if (!app.data) return;
    const data = app.data;
    const mode = data.mode || "home";
    document.title = mode === "send" ? "发送 · 闪传 FlashDrop" : mode === "receive" ? "接收 · 闪传 FlashDrop" : "闪传 FlashDrop · 局域网点对点快传";
    $("view-home").hidden = mode !== "home";
    $("view-send").hidden = mode !== "send";
    $("view-receive").hidden = mode !== "receive";
    $("footer-version").textContent = `v${data.version || "0.1.0"}`;
    renderHeader(mode);
    if (mode === "send") renderSend(data.send);
    if (mode === "receive") renderReceive(data.receive);
    if (mode !== app.lastMode) {
      app.lastMode = mode;
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  }

  function renderHeader(mode) {
    const helpButton = $("btn-help");
    if (!helpButton) return;
    document.body.dataset.mode = mode;
  }

  function phaseInfo(table, status) {
    return table[status] || [status || "未知", "neutral"];
  }

  function paintStatusBadge(element, label, tone) {
    element.className = `status-badge tone-${tone}`;
    element.innerHTML = "";
    const dot = document.createElement("span");
    dot.setAttribute("aria-hidden", "true");
    element.append(dot, document.createTextNode(label));
  }

  function setText(id, value) {
    const element = $(id);
    if (!element) return;
    const text = value == null ? "" : String(value);
    if (element.textContent !== text) element.textContent = text;
  }

  function setHtml(id, html) {
    const element = $(id);
    if (!element) return;
    element.innerHTML = html;
  }

  function toggle(id, visible) {
    const element = $(id);
    if (element) element.hidden = !visible;
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function iconMarkup(kind) {
    if (kind === "success") {
      return '<svg viewBox="0 0 28 28" fill="none"><path d="m6.8 14.6 4.6 4.6L21.5 8.9" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    }
    if (kind === "error") {
      return '<svg viewBox="0 0 28 28" fill="none"><path d="M14 7.4v8.8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="14" cy="20.4" r="1.2" fill="currentColor"/><circle cx="14" cy="14" r="9.2" stroke="currentColor" stroke-width="1.5"/></svg>';
    }
    return '<svg viewBox="0 0 28 28" fill="none"><path d="M8 20.5h11a4 4 0 0 0 .5-7.97A6.2 6.2 0 0 0 7.8 12.1 4.2 4.2 0 0 0 8 20.5Z" stroke="currentColor" stroke-width="1.5"/><path d="M11 24h6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
  }

  function basename(path) {
    const normalized = String(path || "").replaceAll("\\", "/");
    const parts = normalized.split("/");
    return parts[parts.length - 1] || normalized;
  }

  function formatBytes(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value <= 0) return "0 B";
    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
    let number = value;
    let index = 0;
    while (number >= 1024 && index < units.length - 1) {
      number /= 1024;
      index += 1;
    }
    const digits = number >= 100 ? 0 : number >= 10 ? 1 : index === 0 ? 0 : 2;
    return `${number.toFixed(digits)} ${units[index]}`;
  }

  function formatCount(count) {
    const value = Number(count);
    if (!Number.isFinite(value)) return "0";
    return value >= 10000 ? value.toLocaleString("zh-CN") : String(value);
  }

  function formatDuration(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value) || value <= 0) return "不到 1 秒";
    if (value < 60) return `${Math.ceil(value)} 秒`;
    if (value < 3600) {
      const minutes = Math.floor(value / 60);
      const secs = Math.round(value % 60);
      return secs ? `${minutes} 分 ${secs} 秒` : `${minutes} 分钟`;
    }
    const hours = Math.floor(value / 3600);
    const minutes = Math.round((value % 3600) / 60);
    return minutes ? `${hours} 小时 ${minutes} 分` : `${hours} 小时`;
  }

  function formatEta(progress, status) {
    if (status === "finishing" || status === "verifying") return "校验保存中";
    if (!progress || progress.percent >= 100) return "即将完成";
    if (!progress.speed || progress.speed <= 0) return "估算中";
    return progress.eta > 0 ? formatDuration(progress.eta) : "即将完成";
  }

  function progressPercent(progress) {
    const raw = Number(progress && progress.percent);
    if (!Number.isFinite(raw)) return 0;
    return Math.max(0, Math.min(100, raw));
  }

  function setProgressBar(fillId, percentId, progress) {
    const percent = progressPercent(progress);
    const fill = $(fillId);
    fill.style.width = `${percent}%`;
    setText(percentId, percent >= 100 ? "100" : percent.toFixed(1));
    const track = fill.parentElement;
    if (track) {
      track.setAttribute("aria-valuenow", percent.toFixed(0));
      track.setAttribute("aria-valuetext", `${percent.toFixed(1)}%`);
    }
  }

  function setMeta(container, entries) {
    container.innerHTML = "";
    entries.filter(Boolean).forEach((entry) => {
      const span = document.createElement("span");
      span.textContent = entry;
      container.append(span);
    });
  }

  function showResult(blockId, iconId, titleId, descId, metaId, options) {
    const block = $(blockId);
    block.hidden = false;
    block.classList.toggle("error", options.tone === "error");
    setHtml(iconId, iconMarkup(options.icon));
    setText(titleId, options.title);
    setText(descId, options.desc || "");
    setMeta($(metaId), options.meta || []);
  }

  function hideAllSessionBlocks(prefix) {
    if (prefix === "send") {
      ["send-share", "send-waiting-note", "send-progress", "send-result"].forEach((id) => toggle(id, false));
    } else {
      ["receive-review", "receive-progress", "receive-result", "receive-connecting-actions"].forEach((id) => toggle(id, false));
    }
  }

  // ------------------------------------------------------------------
  // Send
  // ------------------------------------------------------------------
  function renderSend(send) {
    const status = send.status || "idle";
    const [phaseLabel, tone] = phaseInfo(SEND_PHASES, status);
    paintStatusBadge($("send-status-badge"), phaseLabel, tone);
    $("send-page-pill").textContent = status === "idle" ? "发送模式" : `发送 · ${phaseLabel}`;

    const isIdle = status === "idle";
    $("send-editor").hidden = !isIdle;
    $("send-live").hidden = isIdle;
    setText("send-editor-count", `${formatCount(send.files.count)} 个文件`);
    setText("send-file-count", `已选 ${formatCount(send.files.count)} 个文件`);
    setText("send-total-size", `共 ${formatBytes(send.files.total_size)}`);
    setText("send-live-files", formatCount(send.files.count));
    setText("send-live-size", formatBytes(send.files.total_size));

    const warning = (send.warnings || [])[0];
    const warningEl = $("send-warning");
    if (warning) {
      warningEl.hidden = false;
      warningEl.textContent = warning;
    } else {
      warningEl.hidden = true;
      warningEl.textContent = "";
    }

    if (isIdle) {
      $("btn-send-start").disabled = !send.files.count;
      $("btn-send-clear").disabled = !send.files.count;
      ensureSendList(send.files.revision);
      renderSendList();
      if (app.lastSendStatus !== null && app.lastSendStatus !== "idle") {
        hideSendLiveState();
      }
    } else {
      renderSendLive(send, status, tone);
    }
    app.lastSendStatus = status;
  }

  function hideSendLiveState() {
    $("send-live").hidden = true;
  }

  function renderSendLive(send, status, tone) {
    const share = send.listen;
    const address = share ? chooseAddress(share.addresses) : null;
    const peerConnected = Boolean(send.peer) || ["negotiating", "starting", "transferring", "finishing"].includes(status);

    const orb = $("send-orb");
    orb.className = "status-orb is-live";
    if (tone === "error") orb.classList.add("tone-error");
    if (tone === "good") orb.classList.add("tone-good");

    const peerChip = $("send-peer-chip");
    peerChip.hidden = !peerConnected;
    if (send.peer) setText("send-peer", send.peer);
    else if (peerConnected) setText("send-peer", "接收方已连接");

    hideAllSessionBlocks("send");
    $("btn-send-stop").hidden = !["preparing", "listening", "negotiating", "starting", "transferring", "finishing"].includes(status);
    $("btn-send-reset").hidden = !["completed", "error", "stopped", "cancelled"].includes(status);
    $("btn-send-reset").textContent = status === "completed" ? "发送新内容" : "调整后重试";
    $("btn-send-stop").disabled = false;

    setText("send-live-port", share ? share.port : "—");
    setText("send-live-address", address ? address.ip : "—");
    setText("send-live-resume", send.resume && send.resume.files ? `${formatCount(send.resume.files)} 个 · ${formatBytes(send.resume.bytes)}` : "无");
    if (share) {
      renderShareAddresses(share, address);
    }

    if (status === "preparing") {
      setText("send-live-kicker", "正在准备发送");
      setText("send-live-title", "读取文件指纹…");
      setText("send-live-desc", "正在检查文件大小与续传指纹，很快就好。");
      $("send-waiting-note").hidden = false;
      setText("send-waiting-text", "准备文件清单…");
      $("send-waiting-note").querySelector("small").textContent = "请保持页面开启";
    } else if (status === "listening") {
      setText("send-live-kicker", "发送通道已开启");
      setText("send-live-title", "等待接收方连接");
      setText("send-live-desc", "把下面的地址告诉对方，在另一台电脑选择「我要接收」。");
      $("send-share").hidden = false;
      $("send-waiting-note").hidden = false;
      setText("send-waiting-text", "监听中…等待对方发起连接");
      $("send-waiting-note").querySelector("small").textContent = "连接前不会传输任何文件";
    } else if (status === "negotiating") {
      setText("send-live-kicker", "接收方已接入");
      setText("send-live-title", "等待对方确认保存位置");
      setText("send-live-desc", "清单已经送达。对方确认目录后，数据通道才会打开。");
      $("send-share").hidden = false;
      $("send-waiting-note").hidden = false;
      setText("send-waiting-text", "对方正在浏览文件清单并选择保存位置…");
      $("send-waiting-note").querySelector("small").textContent = "传输尚未开始";
    } else if (status === "starting") {
      setText("send-live-kicker", "准备传输");
      setText("send-live-title", "正在建立并行数据通道");
      setText("send-live-desc", "接收方已确认，双方正在接通多条 TCP 数据连接。");
      $("send-progress").hidden = false;
      renderSendProgress(send, "starting");
    } else if (status === "transferring") {
      setText("send-live-kicker", "高速传输中");
      setText("send-live-title", "文件正在飞过去");
      setText("send-live-desc", "请保持两台电脑运行，关闭浏览器不会中断后台传输。");
      $("send-progress").hidden = false;
      renderSendProgress(send, status);
    } else if (status === "finishing") {
      setText("send-live-kicker", "数据已送达");
      setText("send-live-title", "等待对方校验并保存");
      setText("send-live-desc", "所有字节已发出，接收方正在把文件安全写入磁盘。");
      $("send-progress").hidden = false;
      renderSendProgress(send, status);
    } else if (status === "completed") {
      setText("send-live-kicker", "发送任务完成");
      setText("send-live-title", "全部送达");
      setText("send-live-desc", "接收方已确认所有文件校验并保存。");
      const completed = send.completed || {};
      showResult("send-result", "send-result-icon", "send-result-title", "send-result-desc", "send-result-meta", {
        tone: "success",
        icon: "success",
        title: "传输完成",
        desc: "对方已完成校验，文件已经保存到目标位置。",
        meta: [
          `${formatCount(completed.files || send.files.count)} 个文件`,
          formatBytes(completed.bytes || send.files.total_size),
          completed.elapsed != null ? `用时 ${formatDuration(completed.elapsed)}` : null,
          completed.resumed_bytes ? `续传 ${formatBytes(completed.resumed_bytes)}` : null,
        ],
      });
    } else if (status === "error") {
      setText("send-live-kicker", "发送已中断");
      setText("send-live-title", "这次没有传完");
      setText("send-live-desc", "对方已收到的分片会保留，重新连接并选择同一目录即可续传。");
      showResult("send-result", "send-result-icon", "send-result-title", "send-result-desc", "send-result-meta", {
        tone: "error",
        icon: "error",
        title: "传输中断",
        desc: send.error || "发送过程中连接断开。",
        meta: ["可重新开始续传", send.files.count ? `${formatCount(send.files.count)} 个文件` : null],
      });
    } else {
      const stopped = status === "cancelled";
      setText("send-live-kicker", stopped ? "对方取消了发送" : "发送已停止");
      setText("send-live-title", stopped ? "接收方取消了本次传输" : "发送服务已停止");
      setText("send-live-desc", "你可以调整文件后重新开启发送。上次已接收的分片仍可续传。");
      showResult("send-result", "send-result-icon", "send-result-title", "send-result-desc", "send-result-meta", {
        tone: "error",
        icon: "error",
        title: stopped ? "本次传输已取消" : "发送已停止",
        desc: send.error || (stopped ? "接收方取消了本次传输。" : "你已停止发送服务。"),
        meta: [],
      });
    }

    $("send-result").hidden = !["completed", "error", "stopped", "cancelled"].includes(status);
  }

  function renderShareAddresses(share, primary) {
    setText("send-primary-ip", primary ? primary.ip : "—");
    setText("send-primary-port", share.port);
    setText("send-data-port-count", String((share.data_ports || []).length));
    const addressList = $("send-address-list");
    addressList.innerHTML = "";
    const alternatives = (share.addresses || []).filter((ip) => !primary || ip !== primary.ip);
    alternatives.forEach((ip) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "address-chip";
      button.textContent = ip;
      button.title = `复制 ${ip}:${share.port}`;
      button.addEventListener("click", () => copyText(`${ip}:${share.port}`, `已复制 ${ip}:${share.port}`));
      addressList.append(button);
    });
    if (!alternatives.length) {
      const label = document.createElement("span");
      label.className = "address-chip";
      label.textContent = "无其他地址";
      addressList.append(label);
    }
    const ports = $("send-data-port-list");
    ports.innerHTML = "";
    (share.data_ports || []).forEach((port) => {
      const chip = document.createElement("span");
      chip.textContent = String(port);
      ports.append(chip);
    });
    $("btn-copy-address").disabled = !primary;
    $("btn-copy-address").dataset.copyValue = primary ? `${primary.ip}:${share.port}` : "";
  }

  function renderSendProgress(send, status) {
    const progress = send.progress || {};
    setProgressBar("send-progress-fill", "send-progress-percent", progress);
    setText("send-progress-bytes", `${formatBytes(progress.done)} / ${formatBytes(progress.total)}`);
    setText("send-speed", progress.speed > 0 ? `${formatBytes(progress.speed)}/s` : "—");
    setText("send-eta", formatEta(progress, status));
    setText("send-elapsed", formatDuration(progress.elapsed));
    setText("send-connections", `${formatCount(progress.connections || 0)} 条`);
    const phaseChip = $("send-phase-chip");
    const label = status === "starting" ? "建立通道" : status === "finishing" ? "校验保存" : "发送中";
    phaseChip.innerHTML = `<i></i>${label}`;
    const resumeLine = $("send-resume-line");
    const resumed = Number(send.resume && send.resume.bytes) || 0;
    resumeLine.hidden = !resumed;
    if (resumed) setText("send-resume-bytes", formatBytes(resumed));
  }

  function chooseAddress(addresses) {
    const list = (addresses || []).filter(Boolean);
    if (!list.length) return null;
    const nonLoopback = list.filter((ip) => !["127.0.0.1", "::1", "localhost"].includes(ip));
    const preferred =
      nonLoopback.find((ip) => ip.startsWith("192.168.")) ||
      nonLoopback.find((ip) => ip.startsWith("10.")) ||
      nonLoopback.find((ip) => !ip.startsWith("172.")) ||
      nonLoopback[0] ||
      list[0];
    return { ip: preferred };
  }

  function ensureSendList(revision) {
    if (app.data.mode !== "send" || app.data.send.status !== "idle") return;
    ensureFileList("send", revision);
  }

  function renderSendList() {
    const cache = caches.send;
    if (app.data.mode !== "send" || app.data.send.status !== "idle") return;
    $("send-file-empty").hidden = cache.items.length > 0;
    renderFileRows($("send-file-list"), cache.items, "send");
    const remaining = Math.max(0, cache.total - cache.items.length);
    $("send-load-more").hidden = remaining <= 0;
    $("send-load-more").textContent = `显示更多（还有 ${formatCount(remaining)} 个）`;
    $("send-file-count").textContent = `已选 ${formatCount(app.data.send.files.count)} 个文件`;
    $("send-total-size").textContent = `共 ${formatBytes(app.data.send.files.total_size)}`;
  }

  function renderFileRows(container, items, kind) {
    container.innerHTML = "";
    if (!items.length) return;
    const fragment = document.createDocumentFragment();
    items.forEach((item) => {
      const row = document.createElement("div");
      row.className = "file-row";

      const destinationRel = kind === "receive" ? (item.dest_rel || item.rel) : item.rel;
      const type = document.createElement("span");
      type.className = "file-type";
      const name = basename(destinationRel);
      const dot = name.lastIndexOf(".");
      type.textContent = dot > 0 && name.length > dot + 1 ? name.slice(dot + 1, dot + 5).toUpperCase() : "FILE";

      const main = document.createElement("div");
      main.className = "file-main";
      const nameEl = document.createElement("span");
      nameEl.className = "file-name";
      nameEl.title = kind === "receive" && item.renamed ? `保存名：${destinationRel}\n原名：${item.rel}` : (item.rel || name);
      const nameText = document.createElement("span");
      nameText.className = "file-name-text";
      nameText.textContent = name;
      nameEl.append(nameText);
      if (kind === "receive" && item.renamed) {
        const badge = document.createElement("span");
        badge.className = "file-name-compat";
        badge.textContent = "名称已适配";
        badge.title = `原名：${item.rel}`;
        nameEl.append(badge);
      }
      const pathEl = document.createElement("span");
      pathEl.className = "file-path";
      pathEl.textContent = destinationRel;
      pathEl.title = kind === "send"
        ? `来源：${item.path}\n发送路径：${item.rel}`
        : (item.renamed ? `保存路径：${destinationRel}\n原名：${item.rel}` : `保存路径：${destinationRel}`);
      main.append(nameEl, pathEl);

      const size = document.createElement("span");
      size.className = "file-row-size";
      size.textContent = formatBytes(item.size);

      row.append(type, main, size);
      if (kind === "send") {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "remove-file";
        remove.dataset.fileId = item.id;
        remove.title = "从发送列表移除";
        remove.setAttribute("aria-label", `移除 ${name}`);
        remove.innerHTML = '<svg viewBox="0 0 18 18" fill="none"><path d="M4.4 6h9.2M7.2 6V4.5h3.6V6m-5.2 0 .7 7.4h5.4L12.4 6M7.8 8.2v3.3m2.4-3.3v3.3" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
        row.append(remove);
      }
      fragment.append(row);
    });
    container.append(fragment);
  }

  function ensureReceiveList(revision) {
    if (app.data.mode !== "receive" || app.data.receive.status !== "review") return;
    ensureFileList("receive", revision);
  }

  function renderReceiveList() {
    if (app.data.mode !== "receive" || app.data.receive.status !== "review") return;
    const cache = caches.receive;
    const receive = app.data.receive;
    renderFileRows($("receive-file-list"), cache.items, "receive");
    setText("receive-file-summary", `${formatCount(receive.manifest.count)} 个文件 · ${formatBytes(receive.manifest.total_size)}`);
    const remaining = Math.max(0, cache.total - cache.items.length);
    $("receive-load-more").hidden = remaining <= 0;
    $("receive-load-more").textContent = `显示更多（还有 ${formatCount(remaining)} 个）`;
  }

  async function ensureFileList(kind, revision) {
    const cache = caches[kind];
    if (!Number.isFinite(Number(revision))) return;
    if (cache.loading || (cache.revision === revision && cache.errorFor !== revision) || cache.errorFor === revision) return;
    cache.loading = true;
    cache.errorFor = null;
    cache.token += 1;
    const token = cache.token;
    try {
      const data = await api(`${cache.endpoint}?offset=0&limit=120`);
      if (token !== cache.token) return;
      cache.revision = revision;
      cache.items = data.files || [];
      cache.total = data.total || 0;
      if (kind === "send") renderSendList();
      else renderReceiveList();
    } catch (error) {
      if (token === cache.token) {
        cache.errorFor = revision;
        showToast(`读取文件列表失败：${error.message}`, "error");
      }
    } finally {
      if (token === cache.token) cache.loading = false;
    }
  }

  async function loadMoreFiles(kind, button) {
    const cache = caches[kind];
    const backendRevision = kind === "send" ? app.data.send.files.revision : app.data.receive.manifest.revision;
    if (cache.loading) return;
    if (cache.revision !== backendRevision) {
      cache.revision = null;
      cache.errorFor = null;
      await ensureFileList(kind, backendRevision);
      if (kind === "send") renderSendList();
      else renderReceiveList();
      return;
    }
    await withBusy(button, "读取中…", async () => {
      const data = await api(`${cache.endpoint}?offset=${cache.items.length}&limit=120`);
      if (data.files && data.files.length) {
        cache.items = cache.items.concat(data.files);
        cache.total = data.total || cache.items.length;
      } else {
        cache.total = cache.items.length;
      }
    });
    if (kind === "send") renderSendList();
    else renderReceiveList();
  }

  // ------------------------------------------------------------------
  // Receive
  // ------------------------------------------------------------------
  function renderReceive(receive) {
    const status = receive.status || "idle";
    const [phaseLabel, tone] = phaseInfo(RECEIVE_PHASES, status);
    paintStatusBadge($("receive-status-badge"), phaseLabel, tone);
    const isIdle = status === "idle";
    $("receive-connect-panel").hidden = !isIdle;
    $("receive-live").hidden = isIdle;

    if (isIdle) {
      app.lastReceiveRevision = null;
      return;
    }

    const orb = $("receive-orb");
    orb.className = "status-orb receive-orb is-live";
    if (tone === "error") orb.classList.add("tone-error");
    if (tone === "good") orb.classList.add("tone-good");
    const peerChip = $("receive-peer-chip");
    const peerText = receive.peer ? (receive.peer.name || receive.peer.address || `${receive.host}:${receive.port}`) : "";
    peerChip.hidden = !peerText;
    if (peerText) {
      setText("receive-peer", receive.peer && receive.peer.address ? `${peerText} · ${receive.peer.address}` : peerText);
    }

    hideAllSessionBlocks("receive");
    $("btn-receive-abort").hidden = false;
    $("btn-receive-abort").disabled = false;

    setText("receive-live-files", formatCount(receive.files_count));
    setText("receive-live-sender", (receive.peer && (receive.peer.name || receive.peer.address)) || receive.host || "—");
    setText("receive-live-size", formatBytes(receive.manifest.total_size));
    setText("receive-live-save", receive.save_dir || "未选择");
    $("receive-live-save").title = receive.save_dir || "";
    setText("receive-live-resume", receive.resume && receive.resume.files ? `${formatCount(receive.resume.files)} 个 · ${formatBytes(receive.resume.bytes)}` : "无");
    setText("receive-live-state", phaseLabel);

    if (status === "connecting") {
      setText("receive-live-kicker", "正在接入");
      setText("receive-live-title", "正在连接发送方…");
      setText("receive-live-desc", "正在握手并读取对方的文件清单。");
      $("receive-connecting-actions").hidden = false;
      app.lastReceiveRevision = null;
      return;
    }

    if (status === "review") {
      setText("receive-live-kicker", "连接成功");
      setText("receive-live-title", "先确认，再开始");
      setText("receive-live-desc", "对方还没有发送文件。选择保存文件夹后，点击「开始接收」。");
      $("receive-review").hidden = false;
      renderReceiveReview(receive);
      ensureReceiveList(receive.manifest.revision);
      renderReceiveList();
      return;
    }

    if (["preparing", "starting", "transferring", "verifying"].includes(status)) {
      $("receive-progress").hidden = false;
      renderReceiveProgress(receive, status);
      if (status === "preparing") {
        setText("receive-live-kicker", "准备接收");
        setText("receive-live-title", "正在与发送方核对续传位置");
        setText("receive-live-desc", "确认从哪里继续后，数据通道才会打开。");
      } else if (status === "starting") {
        setText("receive-live-kicker", "通道即将打开");
        setText("receive-live-title", "正在建立并行数据连接");
        setText("receive-live-desc", "连接建立后，文件会直接写入你选择的文件夹。");
        setText("receive-phase-chip", "建立通道");
      } else if (status === "transferring") {
        setText("receive-live-kicker", "正在接收");
        setText("receive-live-title", "文件正在落盘");
        setText("receive-live-desc", "保持此页面开启可随时查看进度；关闭浏览器不会中断后台。");
      } else {
        setText("receive-live-kicker", "数据已收齐");
        setText("receive-live-title", "正在校验并保存");
        setText("receive-live-desc", "正在把分片安全移动到目标位置，请稍候。");
      }
      $("btn-receive-abort").hidden = status === "verifying";
      return;
    }

    if (status === "completed") {
      setText("receive-live-kicker", "接收任务完成");
      setText("receive-live-title", "文件已安全落地");
      setText("receive-live-desc", "全部文件已通过数据块校验并保存到本地。");
      const completed = receive.completed || {};
      showResult("receive-result", "receive-result-icon", "receive-result-title", "receive-result-desc", "receive-result-meta", {
        tone: "success",
        icon: "success",
        title: "接收完成",
        desc: completed.save_dir ? `文件已保存到 ${completed.save_dir}` : "文件已保存到所选目录。",
        meta: [
          `${formatCount(completed.files || receive.manifest.count)} 个文件`,
          formatBytes(completed.bytes || receive.manifest.total_size),
          receive.path_adaptation && receive.path_adaptation.renamed
            ? `${formatCount(receive.path_adaptation.renamed)} 个名称已适配`
            : null,
          completed.resumed_bytes ? `续传 ${formatBytes(completed.resumed_bytes)}` : null,
          completed.elapsed != null ? `用时 ${formatDuration(completed.elapsed)}` : null,
        ],
      });
      return;
    }

    if (status === "cancelled") {
      setText("receive-live-kicker", "接收已取消");
      setText("receive-live-title", "这次连接已结束");
      setText("receive-live-desc", "已经写入磁盘的分片会被保留，下次选择同一目录可以继续。");
      showResult("receive-result", "receive-result-icon", "receive-result-title", "receive-result-desc", "receive-result-meta", {
        tone: "error",
        icon: "error",
        title: "接收已取消",
        desc: receive.error || "你取消了本次接收。",
        meta: [receive.save_dir ? "已保留续传分片" : null],
      });
      return;
    }

    setText("receive-live-kicker", "接收已中断");
    setText("receive-live-title", "连接或保存出现问题");
    setText("receive-live-desc", "如果已经接收过一部分，重新连接并选择同一个保存文件夹即可续传。");
    showResult("receive-result", "receive-result-icon", "receive-result-title", "receive-result-desc", "receive-result-meta", {
      tone: "error",
      icon: "error",
      title: "接收中断",
      desc: receive.error || "接收过程中发生了错误。",
      meta: [receive.save_dir ? "可尝试继续续传" : null],
    });
  }

  function renderReceiveReview(receive) {
    const peerLabel = receive.peer
      ? `${receive.peer.name || "发送方"}${receive.peer.address ? ` · ${receive.peer.address}` : ""}`
      : `${receive.host || "发送方"}:${receive.port || ""}`;
    setText("receive-sender-label", peerLabel);
    setText("receive-manifest-count", formatCount(receive.manifest.count));
    setText("receive-manifest-size", formatBytes(receive.manifest.total_size));
    setText("receive-free-space", receive.free_space != null ? `可用空间 ${formatBytes(receive.free_space)}` : "");
    setText("receive-file-summary", `${formatCount(receive.manifest.count)} 个文件 · ${formatBytes(receive.manifest.total_size)}`);

    const input = $("receive-save-input");
    if (receive.save_dir && document.activeElement !== input) {
      input.value = receive.save_dir;
    } else if (!receive.save_dir && document.activeElement !== input) {
      input.value = "";
    }

    const resume = receive.resume || {};
    const notice = $("receive-resume-notice");
    if (resume.files > 0) {
      notice.hidden = false;
      notice.querySelector("p").textContent = `发现 ${formatCount(resume.files)} 个可续传文件，可以跳过 ${formatBytes(resume.bytes)}，只接收剩余部分。`;
    } else if (receive.save_dir) {
      notice.hidden = false;
      notice.querySelector("p").textContent = "这个文件夹暂无可续传分片，将从头接收；完成后会清理内部记录。";
    } else {
      notice.hidden = true;
    }

    const adaptation = receive.path_adaptation || {};
    const renamed = Number(adaptation.renamed) || 0;
    const warnings = Array.isArray(adaptation.warnings) ? adaptation.warnings : [];
    const examples = Array.isArray(adaptation.examples) ? adaptation.examples : [];
    const compatNotice = $("receive-compat-notice");
    if (renamed > 0 || warnings.length > 0) {
      compatNotice.hidden = false;
      setText(
        "receive-compat-title",
        renamed > 0 ? `已自动适配 ${formatCount(renamed)} 个 Windows 文件名` : "保存路径兼容性提示",
      );
      setText(
        "receive-compat-desc",
        renamed > 0
          ? "Windows 不接受部分 macOS 文件名。已生成稳定且唯一的保存名，文件清单显示的是实际保存路径。"
          : "",
      );
      const examplesEl = $("receive-compat-examples");
      examplesEl.innerHTML = "";
      examples.forEach((example) => {
        const li = document.createElement("li");
        li.textContent = `原名 ${example.from}  →  保存为 ${example.to}`;
        li.title = `${example.from}\n→\n${example.to}`;
        examplesEl.append(li);
      });
      const warningsEl = $("receive-compat-warnings");
      warningsEl.innerHTML = "";
      warnings.forEach((warning) => {
        const li = document.createElement("li");
        li.textContent = warning;
        warningsEl.append(li);
      });
    } else {
      compatNotice.hidden = true;
      $("receive-compat-examples").innerHTML = "";
      $("receive-compat-warnings").innerHTML = "";
    }

    const reviewError = $("receive-review-error");
    if (receive.error) {
      reviewError.hidden = false;
      reviewError.textContent = receive.error;
      delete reviewError.dataset.local;
    } else if (reviewError.dataset.local === "1") {
      // Keep a validation error visible until the user changes the folder or
      // retries, instead of erasing it on the next status poll.
    } else if (receive.save_dir && resume.free_space != null && resume.needed_space > resume.free_space) {
      reviewError.hidden = false;
      reviewError.textContent = `空间不足：还需 ${formatBytes(resume.needed_space)}，可用 ${formatBytes(resume.free_space)}。`;
    } else {
      reviewError.hidden = true;
      reviewError.textContent = "";
    }

    const start = $("btn-receive-start");
    start.disabled = !receive.can_start;
    start.title = receive.can_start ? "" : receive.save_dir ? "当前无法开始，请检查上方提示" : "请先选择保存文件夹";
    $("btn-receive-cancel").disabled = false;
  }

  function renderReceiveProgress(receive, status) {
    const progress = receive.progress || {};
    setProgressBar("receive-progress-fill", "receive-progress-percent", progress);
    setText("receive-progress-bytes", `${formatBytes(progress.done)} / ${formatBytes(progress.total)}`);
    setText("receive-speed", progress.speed > 0 ? `${formatBytes(progress.speed)}/s` : "—");
    setText("receive-eta", formatEta(progress, status));
    setText("receive-elapsed", formatDuration(progress.elapsed));
    setText("receive-connections", `${formatCount(progress.connections || 0)} 条`);
    const phaseChip = $("receive-phase-chip");
    if (status === "preparing") phaseChip.innerHTML = "<i></i>核对进度";
    else if (status === "starting") phaseChip.innerHTML = "<i></i>建立通道";
    else if (status === "verifying") phaseChip.innerHTML = "<i></i>校验保存";
    else phaseChip.innerHTML = "<i></i>接收中";
    const resumed = Number(receive.resume && receive.resume.bytes) || 0;
    $("receive-resume-line").hidden = !resumed;
    if (resumed) setText("receive-resume-bytes", formatBytes(resumed));
    setText("receive-save-chip", receive.save_dir || "未选择保存位置");
    $("receive-save-chip").title = receive.save_dir || "";
  }

  // ------------------------------------------------------------------
  // Actions
  // ------------------------------------------------------------------
  async function goMode(mode, button) {
    if (app.data && app.data.busy) {
      showToast("当前有传输任务正在进行，请先完成或停止。", "error");
      return;
    }
    await withBusy(button || null, "切换中…", async () => {
      const payload = await api("/api/mode", { method: "POST", body: { mode } });
      if (payload.state) {
        app.data = payload.state;
        render();
      } else {
        await refreshState(true);
      }
      if (mode === "receive") {
        window.setTimeout(() => $("receive-host-input").focus(), 80);
      }
    });
  }

  async function goHome(button) {
    if (app.data && app.data.busy) {
      showToast("传输正在进行中，暂时不能返回模式选择。", "error");
      return;
    }
    await withBusy(button || null, "", async () => {
      const payload = await api("/api/mode", { method: "POST", body: { mode: "home" } });
      if (payload.state) {
        app.data = payload.state;
        render();
      } else {
        await refreshState(true);
      }
    });
  }

  async function pickSendFiles(button) {
    if (!app.data || app.data.mode !== "send" || app.data.send.busy) return;
    clearSendEditorError();
    await withBusy(button, "等待选择…", async () => {
      showToast("已打开系统文件窗口，请在弹出的窗口中选择。", "info", 2600);
      const result = await api("/api/send/pick-files", { method: "POST", body: {} });
      handleAddResult(result);
      await refreshState(true);
    });
  }

  async function pickSendFolder(button) {
    if (!app.data || app.data.mode !== "send" || app.data.send.busy) return;
    clearSendEditorError();
    await withBusy(button, "等待选择…", async () => {
      showToast("已打开系统文件夹窗口，请选择要发送的文件夹。", "info", 2600);
      const result = await api("/api/send/pick-folder", { method: "POST", body: {} });
      handleAddResult(result);
      await refreshState(true);
    });
  }

  function handleAddResult(result) {
    if (!result) return;
    if (result.canceled) return;
    if (result.added > 0) {
      showToast(`已添加 ${formatCount(result.added)} 个文件`, "success");
    }
    if (result.warnings && result.warnings.length && !result.added) {
      showToast(result.warnings[0], "info", 5000);
    }
  }

  async function addManualPaths() {
    const raw = $("send-path-input").value;
    if (!raw.trim()) {
      showSendEditorError("请输入至少一个文件或文件夹路径。");
      return;
    }
    clearSendEditorError();
    const button = $("btn-path-add");
    await withBusy(button, "读取中…", async () => {
      const result = await api("/api/send/add", { method: "POST", body: { paths: raw } });
      $("send-path-input").value = "";
      handleAddResult(result);
      await refreshState(true);
    });
  }

  async function startSend(button) {
    clearSendEditorError();
    const port = Number($("send-port-input").value);
    const connections = Number($("send-connections-select").value);
    if (!Number.isInteger(port) || port < 0 || port > 65535) {
      showSendEditorError("控制端口必须是 0–65535 之间的整数。");
      return;
    }
    if (!Number.isInteger(connections) || connections < 1 || connections > 8) {
      showSendEditorError("并行连接数必须在 1–8 之间。");
      return;
    }
    try {
      localStorage.setItem("flashdrop.controlPort", String(port));
      localStorage.setItem("flashdrop.connections", String(connections));
    } catch (error) {
      /* localStorage can be unavailable in hardened browsers. */
    }
    await withBusy(button, "正在准备…", async () => {
      await api("/api/send/start", { method: "POST", body: { port, connections } });
      await refreshState(true);
    });
  }

  async function stopSend(button) {
    if (!window.confirm("确定停止发送吗？已接收的分片会保留，对方稍后可以续传。")) return;
    await withBusy(button, "正在停止…", async () => {
      await api("/api/send/stop", { method: "POST", body: {} });
      await refreshState(true);
    });
  }

  async function resetSend(button) {
    clearSendEditorError();
    await withBusy(button, "正在返回…", async () => {
      await api("/api/send/reset", { method: "POST", body: {} });
      await refreshState(true);
    });
  }

  async function removeSendFile(fileId, button) {
    if (!fileId) return;
    button.disabled = true;
    try {
      await api("/api/send/remove", { method: "POST", body: { id: fileId } });
      await refreshState(true);
    } catch (error) {
      showToast(error.message, "error");
      button.disabled = false;
    }
  }

  async function clearSendFiles(button) {
    if (!window.confirm("清空已选择的文件？")) return;
    await withBusy(button, "清空中…", async () => {
      await api("/api/send/clear", { method: "POST", body: {} });
      await refreshState(true);
    });
  }

  async function connectReceive(button) {
    clearReceiveReviewError();
    let host = $("receive-host-input").value.trim();
    let port = Number($("receive-port-input").value);
    const combined = host.match(/^([A-Za-z0-9._-]+):(\d{1,5})$/);
    if (combined) {
      host = combined[1];
      port = Number(combined[2]);
      $("receive-host-input").value = host;
      $("receive-port-input").value = String(port);
    }
    const errorEl = $("receive-connect-error");
    errorEl.hidden = true;
    if (!host) {
      errorEl.hidden = false;
      errorEl.textContent = "请填写发送方的主机地址。";
      $("receive-host-input").focus();
      return;
    }
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      errorEl.hidden = false;
      errorEl.textContent = "控制端口必须是 1–65535 之间的整数。";
      $("receive-port-input").focus();
      return;
    }
    try {
      localStorage.setItem("flashdrop.receiveHost", host);
      localStorage.setItem("flashdrop.receivePort", String(port));
    } catch (error) {
      /* ignore */
    }
    await withBusy(button, "连接中…", async () => {
      await api("/api/receive/connect", { method: "POST", body: { host, port } });
      await refreshState(true);
    });
  }

  async function chooseReceiveDir(button) {
    clearReceiveReviewError();
    await withBusy(button, "等待选择…", async () => {
      showToast("请在弹出的系统窗口中选择保存文件夹。", "info", 2600);
      const result = await api("/api/receive/choose-dir", { method: "POST", body: {} });
      if (!result.canceled) {
        $("receive-save-input").value = result.path || "";
        showToast("保存位置已确认", "success", 2200);
      }
      await refreshState(true);
    });
  }

  async function applyReceiveDir(button) {
    const path = $("receive-save-input").value.trim();
    if (!path) {
      showReceiveReviewError("请先选择或输入保存文件夹。");
      return;
    }
    clearReceiveReviewError();
    await withBusy(button, "检查中…", async () => {
      await api("/api/receive/set-dir", { method: "POST", body: { path } });
      await refreshState(true);
    });
  }

  async function startReceive(button) {
    clearReceiveReviewError();
    await withBusy(button, "正在准备…", async () => {
      await api("/api/receive/start", { method: "POST", body: {} });
      await refreshState(true);
    });
  }

  async function cancelReceive(button, withConfirm = true) {
    if (withConfirm && !window.confirm("确定取消接收吗？已写入磁盘的分片会保留，下次可继续。")) return;
    await withBusy(button, "正在取消…", async () => {
      await api("/api/receive/cancel", { method: "POST", body: {} });
      await refreshState(true);
    });
  }

  async function resetReceive(button) {
    await withBusy(button, "正在返回…", async () => {
      await api("/api/receive/reset", { method: "POST", body: {} });
      caches.receive.revision = null;
      caches.receive.items = [];
      caches.receive.total = 0;
      $("receive-connect-error").hidden = true;
      clearReceiveReviewError();
      await refreshState(true);
      window.setTimeout(() => $("receive-host-input").focus(), 70);
    });
  }

  // ------------------------------------------------------------------
  // Helpers
  // ------------------------------------------------------------------
  async function withBusy(button, label, task) {
    if (!button) {
      try {
        await task();
      } catch (error) {
        showToast(error.message, "error");
      }
      return;
    }
    if (button.dataset.busy === "1") return;
    const originalHtml = button.innerHTML;
    const originalDisabled = button.disabled;
    button.dataset.busy = "1";
    button.disabled = true;
    button.classList.add("is-busy");
    if (label) {
      button.innerHTML = `<span class="spinner" aria-hidden="true"></span><span>${escapeHtml(label)}</span>`;
    }
    try {
      await task();
    } catch (error) {
      showToast(error.message, "error");
      if (button === $("btn-receive-connect")) showReceiveConnectError(error.message);
      if (button === $("btn-send-start") || button === $("btn-path-add")) showSendEditorError(error.message);
      if (button === $("btn-receive-apply-dir") || button === $("btn-receive-start")) showReceiveReviewError(error.message);
    } finally {
      button.innerHTML = originalHtml;
      button.disabled = originalDisabled;
      button.classList.remove("is-busy");
      delete button.dataset.busy;
    }
  }

  function showToast(message, type = "info", duration = 4200) {
    if (!message) return;
    const stack = $("toast-stack");
    const toast = document.createElement("div");
    toast.className = `toast${type === "error" ? " is-error" : type === "success" ? " is-success" : ""}`;
    toast.textContent = message;
    stack.append(toast);
    window.setTimeout(() => {
      toast.classList.add("is-leaving");
      window.setTimeout(() => toast.remove(), 220);
    }, duration);
  }

  function showSendEditorError(message) {
    const element = $("send-error-inline");
    element.hidden = false;
    element.textContent = message;
  }

  function clearSendEditorError() {
    const element = $("send-error-inline");
    element.hidden = true;
    element.textContent = "";
  }

  function showReceiveReviewError(message) {
    const element = $("receive-review-error");
    element.hidden = false;
    element.textContent = message;
    element.dataset.local = "1";
  }

  function clearReceiveReviewError() {
    const element = $("receive-review-error");
    element.hidden = true;
    element.textContent = "";
    delete element.dataset.local;
  }

  function showReceiveConnectError(message) {
    const element = $("receive-connect-error");
    element.hidden = false;
    element.textContent = message;
  }

  function updateSettingsSummary() {
    const port = $("send-port-input").value.trim();
    const connections = $("send-connections-select").value;
    setText("send-settings-summary", `端口 ${port || "自动"} · ${connections} 条数据通道`);
  }

  async function copyText(text, successMessage) {
    if (!text) return;
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const area = document.createElement("textarea");
        area.value = text;
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.append(area);
        area.select();
        document.execCommand("copy");
        area.remove();
      }
      showToast(successMessage || "已复制", "success", 2200);
    } catch (error) {
      showToast("复制失败，请手动选择文本。", "error");
    }
  }

  // ------------------------------------------------------------------
  // Events
  // ------------------------------------------------------------------
  function bindEvents() {
    $("btn-mode-send").addEventListener("click", (event) => goMode("send", event.currentTarget));
    $("btn-mode-receive").addEventListener("click", (event) => goMode("receive", event.currentTarget));
    $("brand-home").addEventListener("click", () => {
      if (!app.data) return;
      if (app.data.mode === "home") {
        window.scrollTo({ top: 0, behavior: "smooth" });
      } else {
        goHome(null);
      }
    });

    $("send-back").addEventListener("click", (event) => goHome(event.currentTarget));
    $("receive-back").addEventListener("click", (event) => goHome(event.currentTarget));

    $("btn-pick-files").addEventListener("click", (event) => pickSendFiles(event.currentTarget));
    $("btn-pick-folder").addEventListener("click", (event) => pickSendFolder(event.currentTarget));
    $("btn-path-toggle").addEventListener("click", () => {
      const editor = $("send-path-editor");
      editor.hidden = !editor.hidden;
      if (!editor.hidden) $("send-path-input").focus();
    });
    $("btn-path-cancel").addEventListener("click", () => {
      $("send-path-editor").hidden = true;
    });
    $("btn-path-add").addEventListener("click", addManualPaths);
    $("btn-send-clear").addEventListener("click", (event) => clearSendFiles(event.currentTarget));
    $("send-file-list").addEventListener("click", (event) => {
      const button = event.target.closest(".remove-file");
      if (!button) return;
      event.preventDefault();
      removeSendFile(button.dataset.fileId, button);
    });
    $("send-load-more").addEventListener("click", (event) => loadMoreFiles("send", event.currentTarget));
    $("btn-send-start").addEventListener("click", (event) => startSend(event.currentTarget));
    $("btn-send-stop").addEventListener("click", (event) => stopSend(event.currentTarget));
    $("btn-send-reset").addEventListener("click", (event) => resetSend(event.currentTarget));
    $("btn-copy-address").addEventListener("click", (event) => {
      copyText(event.currentTarget.dataset.copyValue, "地址已复制");
    });
    $("send-port-input").addEventListener("input", () => {
      updateSettingsSummary();
      try {
        localStorage.setItem("flashdrop.controlPort", $("send-port-input").value);
      } catch (error) {
        /* ignore */
      }
    });
    $("send-connections-select").addEventListener("change", () => {
      updateSettingsSummary();
      try {
        localStorage.setItem("flashdrop.connections", $("send-connections-select").value);
      } catch (error) {
        /* ignore */
      }
    });

    $("receive-connect-form").addEventListener("submit", (event) => {
      event.preventDefault();
      connectReceive($("btn-receive-connect"));
    });
    $("receive-host-input").addEventListener("input", () => {
      try {
        localStorage.setItem("flashdrop.receiveHost", $("receive-host-input").value);
      } catch (error) {
        /* ignore */
      }
    });
    $("receive-port-input").addEventListener("input", () => {
      try {
        localStorage.setItem("flashdrop.receivePort", $("receive-port-input").value);
      } catch (error) {
        /* ignore */
      }
    });
    $("btn-receive-choose-dir").addEventListener("click", (event) => chooseReceiveDir(event.currentTarget));
    $("btn-receive-apply-dir").addEventListener("click", (event) => applyReceiveDir(event.currentTarget));
    $("btn-receive-start").addEventListener("click", (event) => startReceive(event.currentTarget));
    $("btn-receive-cancel").addEventListener("click", (event) => cancelReceive(event.currentTarget, true));
    $("btn-receive-connect-cancel").addEventListener("click", (event) => cancelReceive(event.currentTarget, false));
    $("btn-receive-abort").addEventListener("click", (event) => cancelReceive(event.currentTarget, true));
    $("btn-receive-reset").addEventListener("click", (event) => resetReceive(event.currentTarget));
    $("receive-load-more").addEventListener("click", (event) => loadMoreFiles("receive", event.currentTarget));

    $("btn-help").addEventListener("click", () => {
      const dialog = $("help-dialog");
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    });
    $("btn-help-close").addEventListener("click", closeHelp);
    $("btn-help-done").addEventListener("click", closeHelp);
    $("help-dialog").addEventListener("click", (event) => {
      if (event.target === event.currentTarget) closeHelp();
    });

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshState(true);
      else schedulePoll();
    });
  }

  function closeHelp() {
    const dialog = $("help-dialog");
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }
})();
