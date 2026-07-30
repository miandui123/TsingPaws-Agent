(() => {
  const CHANNEL_PATH = "/channels/tsingpaws_cloud";
  const PANEL_ID = "tp-cloud-inline";
  const STATE = { status: null, timer: null, refs: null, busy: false, observer: null, pendingSync: 0 };

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    Object.entries(attrs).forEach(([key, value]) => {
      if (key === "className") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2).toLowerCase(), value);
      else if (value !== false && value != null) node.setAttribute(key, value === true ? "" : String(value));
    });
    (Array.isArray(children) ? children : [children]).forEach((child) => {
      if (child != null) node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function isCloudRoute() {
    const path = (location.pathname || "").replace(/\/+$/, "");
    return path === CHANNEL_PATH || path.includes(CHANNEL_PATH);
  }

  async function localApi(path, method = "GET", payload) {
    const response = await fetch(path, {
      method,
      credentials: "same-origin",
      headers: { Accept: "application/json", ...(method === "GET" ? {} : { "Content-Type": "application/json" }) },
      body: method === "GET" ? undefined : JSON.stringify(payload || {}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || data.error || `HTTP ${response.status}`);
    return data;
  }

  function serviceLabel(status) {
    if (!status?.agent_running) return ["服务未启动", "red"];
    if (status.config_error) return ["需要检查", "red"];
    if (status.relay_connected && status.pico_reachable) return ["服务正常", "green"];
    if (status.relay_connecting) return ["正在连接", "orange"];
    return ["暂不可用", "gray"];
  }

  async function loadStatus() {
    try { STATE.status = await localApi("/api/tsingpaws/status"); }
    catch (_) { STATE.status = { agent_running: false, relay_connected: false, pico_reachable: false }; }
    render();
  }

  async function claimPairing() {
    const refs = STATE.refs;
    const code = (refs.code.value || "").replace(/\D/g, "");
    if (code.length !== 6 || STATE.busy) return;
    STATE.busy = true;
    refs.result.textContent = "正在绑定…";
    refs.result.className = "tp-pair-result";
    render();
    try {
      const out = await localApi("/api/tsingpaws/pairing/claim", "POST", { pairing_code: code });
      refs.code.value = "";
      refs.result.textContent = out.message || "绑定成功，已添加到 APP";
      refs.result.className = "tp-pair-result ok";
    } catch (error) {
      refs.result.textContent = error.message || "绑定失败，请重新生成绑定码";
      refs.result.className = "tp-pair-result bad";
    } finally {
      STATE.busy = false;
      render();
      loadStatus();
    }
  }

  function buildPanel() {
    const root = el("div", { id: PANEL_ID, className: "tp-cloud-inline" });
    const refs = {};
    refs.badge = el("span", { className: "tp-badge gray", text: "检查中" });
    root.appendChild(el("div", { className: "tp-cloud-hero" }, [
      el("div", { className: "tp-cloud-hero-icon", text: "T" }),
      el("div", {}, [el("h2", { text: "TsingPaws" }), el("p", { text: "连接手机 APP，随时使用您的小主机" })]),
      refs.badge,
    ]));

    refs.cloud = el("strong", { text: "检查中" });
    refs.assistant = el("strong", { text: "检查中" });
    root.appendChild(el("section", { className: "tp-customer-card" }, [
      el("h3", { text: "设备状态" }),
      el("div", { className: "tp-status-grid" }, [
        el("div", { className: "tp-status-item" }, [el("span", { text: "手机连接服务" }), refs.cloud]),
        el("div", { className: "tp-status-item" }, [el("span", { text: "本机智能助手" }), refs.assistant]),
      ]),
    ]));

    refs.code = el("input", { className: "tp-code-input", inputmode: "numeric", maxlength: "6", placeholder: "六位绑定码", autocomplete: "one-time-code" });
    refs.code.addEventListener("input", () => {
      refs.code.value = refs.code.value.replace(/\D/g, "").slice(0, 6);
      render();
    });
    refs.code.addEventListener("keydown", (event) => { if (event.key === "Enter") claimPairing(); });
    refs.bind = el("button", { className: "primary", text: "确认添加", onClick: claimPairing });
    refs.result = el("div", { className: "tp-pair-result" });
    root.appendChild(el("section", { className: "tp-customer-card" }, [
      el("h3", { text: "添加到 TsingPaws APP" }),
      el("p", { className: "tp-pair-desc", text: "请先在 TsingPaws APP 中点击“添加 TsingPaws”，然后在这里输入 APP 显示的六位绑定码。" }),
      el("div", { className: "tp-pair-row" }, [refs.code, refs.bind]),
      refs.result,
      el("p", { className: "tp-binding-rule", text: "一台小主机同时只能绑定一个 APP 账号。更换账号前，请先在原 APP 中解除绑定。" }),
    ]));
    STATE.refs = refs;
    return root;
  }

  function decorateNav() {
    const link = document.querySelector(`a[href="${CHANNEL_PATH}"], a[href="${CHANNEL_PATH}/"]`);
    if (!link) return;
    link.classList.add("tp-cloud-nav");
    let badge = link.querySelector(".tp-cloud-nav-badge");
    if (!badge) { badge = el("span", { className: "tp-cloud-nav-badge gray" }); link.appendChild(badge); }
    const [label, color] = serviceLabel(STATE.status);
    badge.textContent = label;
    badge.className = `tp-cloud-nav-badge ${color}`;
  }

  function render() {
    const refs = STATE.refs;
    if (!refs) { decorateNav(); return; }
    const status = STATE.status || {};
    const [label, color] = serviceLabel(status);
    refs.badge.textContent = label;
    refs.badge.className = `tp-badge ${color}`;
    refs.cloud.textContent = status.relay_connected ? "正常" : status.relay_connecting ? "连接中" : "未连接";
    refs.cloud.className = status.relay_connected ? "ok" : "warn";
    refs.assistant.textContent = status.pico_reachable ? "正常" : "暂不可用";
    refs.assistant.className = status.pico_reachable ? "ok" : "warn";
    refs.bind.disabled = STATE.busy || refs.code.value.length !== 6 || !status.registered;
    refs.bind.textContent = STATE.busy ? "正在添加…" : "确认添加";
    decorateNav();
  }

  function hideNativeChannelChrome(main) {
    if (!main) return;
    Array.from(main.children).forEach((child) => {
      if (child && child.id !== PANEL_ID) child.setAttribute("data-tp-hidden", "1");
    });
  }

  function scheduleSync(delay = 0) {
    if (STATE.pendingSync) clearTimeout(STATE.pendingSync);
    STATE.pendingSync = setTimeout(() => {
      STATE.pendingSync = 0;
      syncPanel();
    }, delay);
  }

  function syncPanel() {
    if (!isCloudRoute()) {
      const panel = document.getElementById(PANEL_ID);
      const host = panel?.parentElement || document.querySelector("main.tp-cloud-host");
      panel?.remove();
      if (host) {
        host.classList.remove("tp-cloud-host");
        host.querySelectorAll("[data-tp-hidden]").forEach((node) => node.removeAttribute("data-tp-hidden"));
      }
      STATE.refs = null;
      decorateNav();
      return;
    }
    const main = document.querySelector("main");
    if (!main) return;
    main.classList.add("tp-cloud-host");
    hideNativeChannelChrome(main);
    let panel = document.getElementById(PANEL_ID);
    if (!panel) {
      panel = buildPanel();
      main.prepend(panel);
    } else if (panel.parentElement !== main) {
      main.prepend(panel);
    } else if (main.firstElementChild !== panel) {
      main.prepend(panel);
    }
    hideNativeChannelChrome(main);
    render();
  }

  async function boot() {
    syncPanel();
    await loadStatus();
    STATE.timer = setInterval(loadStatus, 5000);
    window.addEventListener("popstate", () => scheduleSync(0));
    document.addEventListener("click", (event) => {
      const link = event.target?.closest?.('a[href^="/channels/"]');
      if (link) scheduleSync(50);
    }, true);
    ["pushState", "replaceState"].forEach((name) => {
      const original = history[name];
      history[name] = function (...args) { const value = original.apply(this, args); scheduleSync(50); return value; };
    });
    // Launcher SPA may render `main` after our script boots or replace it later.
    // Watch the whole document and keep re-syncing while we're on the channel route.
    if (!STATE.observer) {
      STATE.observer = new MutationObserver(() => {
        if (isCloudRoute()) scheduleSync(0);
      });
      STATE.observer.observe(document.documentElement, { childList: true, subtree: true });
    }
    // Some launcher renders settle a beat later after hard refresh.
    scheduleSync(150);
    scheduleSync(500);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
