(() => {
  const API = "/api/skill-library/api/v1";
  const state = { q: "", category: "all", sort: "downloads", offset: 0, total: 0, categories: [] };
  const clientId = (() => {
    let id = localStorage.getItem("tp-skill-library-client");
    if (!id) {
      id = self.crypto?.randomUUID?.() || `web-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      localStorage.setItem("tp-skill-library-client", id);
    }
    return id;
  })();
  const names = { all: "全部", opc: "OpenClaw", office: "办公", devtools: "开发", finance: "金融", productivity: "效率", content: "内容", news: "资讯", education: "教育", data: "数据", deploy: "部署", life: "生活", business: "商业", knowledge: "知识" };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const fmt = (n) => new Intl.NumberFormat("zh-CN", { notation: Number(n) > 9999 ? "compact" : "standard", maximumFractionDigits: 1 }).format(n || 0);

  async function request(path, options = {}) {
    const response = await fetch(path, { ...options, credentials: "same-origin", headers: { "X-Skill-Library-Client": clientId, ...(options.headers || {}) } });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
    return data;
  }

  function panel() {
    let root = document.getElementById("tp-skill-library");
    if (root) return root;
    root = document.createElement("section");
    root.id = "tp-skill-library";
    root.innerHTML = `
      <div class="tp-lib-head"><div><h1>技能库</h1><p>从团队技能中心查找并下载需要的能力</p></div><div class="tp-lib-health"><i></i><span>连接技能中心</span></div></div>
      <form class="tp-lib-search"><span>⌕</span><input placeholder="搜索技能或描述，例如：腾讯会议、周报、数据分析"><select><option value="downloads">最受欢迎</option><option value="favorites">收藏最多</option><option value="name">名称排序</option><option value="relevance">相关度</option></select><button>搜索</button></form>
      <div class="tp-lib-cats"></div><div class="tp-lib-meta"></div><div class="tp-lib-grid"></div><div class="tp-lib-pages"></div><div class="tp-lib-modal" hidden></div>`;
    document.body.appendChild(root);
    root.querySelector("form").addEventListener("submit", (event) => { event.preventDefault(); state.q = root.querySelector("input").value.trim(); state.offset = 0; load(); });
    root.querySelector("select").addEventListener("change", (event) => { state.sort = event.target.value; state.offset = 0; load(); });
    let timer;
    root.querySelector("input").addEventListener("input", (event) => { clearTimeout(timer); timer = setTimeout(() => { state.q = event.target.value.trim(); state.offset = 0; load(); }, 450); });
    return root;
  }

  function show() {
    panel().classList.add("open");
    document.querySelectorAll("[data-tp-library-tab]").forEach((x) => x.classList.add("active"));
    loadCategories(); load();
  }
  function hide() {
    document.getElementById("tp-skill-library")?.classList.remove("open");
    document.querySelectorAll("[data-tp-library-tab]").forEach((x) => x.classList.remove("active"));
  }

  function addTab() {
    if (document.querySelector("[data-tp-library-tab]")) return;
    let expert = [...document.querySelectorAll("button, a, [role=tab], [role=button]")]
      .filter((x) => x.textContent.trim() === "专家" && x.getClientRects().length)
      .sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)[0];
    if (!expert) {
      const labels = [...document.querySelectorAll("body *")].filter((x) =>
        (x.textContent.trim() === "专家" || x.textContent.trim() === "技能") &&
        x.getClientRects().length && x.children.length === 0
      );
      const expertLabel = labels.filter((x) => x.textContent.trim() === "专家").sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top)[0];
      const skillLabel = labels.filter((x) => x.textContent.trim() === "技能").find((x) => expertLabel && Math.abs(x.getBoundingClientRect().top - expertLabel.getBoundingClientRect().top) < 8);
      if (expertLabel && skillLabel) {
        let common = expertLabel.parentElement;
        while (common && !common.contains(skillLabel)) common = common.parentElement;
        if (common) {
          expert = expertLabel;
          while (expert.parentElement && expert.parentElement !== common) expert = expert.parentElement;
        }
      }
    }
    if (!expert?.parentElement) return;
    const tab = document.createElement("button");
    tab.type = "button"; tab.textContent = "技能库"; tab.dataset.tpLibraryTab = "1"; tab.className = expert.className;
    tab.setAttribute("aria-label", "技能库");
    tab.addEventListener("click", (event) => { event.stopPropagation(); show(); });
    expert.insertAdjacentElement("afterend", tab);
    [...expert.parentElement.children].forEach((item) => { if (item !== tab) item.addEventListener("click", hide); });
  }

  async function loadCategories() {
    if (state.categories.length) return;
    try { state.categories = (await request(`${API}/categories`)).categories || []; renderCategories(); }
    catch (error) { panel().querySelector(".tp-lib-health span").textContent = "技能中心不可用"; }
  }
  function renderCategories() {
    const box = panel().querySelector(".tp-lib-cats");
    box.innerHTML = state.categories.map((x) => `<button class="${x.id === state.category ? "active" : ""}" data-category="${esc(x.id)}">${esc(names[x.id] || x.id)} <small>${fmt(x.count)}</small></button>`).join("");
    box.querySelectorAll("button").forEach((button) => button.onclick = () => { state.category = button.dataset.category; state.offset = 0; renderCategories(); load(); });
  }

  async function load() {
    const root = panel(), grid = root.querySelector(".tp-lib-grid");
    grid.innerHTML = '<div class="tp-lib-loading">正在查找技能…</div>';
    const query = new URLSearchParams({ q: state.q, category: state.category, sort: state.sort, order: "desc", offset: state.offset, limit: "20", clientId });
    try {
      const data = await request(`${API}/search?${query}`); state.total = data.total || 0;
      root.querySelector(".tp-lib-health span").textContent = `${fmt(state.total)} 个技能可用`;
      root.querySelector(".tp-lib-meta").textContent = state.q ? `“${state.q}” 找到 ${fmt(state.total)} 个结果` : `共 ${fmt(state.total)} 个技能`;
      grid.innerHTML = data.results.length ? data.results.map(card).join("") : '<div class="tp-lib-loading">没有找到匹配的技能</div>';
      grid.querySelectorAll("[data-skill]").forEach((item) => item.addEventListener("click", () => openDetail(item.dataset.skill)));
      renderPages();
    } catch (error) { grid.innerHTML = `<div class="tp-lib-loading">读取失败：${esc(error.message)}</div>`; }
  }
  function card(x) {
    return `<article class="tp-lib-card" data-skill="${esc(x.slug)}" tabindex="0"><div class="tp-lib-cardtop"><b>${esc((x.displayName || x.slug).trim()[0] || "技")}</b><span>v${esc(x.version)}</span></div><h2>${esc(x.displayName || x.slug)}</h2><code>${esc(x.slug)}</code><p>${esc(x.summary || "暂无技能说明")}</p><div class="tp-lib-tags">${(x.categories || []).slice(0, 2).map((c) => `<span>${esc(names[c] || c)}</span>`).join("")}</div><footer><span>↓ ${fmt(x.downloads)}　☆ ${fmt(x.favorites)}</span><button>详情</button></footer></article>`;
  }
  async function openDetail(slug) {
    const modal = panel().querySelector(".tp-lib-modal"); modal.hidden = false;
    modal.innerHTML = '<div class="tp-lib-dialog"><button class="tp-lib-close">×</button><div class="tp-lib-loading">正在读取详情…</div></div>';
    modal.querySelector(".tp-lib-close").onclick = () => { modal.hidden = true; };
    modal.onclick = (event) => { if (event.target === modal) modal.hidden = true; };
    try {
      const x = await request(`${API}/skills/${encodeURIComponent(slug)}?clientId=${encodeURIComponent(clientId)}`), v = x.latestVersion || {}, unsafe = x.moderation?.isMalwareBlocked || x.moderation?.isSuspicious;
      modal.innerHTML = `<div class="tp-lib-dialog"><button class="tp-lib-close">×</button><div class="tp-lib-detail-icon">${esc((x.displayName || x.slug).trim()[0] || "技")}</div><h2>${esc(x.displayName || x.slug)}</h2><code>${esc(x.slug)}</code><p>${esc(x.summary || "暂无技能说明")}</p><dl><div><dt>版本</dt><dd>v${esc(v.version || "—")}</dd></div><div><dt>发布者</dt><dd>${esc(x.publisher || "—")}</dd></div><div><dt>大小</dt><dd>${fmt(v.size)} B</dd></div><div><dt>安全检查</dt><dd class="${unsafe ? "bad" : "good"}">${unsafe ? "需要谨慎" : "未发现风险"}</dd></div></dl><button class="tp-lib-install">安装到技能</button><div class="tp-lib-result"></div></div>`;
      modal.querySelector(".tp-lib-close").onclick = () => { modal.hidden = true; };
      modal.querySelector(".tp-lib-install").onclick = () => installSkill(x.slug, modal);
    } catch (error) { modal.querySelector(".tp-lib-loading").textContent = `详情读取失败：${error.message}`; }
  }
  async function installSkill(slug, modal) {
    const button = modal.querySelector(".tp-lib-install"), result = modal.querySelector(".tp-lib-result");
    button.disabled = true; button.textContent = "正在下载并安装…"; result.textContent = "";
    try {
      const data = await request("/api/skill-library/install", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ slug }) });
      button.textContent = "安装完成"; result.className = "tp-lib-result good"; result.textContent = `${data.replaced ? "已更新" : "已安装"} v${data.version}，刷新“技能”页即可看到。`;
    } catch (error) { button.disabled = false; button.textContent = "重新安装"; result.className = "tp-lib-result bad"; result.textContent = `安装失败：${error.message}`; }
  }
  function renderPages() {
    const pages = Math.max(1, Math.ceil(state.total / 20)), current = Math.floor(state.offset / 20) + 1, box = panel().querySelector(".tp-lib-pages");
    box.innerHTML = `<button ${current <= 1 ? "disabled" : ""}>← 上一页</button><span>${current} / ${pages}</span><button ${current >= pages ? "disabled" : ""}>下一页 →</button>`;
    const buttons = box.querySelectorAll("button"); buttons[0].onclick = () => { state.offset -= 20; load(); panel().scrollTo({ top: 0, behavior: "smooth" }); }; buttons[1].onclick = () => { state.offset += 20; load(); panel().scrollTo({ top: 0, behavior: "smooth" }); };
  }

  const observer = new MutationObserver(addTab);
  observer.observe(document.documentElement, { childList: true, subtree: true });
  addTab();
})();
