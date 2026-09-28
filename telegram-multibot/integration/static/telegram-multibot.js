(() => {
  "use strict";

  const ROUTE = "/telegram-bots";
  const ROOT_ID = "tp-telegram-multibot";
  const NAV_ID = "tp-telegram-multibot-nav";
  const state = {
    bots: [],
    loading: false,
    error: "",
    busy: new Set(),
    editing: null,
    timer: 0,
    syncTimer: 0,
    observer: null,
  };

  function node(tag, attrs = {}, children = []) {
    const element = document.createElement(tag);
    Object.entries(attrs).forEach(([key, value]) => {
      if (key === "className") element.className = value;
      else if (key === "text") element.textContent = value;
      else if (key === "html") element.innerHTML = value;
      else if (key.startsWith("on") && typeof value === "function") {
        element.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (value !== false && value != null) {
        element.setAttribute(key, value === true ? "" : String(value));
      }
    });
    (Array.isArray(children) ? children : [children]).forEach((child) => {
      if (child != null) element.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return element;
  }

  function isRoute() {
    return (location.pathname || "").replace(/\/+$/, "") === ROUTE;
  }

  function botId(bot) {
    return String(bot.id ?? bot.bot_id ?? bot.name ?? "");
  }

  async function api(path, method = "GET", payload) {
    const options = {
      method,
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    };
    if (payload !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(payload);
    }
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.message || data.detail || data.error || `请求失败（HTTP ${response.status}）`);
    return data;
  }

  function normalizeBots(data) {
    const list = Array.isArray(data) ? data : Array.isArray(data?.bots) ? data.bots : Array.isArray(data?.items) ? data.items : [];
    return list.filter((item) => item && typeof item === "object").map((item) => ({ ...item, id: botId(item) }));
  }

  function statusInfo(bot) {
    if (bot.enabled === false) return { key: "disabled", label: "已停用", detail: "机器人当前不会接收消息" };
    const raw = String(bot.status || bot.state || "").toLowerCase();
    if (["running", "online", "connected", "ok", "healthy"].includes(raw)) {
      return { key: "online", label: "运行中", detail: bot.status_detail || "Telegram 连接正常" };
    }
    if (["starting", "connecting", "pending", "testing"].includes(raw)) {
      return { key: "pending", label: "连接中", detail: bot.status_detail || "正在连接 Telegram" };
    }
    if (["error", "failed", "offline", "conflict"].includes(raw) || bot.last_error) {
      return { key: "error", label: "异常", detail: bot.status_detail || bot.last_error || "连接异常，请测试配置" };
    }
    return { key: "unknown", label: "待确认", detail: bot.status_detail || "尚未获得运行状态" };
  }

  function allowFromText(value) {
    if (Array.isArray(value)) return value.join(", ");
    return typeof value === "string" ? value : "";
  }

  function parseAllowFrom(value) {
    return String(value || "")
      .split(/[\s,，;；]+/)
      .map((item) => item.trim())
      .filter(Boolean);
  }

  function toast(message, kind = "ok") {
    let area = document.getElementById("tp-tg-toast-area");
    if (!area) {
      area = node("div", { id: "tp-tg-toast-area", className: "tp-tg-toast-area", "aria-live": "polite" });
      document.body.appendChild(area);
    }
    const item = node("div", { className: `tp-tg-toast ${kind}`, text: message });
    area.appendChild(item);
    setTimeout(() => item.classList.add("show"), 10);
    setTimeout(() => {
      item.classList.remove("show");
      setTimeout(() => item.remove(), 180);
    }, 3200);
  }

  function setBusy(id, value) {
    if (value) state.busy.add(String(id));
    else state.busy.delete(String(id));
    render();
  }

  async function loadBots({ quiet = false } = {}) {
    if (state.loading) return;
    state.loading = true;
    if (!quiet) state.error = "";
    render();
    try {
      state.bots = normalizeBots(await api("/api/telegram-bots"));
      state.error = "";
    } catch (error) {
      state.error = error.message || "无法读取机器人列表";
    } finally {
      state.loading = false;
      render();
    }
  }

  function iconTelegram() {
    return node("span", {
      className: "tp-tg-plane",
      "aria-hidden": "true",
      html: '<svg viewBox="0 0 24 24"><path d="M21.7 3.3 18.5 20c-.2 1.2-.9 1.5-1.9.9l-4.8-3.6-2.3 2.2c-.3.3-.5.5-1 .5l.3-4.9 9-8.1c.4-.4-.1-.6-.6-.3L6.1 13.7l-4.8-1.5c-1-.3-1-1 .2-1.5L20.2 3c.9-.3 1.7.2 1.5.3Z"/></svg>',
    });
  }

  function ensureNav() {
    let existing = document.getElementById(NAV_ID);
    const telegramLink = document.querySelector('a[href="/channels/telegram"], a[href="/channels/telegram/"]');
    const channelLink = telegramLink || document.querySelector('a[href^="/channels/"]');
    if (!channelLink) return;
    if (!existing) {
      existing = node("a", { id: NAV_ID, href: ROUTE, className: `${channelLink.className || ""} tp-tg-nav` }, [
        iconTelegram(),
        node("span", { className: "tp-tg-nav-label", text: "Telegram 机器人" }),
        node("span", { className: "tp-tg-nav-count", text: String(state.bots.length) }),
      ]);
      existing.addEventListener("click", (event) => {
        if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        if (!isRoute()) history.pushState({}, "", ROUTE);
        scheduleSync(0);
      });
      const insertionTarget = telegramLink || channelLink;
      insertionTarget.insertAdjacentElement("afterend", existing);
    }
    existing.classList.toggle("active", isRoute());
    existing.setAttribute("aria-current", isRoute() ? "page" : "false");
    const count = existing.querySelector(".tp-tg-nav-count");
    if (count) count.textContent = String(state.bots.length);
  }

  function hideNative(main) {
    Array.from(main.children).forEach((child) => {
      if (child.id !== ROOT_ID) child.setAttribute("data-tp-tg-hidden", "1");
    });
  }

  function restoreNative(main) {
    main?.querySelectorAll("[data-tp-tg-hidden]").forEach((child) => child.removeAttribute("data-tp-tg-hidden"));
    main?.classList.remove("tp-tg-host");
  }

  function makeButton(label, className, action, disabled = false) {
    return node("button", { type: "button", className, text: label, disabled, onClick: action });
  }

  function renderCard(bot) {
    const id = botId(bot);
    const busy = state.busy.has(id);
    const status = statusInfo(bot);
    const displayName = bot.display_name || bot.name || bot.username || `机器人 ${id}`;
    const username = bot.username ? `@${String(bot.username).replace(/^@/, "")}` : "尚未识别 Telegram 用户名";
    const allow = allowFromText(bot.allow_from);
    const card = node("article", { className: `tp-tg-card status-${status.key}` });
    card.append(
      node("div", { className: "tp-tg-card-head" }, [
        node("div", { className: "tp-tg-avatar" }, [iconTelegram()]),
        node("div", { className: "tp-tg-card-title" }, [
          node("h3", { text: displayName }),
          node("p", { text: username }),
        ]),
        node("span", { className: `tp-tg-status ${status.key}` }, [
          node("i", { "aria-hidden": "true" }),
          node("span", { text: status.label }),
        ]),
      ]),
      node("div", { className: "tp-tg-card-body" }, [
        node("p", { className: "tp-tg-status-detail", text: status.detail }),
        node("div", { className: "tp-tg-meta" }, [
          node("span", { text: "允许用户" }),
          node("strong", { text: allow || "全部用户" }),
        ]),
      ]),
      node("div", { className: "tp-tg-actions" }, [
        makeButton("测试", "tp-tg-btn secondary", () => testBot(bot), busy),
        makeButton("编辑", "tp-tg-btn secondary", () => openEditor(bot), busy),
        makeButton(bot.enabled === false ? "启用" : "停用", "tp-tg-btn secondary", () => toggleBot(bot), busy),
        makeButton("删除", "tp-tg-btn danger", () => deleteBot(bot), busy),
      ]),
    );
    if (busy) card.classList.add("is-busy");
    return card;
  }

  function emptyState() {
    return node("section", { className: "tp-tg-empty" }, [
      node("div", { className: "tp-tg-empty-icon" }, [iconTelegram()]),
      node("h3", { text: "还没有 Telegram 机器人" }),
      node("p", { text: "添加 BotFather 创建的机器人 Token，即可让多个机器人共用这台 TsingPaws。" }),
      makeButton("添加第一个机器人", "tp-tg-btn primary", () => openEditor()),
    ]);
  }

  function buildRoot() {
    const root = node("div", { id: ROOT_ID, className: "tp-tg-root" });
    root.append(
      node("header", { className: "tp-tg-hero" }, [
        node("div", { className: "tp-tg-hero-icon" }, [iconTelegram()]),
        node("div", { className: "tp-tg-hero-copy" }, [
          node("span", { className: "tp-tg-eyebrow", text: "TELEGRAM BOTS" }),
          node("h1", { text: "Telegram 机器人" }),
          node("p", { text: "在一个 TsingPaws Gateway 中统一管理多个 Telegram 机器人。" }),
        ]),
        makeButton("添加机器人", "tp-tg-btn primary tp-tg-add", () => openEditor()),
      ]),
      node("div", { className: "tp-tg-summary", id: "tp-tg-summary" }),
      node("div", { className: "tp-tg-content", id: "tp-tg-content" }),
    );
    return root;
  }

  function render() {
    ensureNav();
    const root = document.getElementById(ROOT_ID);
    if (!root) return;
    const summary = root.querySelector("#tp-tg-summary");
    const content = root.querySelector("#tp-tg-content");
    if (!summary || !content) return;
    const online = state.bots.filter((bot) => statusInfo(bot).key === "online").length;
    const enabled = state.bots.filter((bot) => bot.enabled !== false).length;
    summary.replaceChildren(
      node("span", { className: "tp-tg-summary-item" }, [node("strong", { text: String(state.bots.length) }), " 个机器人"]),
      node("span", { className: "tp-tg-summary-item online" }, [node("i"), node("strong", { text: String(online) }), " 个运行中"]),
      node("span", { className: "tp-tg-summary-item" }, [node("strong", { text: String(enabled) }), " 个已启用"]),
      makeButton("刷新", "tp-tg-refresh", () => loadBots(), state.loading),
    );
    content.replaceChildren();
    if (state.loading && !state.bots.length) {
      content.appendChild(node("div", { className: "tp-tg-loading", text: "正在读取机器人列表…" }));
    } else if (state.error && !state.bots.length) {
      content.appendChild(node("section", { className: "tp-tg-error" }, [
        node("strong", { text: "机器人列表加载失败" }),
        node("p", { text: state.error }),
        makeButton("重新加载", "tp-tg-btn secondary", () => loadBots()),
      ]));
    } else if (!state.bots.length) {
      content.appendChild(emptyState());
    } else {
      if (state.error) content.appendChild(node("div", { className: "tp-tg-inline-error", text: state.error }));
      const grid = node("div", { className: "tp-tg-grid" });
      state.bots.forEach((bot) => grid.appendChild(renderCard(bot)));
      content.appendChild(grid);
    }
  }

  function formRow(label, input, hint) {
    return node("label", { className: "tp-tg-field" }, [
      node("span", { className: "tp-tg-field-label", text: label }),
      input,
      hint ? node("small", { text: hint }) : null,
    ]);
  }

  function closeEditor() {
    document.getElementById("tp-tg-modal")?.remove();
    state.editing = null;
  }

  function openEditor(bot = null) {
    closeEditor();
    state.editing = bot;
    const editing = Boolean(bot);
    const nameInput = node("input", {
      type: "text",
      maxlength: "64",
      required: true,
      value: bot?.display_name || bot?.name || "",
      placeholder: "例如：客服机器人",
      autocomplete: "off",
    });
    const tokenInput = node("input", {
      type: "password",
      required: !editing,
      placeholder: editing ? "留空表示保持当前 Token" : "粘贴 BotFather 提供的 Token",
      autocomplete: "new-password",
      spellcheck: "false",
    });
    const allowInput = node("textarea", {
      rows: "3",
      placeholder: "多个用户 ID 用逗号或换行分隔；留空允许全部用户",
      spellcheck: "false",
    });
    allowInput.value = allowFromText(bot?.allow_from);
    const enabledInput = node("input", { type: "checkbox" });
    enabledInput.checked = bot?.enabled !== false;
    const error = node("div", { className: "tp-tg-form-error", role: "alert" });
    const save = makeButton(editing ? "保存修改" : "添加机器人", "tp-tg-btn primary", () => {});
    const form = node("form", { className: "tp-tg-form" }, [
      formRow("机器人名称", nameInput, "仅用于管理页面显示。"),
      formRow(editing ? "Bot Token（可选）" : "Bot Token", tokenInput, editing ? "出于安全考虑，现有 Token 不会回显；留空即保持不变。" : "Token 只会提交到本机服务，保存后不会再次显示。"),
      formRow("允许用户 allow_from", allowInput, "可填写 Telegram 用户 ID 或用户名。"),
      node("label", { className: "tp-tg-switch-row" }, [
        enabledInput,
        node("span", { className: "tp-tg-switch" }),
        node("span", {}, [node("strong", { text: "启用机器人" }), node("small", { text: "保存后立即开始或停止接收消息" })]),
      ]),
      error,
      node("div", { className: "tp-tg-modal-actions" }, [
        makeButton("取消", "tp-tg-btn secondary", closeEditor),
        save,
      ]),
    ]);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const name = nameInput.value.trim();
      const token = tokenInput.value.trim();
      if (!name) { error.textContent = "请输入机器人名称。"; nameInput.focus(); return; }
      if (!editing && !token) { error.textContent = "首次添加时必须填写 Bot Token。"; tokenInput.focus(); return; }
      const payload = { name, enabled: enabledInput.checked, allow_from: parseAllowFrom(allowInput.value) };
      if (token) payload.token = token;
      save.disabled = true;
      save.textContent = "正在保存…";
      error.textContent = "";
      try {
        if (editing) await api(`/api/telegram-bots/${encodeURIComponent(botId(bot))}`, "PUT", payload);
        else await api("/api/telegram-bots", "POST", payload);
        tokenInput.value = "";
        closeEditor();
        toast(editing ? "机器人配置已更新" : "机器人已添加");
        await loadBots();
      } catch (err) {
        tokenInput.value = "";
        error.textContent = err.message || "保存失败，请检查配置。";
        save.disabled = false;
        save.textContent = editing ? "保存修改" : "添加机器人";
      }
    });
    save.addEventListener("click", (event) => { event.preventDefault(); form.requestSubmit(); });
    const modal = node("div", { id: "tp-tg-modal", className: "tp-tg-modal", role: "dialog", "aria-modal": "true" }, [
      node("div", { className: "tp-tg-modal-backdrop", onClick: closeEditor }),
      node("section", { className: "tp-tg-dialog" }, [
        node("header", { className: "tp-tg-dialog-head" }, [
          node("div", {}, [
            node("span", { className: "tp-tg-eyebrow", text: editing ? "EDIT BOT" : "NEW BOT" }),
            node("h2", { text: editing ? "编辑 Telegram 机器人" : "添加 Telegram 机器人" }),
          ]),
          makeButton("×", "tp-tg-modal-close", closeEditor),
        ]),
        form,
      ]),
    ]);
    modal.addEventListener("keydown", (event) => { if (event.key === "Escape") closeEditor(); });
    document.body.appendChild(modal);
    setTimeout(() => nameInput.focus(), 0);
  }

  async function toggleBot(bot) {
    const id = botId(bot);
    setBusy(id, true);
    try {
      await api(`/api/telegram-bots/${encodeURIComponent(id)}`, "PUT", { enabled: bot.enabled === false });
      toast(bot.enabled === false ? "机器人已启用" : "机器人已停用");
      await loadBots({ quiet: true });
    } catch (error) {
      toast(error.message || "操作失败", "bad");
    } finally {
      setBusy(id, false);
    }
  }

  async function testBot(bot) {
    const id = botId(bot);
    setBusy(id, true);
    try {
      const result = await api(`/api/telegram-bots/${encodeURIComponent(id)}/test`, "POST", {});
      toast(result.message || (result.ok === false ? "测试失败" : "连接测试成功"), result.ok === false ? "bad" : "ok");
      await loadBots({ quiet: true });
    } catch (error) {
      toast(error.message || "连接测试失败", "bad");
    } finally {
      setBusy(id, false);
    }
  }

  async function deleteBot(bot) {
    const name = bot.display_name || bot.name || bot.username || "这个机器人";
    if (!window.confirm(`确定删除“${name}”吗？删除后需要重新填写 Token 才能恢复。`)) return;
    const id = botId(bot);
    setBusy(id, true);
    try {
      await api(`/api/telegram-bots/${encodeURIComponent(id)}`, "DELETE");
      toast("机器人已删除");
      state.bots = state.bots.filter((item) => botId(item) !== id);
      render();
      await loadBots({ quiet: true });
    } catch (error) {
      toast(error.message || "删除失败", "bad");
    } finally {
      setBusy(id, false);
    }
  }

  function syncPanel() {
    ensureNav();
    const root = document.getElementById(ROOT_ID);
    if (!isRoute()) {
      if (root) {
        const main = root.parentElement;
        root.remove();
        restoreNative(main);
      }
      return;
    }
    const main = document.querySelector("main");
    if (!main) return;
    main.classList.add("tp-tg-host");
    hideNative(main);
    let panel = document.getElementById(ROOT_ID);
    if (!panel) {
      panel = buildRoot();
      main.prepend(panel);
      loadBots();
    } else if (panel.parentElement !== main || main.firstElementChild !== panel) {
      main.prepend(panel);
    }
    hideNative(main);
    render();
  }

  function scheduleSync(delay = 0) {
    clearTimeout(state.syncTimer);
    state.syncTimer = setTimeout(syncPanel, delay);
  }

  function boot() {
    scheduleSync(0);
    document.addEventListener("click", (event) => {
      const link = event.target?.closest?.(`a[href="${ROUTE}"]`);
      if (link) scheduleSync(20);
    }, true);
    window.addEventListener("popstate", () => scheduleSync(0));
    ["pushState", "replaceState"].forEach((method) => {
      const original = history[method];
      history[method] = function (...args) {
        const result = original.apply(this, args);
        scheduleSync(20);
        return result;
      };
    });
    state.observer = new MutationObserver((records) => {
      const panel = document.getElementById(ROOT_ID);
      const nav = document.getElementById(NAV_ID);
      const modal = document.getElementById("tp-tg-modal");
      const hasExternalChange = records.some((record) =>
        !(panel?.contains(record.target) || nav?.contains(record.target) || modal?.contains(record.target))
      );
      if (hasExternalChange) scheduleSync(30);
    });
    state.observer.observe(document.documentElement, { childList: true, subtree: true });
    state.timer = setInterval(() => { if (isRoute() && !state.editing) loadBots({ quiet: true }); }, 10000);
    scheduleSync(250);
    scheduleSync(700);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot, { once: true });
  else boot();
})();
