const App = (() => {
  const SESSION_KEY    = "qalitas_a1_session";
  const TENANT_KEY     = "qalitas_tenant";
  const TENANT_DEFAULT = "";
  const TENANT_AWARE_V1_PREFIXES = [
    "/api/v1/dashboard/overview",
    "/api/v1/documents",
    "/api/v1/requirements",
    "/api/v1/validation",
    "/api/v1/validations",
    "/api/v1/runs",
    "/api/v1/analytics/overview",
    "/api/v1/analytics/families",
  ];

  function getSession() {
    try { return JSON.parse(localStorage.getItem(SESSION_KEY) || "null"); }
    catch { return null; }
  }
  function setSession(s) {
    const payload = s || {};
    localStorage.setItem(SESSION_KEY, JSON.stringify(payload));
    if (payload && typeof payload.tenant_id === "string" && payload.tenant_id.trim()) {
      setTenant(payload.tenant_id);
    }
  }
  function clearSession() { localStorage.removeItem(SESSION_KEY); }
  function ensureAuth() {
    const s = getSession();
    if (!s || !s.access_token) { window.location.href = "/ui/login.html"; return null; }
    if (typeof s.tenant_id === "string" && s.tenant_id.trim()) setTenant(s.tenant_id);
    return s;
  }
  async function logout() {
    try {
      const s = getSession();
      if (s?.access_token) {
        await fetch("/api/v1/auth/logout", {
          method: "POST",
          headers: { "Authorization": `Bearer ${s.access_token}` },
        });
      }
    } catch {}
    clearSession();
    window.location.href = "/ui/login.html";
  }

  function _normalizeTenant(id) {
    const v = String(id || "").trim().toLowerCase();
    if (v) return v;
    const fromSession = String(getSession()?.tenant_id || "").trim().toLowerCase();
    return fromSession || TENANT_DEFAULT;
  }
  function _getSessionTenant() {
    const s = getSession();
    return String(s?.tenant_id || "").trim().toLowerCase() || "";
  }
  function getTenant() { return _normalizeTenant(localStorage.getItem(TENANT_KEY)); }
  function setTenant(id) { localStorage.setItem(TENANT_KEY, _normalizeTenant(id)); }

  function _isTenantAwareV1Path(pathname) {
    return TENANT_AWARE_V1_PREFIXES.some(p => pathname === p || pathname.startsWith(`${p}/`));
  }
  function _appendTenantIfNeeded(path) {
    if (typeof path !== "string" || !path.startsWith("/api/v1/")) return path;
    const url = new URL(path, window.location.origin);
    if (!_isTenantAwareV1Path(url.pathname)) return path;
    if (url.searchParams.has("tenant_id")) return path;
    url.searchParams.set("tenant_id", getTenant());
    return `${url.pathname}${url.search}${url.hash}`;
  }

  async function api(path, options = {}, withAuth = true) {
    const finalPath = _appendTenantIfNeeded(path);
    const opts = { ...options };
    opts.headers = opts.headers || {};
    if (withAuth) {
      const s = getSession();
      if (!s || !s.access_token) throw new Error("Session expirÃ©e");
      opts.headers["Authorization"] = `Bearer ${s.access_token}`;
    }
    const res = await fetch(finalPath, opts);
    if (res.status === 401) { logout(); return null; }
    if (!res.ok) {
      const txt = await res.text();
      throw new Error(txt || `HTTP ${res.status}`);
    }
    return res.json();
  }

  async function listTenants() {
    const data = await api("/api/v2/tenants");
    const items = Array.isArray(data?.items) ? data.items : [];
    return items
      .map(x => ({ tenant_id: _normalizeTenant(x?.tenant_id), company_name: x?.company_name || "", documents_count: Number(x?.documents_count || 0) }))
      .filter(x => x.tenant_id);
  }

  async function populateTenantSelect(selectId) {
    const select = document.getElementById(selectId);
    if (!select) return [];
    const sessionTenant = _getSessionTenant();
    const current = sessionTenant || getTenant();
    if (sessionTenant) setTenant(sessionTenant);
    let tenants = [];
    try { tenants = await listTenants(); } catch {}
    if (sessionTenant) tenants = tenants.filter(x => x.tenant_id === sessionTenant);
    if (!tenants.some(x => x.tenant_id === current)) tenants.unshift({ tenant_id: current, company_name: "", documents_count: 0 });
    select.innerHTML = tenants.map(x => {
      const n = x.company_name ? ` â€” ${x.company_name}` : "";
      return `<option value="${x.tenant_id}">${x.tenant_id}${n}</option>`;
    }).join("");
    select.value = current;
    select.disabled = Boolean(sessionTenant);
    if (!select.value && tenants.length) { setTenant(tenants[0].tenant_id); select.value = getTenant(); }
    return tenants;
  }

  function poll(jobId, onUpdate, interval = 3000, maxMs = 600000) {
    return new Promise((resolve, reject) => {
      const start = Date.now();
      const TERMINAL = new Set(["DONE", "FAILED", "ERROR"]);
      async function tick() {
        try {
          const job = await api(`/api/v1/runs/${jobId}/details`);
          if (onUpdate) onUpdate(job);
          if (TERMINAL.has(String(job.status || "").toUpperCase())) resolve(job);
          else if (Date.now() - start > maxMs) reject(new Error("Timeout polling job " + jobId));
          else setTimeout(tick, interval);
        } catch (err) { reject(err); }
      }
      tick();
    });
  }

  function toast(msg, type = "info") {
    let c = document.getElementById("_toast_c");
    if (!c) {
      c = document.createElement("div");
      c.id = "_toast_c";
      c.style.cssText = "position:fixed;top:64px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:8px;pointer-events:none";
      document.body.appendChild(c);
    }
    const colors = { info:"#1565C0", success:"#16a34a", error:"#DC2626", warning:"#F59E0B" };
    const el = document.createElement("div");
    el.style.cssText = `background:${colors[type]||colors.info};color:#fff;padding:10px 16px;border-radius:10px;font-size:12px;font-weight:500;box-shadow:0 4px 14px rgba(0,0,0,0.18);pointer-events:auto;max-width:320px;opacity:1;transition:opacity 0.3s`;
    el.textContent = msg;
    c.appendChild(el);
    setTimeout(() => { el.style.opacity = "0"; setTimeout(() => el.remove(), 300); }, 3500);
  }

  function formatNumber(v) {
    const n = Number(v || 0);
    return Number.isFinite(n) ? n.toLocaleString("fr-FR") : "0";
  }
  function pct(a, b) { return !b ? "0%" : `${Math.round((a / b) * 100)}%`; }
  function truncate(text, len = 70) { return String(text || "").length > len ? String(text).slice(0, len) + "â€¦" : String(text || ""); }
  function escHtml(s) { return String(s || "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }
  function fmtDate(iso) {
    if (!iso) return "â€”";
    return new Date(iso).toLocaleDateString("fr-TN", { day: "2-digit", month: "short", year: "numeric" });
  }

  function statusBadge(status) {
    const s = String(status || "").toUpperCase();
    const map = {
      "CONFORME":                ["badge-conforme",   "Conforme"],
      "PARTIELLEMENT_CONFORME":  ["badge-partiel",    "Partiel"],
      "NON_CONFORME":            ["badge-nc",         "Non conforme"],
      "ABSENCE_DE_PREUVE":       ["badge-absence",    "Absence"],
      "APPLICABLE":              ["badge-applicable", "Applicable"],
      "NON_APPLICABLE":          ["badge-absence",    "Non applicable"],
      "APPLICABLE_SOUS_CONDITIONS":["badge-incertain","Sous conditions"],
      "INCERTAIN":               ["badge-incertain",  "Incertain"],
      "PROMOTED":                ["badge-blue",       "Promu"],
      "TO_VALIDATE":             ["badge-incertain",  "A valider"],
      "REJECTED":                ["badge-nc",         "Rejete"],
      "DONE":                    ["badge-done",       "Termine"],
      "RUNNING":                 ["badge-running",    "En cours"],
      "FAILED":                  ["badge-failed",     "Echoue"],
      "ERROR":                   ["badge-failed",     "Erreur"],
      "PENDING":                 ["badge-pending",    "En attente"],
    };
    const [cls, label] = map[s] || ["badge-gray", s.replace(/_/g," ")];
    return `<span class="badge ${cls}">${label}</span>`;
  }

  function domainBadge(domain) {
    const d = String(domain || "").toUpperCase();
    const map = {
      "SST":               "domain-sst",
      "SANTE_SECURITE":    "domain-sst",
      "ENVIRONNEMENT":     "domain-env",
      "ENV":               "domain-env",
      "QUALITE":           "domain-qual",
      "ISO_9001":          "domain-qual",
      "ISO_14001":         "domain-env",
      "ISO_45001":         "domain-sst",
      "GOUVERNANCE":       "domain-gouv",
      "ADMINISTRATIF":     "domain-admin",
      "JURIDIQUE_GENERAL": "domain-jur",
    };
    const cls = map[d] || "badge-gray";
    return `<span class="badge ${cls}">${escHtml(domain || "â€”")}</span>`;
  }

  function severityBadge(sev) {
    const s = String(sev || "").toUpperCase();
    const map = { "CRITIQUE": "badge-critique", "MAJEUR": "badge-majeur", "MINEUR": "badge-mineur" };
    return `<span class="badge ${map[s]||"badge-gray"}">${s || "â€”"}</span>`;
  }

  function confidenceBadge(score) {
    const v = Number(score || 0);
    const cls = v >= 0.76 ? "conf-high" : v >= 0.65 ? "conf-med" : "conf-low";
    return `<span class="${cls}">${(v * 100).toFixed(0)}%</span>`;
  }

  const NAV_SECTIONS = [
    { label: "PRINCIPAL", items: [
      { key: "dashboard",      href: "/ui/dashboard.html",    label: "Vue globale",     icon: _iconGrid() },
    ]},
    { label: "PIPELINE IA", items: [
      { key: "documents",      href: "/ui/documents.html",    label: "Documents",       icon: _iconFile() },
      { key: "requirements",   href: "/ui/requirements.html", label: "Exigences",       icon: _iconList() },
      { key: "upload",         href: "/ui/upload.html",       label: "Pipeline JORT",   icon: _iconUpload() },
      { key: "company",        href: "/ui/company.html",      label: "Entreprise",      icon: _iconBuilding() },
    ]},
    { label: "GESTION", items: [
      { key: "applicability",  href: "/ui/applicability.html",label: "Applicabilité",   icon: _iconCheck() },
      { key: "compliance",     href: "/ui/compliance.html",   label: "Conformité",      icon: _iconShield() },
    ]},
    { label: "ANALYSE", items: [
      { key: "analytics",      href: "/ui/analytics.html",    label: "Analytics BI",    icon: _iconChart() },
      { key: "assistant",      href: "/ui/assistant.html",    label: "Assistant expert", icon: _iconChat(), purple: true },
    ]},
    { label: "PILOTAGE", items: [
      { key: "reports",        href: "/ui/reports.html",      label: "Rapports PDF",    icon: _iconDoc() },
      { key: "runs",           href: "/ui/runs.html",         label: "Historique runs", icon: _iconClock() },
      { key: "settings",       href: "/ui/settings.html",     label: "ParamÃ¨tres",      icon: _iconSettings() },
    ]},
  ];

  const TOP_NAV = [
    { key: "dashboard",    href: "/ui/dashboard.html",    label: "Dashboard" },
    { key: "requirements", href: "/ui/requirements.html", label: "Exigences" },
    { key: "compliance",   href: "/ui/compliance.html",   label: "ConformitÃ©" },
    { key: "analytics",   href: "/ui/analytics.html",    label: "Analytics" },
    { key: "assistant",   href: "/ui/assistant.html",    label: "Assistant IA" },
  ];

  function renderShell(activePage) {
    const me = ensureAuth();
    if (!me) return;
    const topbarEl = document.getElementById("topbar");
    const sidebarEl = document.getElementById("sidebar");
    if (topbarEl) topbarEl.innerHTML = _topbarHTML(me, activePage);
    if (sidebarEl) sidebarEl.innerHTML = _sidebarHTML(activePage);
    if (String(me.role || "").toUpperCase() === "AUDITEUR") {
      document.querySelectorAll("[data-write]").forEach(el => el.style.display = "none");
    }
    if (String(me.role || "").toUpperCase() !== "ADMIN_QHSE") {
      document.querySelectorAll("[data-admin]").forEach(el => el.style.display = "none");
    }
    return me;
  }

  function _topbarHTML(me, active) {
    const tenant = getTenant();
    const tenantLabel = tenant.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
    const initials = (me.display_name || me.username || "U")
      .split(" ").map(w => w[0] || "").join("").slice(0, 2).toUpperCase() || "U";
    const navLinks = TOP_NAV.map(n =>
      `<a href="${n.href}" class="topbar-nav-link${active === n.key ? " active" : ""}">${n.label}</a>`
    ).join("");
    return `
      <div class="topbar-left">
        <a href="/ui/dashboard.html" class="topbar-logo">QALI<span>TAS</span></a>
        <nav class="topbar-nav">${navLinks}</nav>
      </div>
      <div class="topbar-right">
        <span class="tenant-pill">${escHtml(tenantLabel)}</span>
        <div class="user-avatar" title="${escHtml(me.display_name || me.username)}">${initials}</div>
        <button class="topbar-logout" onclick="App.logout()">DÃ©connexion</button>
      </div>
    `;
  }

  function _sidebarHTML(active) {
    let html = "";
    for (const section of NAV_SECTIONS) {
      html += `<div class="sidebar-section-label">${section.label}</div>`;
      for (const item of section.items) {
        const isActive = active === item.key;
        const cls = ["sidebar-item", isActive ? "active" : "", item.purple ? "purple" : ""].filter(Boolean).join(" ");
        html += `<a href="${item.href}" class="${cls}">
          <span class="sidebar-icon">${item.icon}</span>
          <span>${item.label}</span>
        </a>`;
      }
    }
    return html;
  }

  function sidebarHTML(active) { return _sidebarHTML(active); }

  function _svg(d, vb = "0 0 16 16") {
    return `<svg width="14" height="14" viewBox="${vb}" fill="none" xmlns="http://www.w3.org/2000/svg">${d}</svg>`;
  }
  function _iconGrid()     { return _svg(`<rect x="1" y="1" width="6" height="6" rx="1" fill="currentColor"/><rect x="9" y="1" width="6" height="6" rx="1" fill="currentColor"/><rect x="1" y="9" width="6" height="6" rx="1" fill="currentColor"/><rect x="9" y="9" width="6" height="6" rx="1" fill="currentColor"/>`); }
  function _iconFile()     { return _svg(`<path d="M3 2h7l3 3v9a1 1 0 01-1 1H3a1 1 0 01-1-1V3a1 1 0 011-1z" stroke="currentColor" stroke-width="1.5" fill="none"/><path d="M10 2v3h3" stroke="currentColor" stroke-width="1.5"/>`); }
  function _iconList()     { return _svg(`<line x1="2" y1="4" x2="14" y2="4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><line x1="2" y1="8" x2="14" y2="8" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><line x1="2" y1="12" x2="10" y2="12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>`); }
  function _iconUpload()   { return _svg(`<path d="M8 11V3M8 3L5 6M8 3l3 3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/><path d="M2 13h12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>`); }
  function _iconBuilding() { return _svg(`<rect x="2" y="5" width="12" height="10" rx="1" stroke="currentColor" stroke-width="1.5" fill="none"/><path d="M5 15v-4h6v4" stroke="currentColor" stroke-width="1.5"/><path d="M1 7l7-5 7 5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>`); }
  function _iconCheck()    { return _svg(`<circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.5" fill="none"/><path d="M5.5 8.5l2 2 3-3.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>`); }
  function _iconShield()   { return _svg(`<path d="M8 2L3 4.5V8c0 3 2 5 5 6 3-1 5-3 5-6V4.5L8 2z" stroke="currentColor" stroke-width="1.5" fill="none" stroke-linejoin="round"/><path d="M5.5 8.5l2 2 3-3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>`); }
  function _iconChart()    { return _svg(`<path d="M2 13l4-6 3 3 3-5 3 3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" fill="none"/>`); }
  function _iconChat()     { return _svg(`<path d="M14 9.5A6 6 0 012 8V7a6 6 0 0112 0v2.5z" stroke="currentColor" stroke-width="1.5" fill="none"/><path d="M5 13.5L2 15l1-3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>`); }
  function _iconDoc()      { return _svg(`<path d="M3 2h8l3 3v9H3V2z" stroke="currentColor" stroke-width="1.5" fill="none"/><line x1="6" y1="8" x2="11" y2="8" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/><line x1="6" y1="11" x2="11" y2="11" stroke="currentColor" stroke-width="1.2" stroke-linecap="round"/>`); }
  function _iconClock()    { return _svg(`<circle cx="8" cy="8" r="6" stroke="currentColor" stroke-width="1.5" fill="none"/><path d="M8 5v3.5l2.5 1.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>`); }
  function _iconSettings() { return _svg(`<circle cx="8" cy="8" r="2.5" stroke="currentColor" stroke-width="1.4" fill="none"/><path d="M8 2v1.5M8 12.5V14M2 8h1.5M12.5 8H14M3.5 3.5l1.1 1.1M11.4 11.4l1.1 1.1M3.5 12.5l1.1-1.1M11.4 4.6l1.1-1.1" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>`); }

  return {
    api, ensureAuth, getSession, setSession, clearSession, logout,
    getTenant, setTenant, listTenants, populateTenantSelect,
    poll, toast,
    renderShell, sidebarHTML,
    statusBadge, domainBadge, severityBadge, confidenceBadge,
    formatNumber, pct, truncate, escHtml, fmtDate,
  };
})();
