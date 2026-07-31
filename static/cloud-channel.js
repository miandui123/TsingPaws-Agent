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
    if (!status.binding_known) return ["确认状态", "gray"];
    if (!status.bound) return ["等待绑定", "purple"];
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
    if (!STATE.status?.pairing_enabled) {
      refs.result.textContent = STATE.status?.pairing_disabled_reason || "当前无法绑定";
      refs.result.className = "tp-pair-result bad";
      return;
    }
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
    root.appendChild(el("header", { className: "tp-cloud-hero" }, [
      el("div", { className: "tp-cloud-hero-icon", text: "T" }),
      el("div", { className: "tp-cloud-brand" }, [
        el("span", { className: "tp-eyebrow", text: "TSINGPAWS CONNECT" }),
        el("h2", { text: "连接您的 TsingPaws" }),
        el("p", { text: "一个账号连接手机、电脑和 TsingPaws" }),
      ]),
      refs.badge,
    ]));

    refs.stateCard = el("section", { className: "tp-state-card loading" });
    refs.stateIcon = el("div", { className: "tp-state-icon", text: "…" });
    refs.stateTitle = el("h3", { text: "正在确认绑定状态" });
    refs.stateDesc = el("p", { text: "请稍候，正在读取这台 TsingPaws 的连接信息。" });
    refs.account = el("div", { className: "tp-account-chip" });
    refs.cloud = el("strong", { text: "检查中" });
    refs.assistant = el("strong", { text: "检查中" });
    refs.health = el("div", { className: "tp-health-row" }, [
      el("div", { className: "tp-health-item" }, [
        el("span", { className: "tp-health-dot" }),
        el("span", { text: "APP 连接" }),
        refs.cloud,
      ]),
      el("div", { className: "tp-health-item" }, [
        el("span", { className: "tp-health-dot" }),
        el("span", { text: "TsingPaws 助手" }),
        refs.assistant,
      ]),
    ]);
    refs.stateCard.append(
      refs.stateIcon,
      el("div", { className: "tp-state-copy" }, [
        refs.stateTitle,
        refs.stateDesc,
        refs.account,
        refs.health,
      ]),
    );
    root.appendChild(refs.stateCard);

    refs.code = el("input", { className: "tp-code-input", inputmode: "numeric", maxlength: "6", placeholder: "六位绑定码", autocomplete: "one-time-code" });
    refs.code.addEventListener("input", () => {
      refs.code.value = refs.code.value.replace(/\D/g, "").slice(0, 6);
      render();
    });
    refs.code.addEventListener("keydown", (event) => { if (event.key === "Enter") claimPairing(); });
    refs.bind = el("button", { className: "primary", text: "确认绑定", onClick: claimPairing });
    refs.result = el("div", { className: "tp-pair-result" });
    refs.pairCard = el("section", { className: "tp-pair-card" });
    refs.pairTitle = el("h3", { text: "添加到 TsingPaws APP" });
    refs.pairDesc = el("p", {
      className: "tp-pair-desc",
      text: "在 APP 中选择“添加 TsingPaws”，然后输入 APP 显示的六位绑定码。",
    });
    refs.boundNotice = el("div", { className: "tp-bound-notice" });
    refs.pairCard.append(
      el("div", { className: "tp-section-heading" }, [
        el("span", { className: "tp-step-number", text: "1" }),
        el("div", {}, [
          refs.pairTitle,
          el("p", { text: "完成后，手机和电脑就能与这台 TsingPaws 对话" }),
        ]),
      ]),
      refs.pairDesc,
      el("label", { className: "tp-code-label", text: "六位绑定码" }),
      el("div", { className: "tp-pair-row" }, [refs.code, refs.bind]),
      refs.result,
      refs.boundNotice,
      el("div", { className: "tp-binding-rule" }, [
        el("span", { className: "tp-rule-icon", text: "i" }),
        el("p", { text: "一台 TsingPaws 同时只能绑定一个 APP 账号。更换账号前，请先在原 APP 中解除绑定。" }),
      ]),
    );
    root.appendChild(refs.pairCard);
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
    const known = Boolean(status.binding_known);
    const bound = Boolean(status.bound);
    const hint = status.account_hint || "原 APP";
    refs.stateCard.className = `tp-state-card ${!known ? "loading" : bound ? "bound" : "unbound"}`;
    refs.stateIcon.textContent = !known ? "…" : bound ? "✓" : "＋";
    refs.stateTitle.textContent = !known ? "正在确认绑定状态" : bound ? "已连接到 TsingPaws APP" : "等待绑定 TsingPaws APP";
    refs.stateDesc.textContent = !known
      ? "请稍候，正在读取这台 TsingPaws 的连接信息。"
      : bound
        ? "绑定已完成，可以在相同账号的手机和电脑上开始对话。"
        : "输入 APP 生成的六位绑定码，即可完成连接。";
    refs.account.textContent = bound ? `当前绑定账号  ${hint}` : "";
    refs.account.hidden = !bound;
    refs.health.hidden = !bound;
    refs.code.disabled = !status.pairing_enabled || STATE.busy;
    refs.code.placeholder = bound ? "已完成绑定" : known ? "六位绑定码" : "正在确认";
    if (bound) refs.code.value = "";
    refs.bind.disabled = STATE.busy || refs.code.value.length !== 6 || !status.pairing_enabled;
    refs.bind.textContent = STATE.busy ? "正在绑定…" : bound ? "已绑定" : !known ? "确认状态中" : "确认绑定";
    refs.pairCard.classList.toggle("is-bound", bound);
    refs.pairTitle.textContent = bound ? "这台 TsingPaws 已完成绑定" : "添加到 TsingPaws APP";
    refs.pairDesc.textContent = bound
      ? "如需更换账号，请先使用原账号在 APP 中解除绑定。"
      : "在 APP 中选择“添加 TsingPaws”，然后输入 APP 显示的六位绑定码。";
    refs.boundNotice.textContent = bound
      ? `请使用 ${hint} 账号在原 APP 中解除绑定后，再连接其他账号。`
      : "";
    refs.boundNotice.hidden = !bound;
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
