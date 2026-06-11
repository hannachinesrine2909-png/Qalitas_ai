const TOKEN_KEY = "qalitas.token";
const ROLE_KEY = "qalitas.role";
const TENANT_KEY = "qalitas.tenant";
const HOME_TENANT_KEY = "qalitas.home_tenant";
const ACTIVE_TENANT_KEY = "qalitas.active_tenant";
const COMPANY_NAME_KEY = "qalitas.company_name";
const USER_KEY = "qalitas.username";
const DISPLAY_KEY = "qalitas.display_name";
const LOGIN_AT_KEY = "qalitas.login_at";
const TTL_MS = 8 * 60 * 60 * 1000;
const WARN_BEFORE_MS = 5 * 60 * 1000;

const ROLE_ALIASES = {
  SUPER_ADMIN: ["SUPER_ADMIN"],
  ADMIN: ["SUPER_ADMIN", "ADMIN_QHSE"],
  ANALYSTE: ["ANALYSTE_CONFORMITE"],
  AUDITEUR: ["AUDITEUR"],
  WRITE: ["SUPER_ADMIN", "ADMIN_QHSE", "ANALYSTE_CONFORMITE"],
  ALL: ["SUPER_ADMIN", "ADMIN_QHSE", "ANALYSTE_CONFORMITE", "AUDITEUR"],
};

const SHELL_LABELS = {
  dashboard: "Pilotage",
  upload: "Documents & traitements",
  requirements: "Exigences reglementaires",
  applicability: "Applicabilite",
  compliance: "Conformite",
  assistant: "Assistant expert",
  analytics: "Analyses",
  onboarding: "Nouvelle entreprise",
};

const TOPBAR_LABELS = {
  dashboard: "Pilotage",
  requirements: "Exigences",
  assistant: "Assistant expert",
  analytics: "Analyses",
};

function nowMs() {
  return Date.now();
}

function escapeHtml(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function humanizeTenant(tenantId = "") {
  const clean = String(tenantId || "").trim();
  if (!clean) {
    return "tenant";
  }
  return clean.replace(/[_-]+/g, " ").replace(/\b\w/g, (char) => char.toUpperCase());
}

function companyLabel(session = {}) {
  return String(session.company_name || "").trim() || humanizeTenant(session.active_tenant_id || session.tenant_id || "");
}

export function saveAuth(payload = {}) {
  const previous = getAuth();
  const activeTenant = String(payload.active_tenant_id || payload.tenant_id || "");
  const homeTenant = String(payload.home_tenant_id || activeTenant || "");
  localStorage.setItem(TOKEN_KEY, String(payload.access_token || previous.token || ""));
  localStorage.setItem(ROLE_KEY, String(payload.role || previous.role || ""));
  localStorage.setItem(TENANT_KEY, activeTenant);
  localStorage.setItem(HOME_TENANT_KEY, homeTenant);
  localStorage.setItem(ACTIVE_TENANT_KEY, activeTenant);
  localStorage.setItem(COMPANY_NAME_KEY, String(payload.company_name || previous.company_name || ""));
  localStorage.setItem(USER_KEY, String(payload.username || previous.username || ""));
  localStorage.setItem(DISPLAY_KEY, String(payload.display_name || payload.username || previous.display_name || previous.username || ""));
  localStorage.setItem(LOGIN_AT_KEY, String(nowMs()));
}

export function clearAuth() {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(ROLE_KEY);
  localStorage.removeItem(TENANT_KEY);
  localStorage.removeItem(HOME_TENANT_KEY);
  localStorage.removeItem(ACTIVE_TENANT_KEY);
  localStorage.removeItem(COMPANY_NAME_KEY);
  localStorage.removeItem(USER_KEY);
  localStorage.removeItem(DISPLAY_KEY);
  localStorage.removeItem(LOGIN_AT_KEY);
}

export function getAuth() {
  const activeTenant = localStorage.getItem(ACTIVE_TENANT_KEY) || localStorage.getItem(TENANT_KEY) || "";
  const homeTenant = localStorage.getItem(HOME_TENANT_KEY) || activeTenant;
  const role = localStorage.getItem(ROLE_KEY) || "";
  return {
    token: localStorage.getItem(TOKEN_KEY) || "",
    role,
    tenant_id: activeTenant,
    active_tenant_id: activeTenant,
    home_tenant_id: homeTenant,
    company_name: localStorage.getItem(COMPANY_NAME_KEY) || "",
    is_super_admin: String(role || "").toUpperCase() === "SUPER_ADMIN",
    username: localStorage.getItem(USER_KEY) || "",
    display_name: localStorage.getItem(DISPLAY_KEY) || "",
    login_at: Number(localStorage.getItem(LOGIN_AT_KEY) || 0),
  };
}

export function isExpired(session = getAuth()) {
  if (!session || !session.token || !session.login_at) {
    return true;
  }
  return nowMs() - Number(session.login_at) > TTL_MS;
}

function redirectToLogin() {
  const next = encodeURIComponent(window.location.pathname + window.location.search);
  window.location.href = `/ui/login.html?next=${next}`;
}

export function requireAuth() {
  const session = getAuth();
  if (!session.token || isExpired(session)) {
    clearAuth();
    if (!window.location.pathname.endsWith("/login.html")) {
      redirectToLogin();
    }
    return null;
  }
  return session;
}

function expandRoleToken(token) {
  const cleaned = String(token || "").trim().toUpperCase();
  if (!cleaned) {
    return [];
  }
  if (ROLE_ALIASES[cleaned]) {
    return ROLE_ALIASES[cleaned];
  }
  return [cleaned];
}

function elementAllowsRole(elementRoleAttr, role) {
  const requested = String(elementRoleAttr || "").split(/[ ,|]+/).map((r) => r.trim()).filter(Boolean);
  if (!requested.length) {
    return true;
  }
  const allowed = new Set();
  requested.forEach((item) => {
    expandRoleToken(item).forEach((r) => allowed.add(r));
  });
  return allowed.has(String(role || "").toUpperCase());
}

export function requireRole(role = getAuth().role) {
  const nodes = document.querySelectorAll("[data-role]");
  nodes.forEach((el) => {
    const visible = elementAllowsRole(el.getAttribute("data-role"), role);
    if (visible) {
      el.classList.remove("hidden");
      el.removeAttribute("aria-hidden");
      el.querySelectorAll("button,input,select,textarea").forEach((ctrl) => {
        if (ctrl.dataset.forceDisabled === "1") {
          return;
        }
        ctrl.disabled = false;
      });
      return;
    }
    el.classList.add("hidden");
    el.setAttribute("aria-hidden", "true");
    el.querySelectorAll("button,input,select,textarea").forEach((ctrl) => {
      ctrl.disabled = true;
      ctrl.dataset.forceDisabled = "1";
    });
  });
}

export function isReadOnlyRole(role = getAuth().role) {
  return String(role || "").toUpperCase() === "AUDITEUR";
}

async function fetchVisibleTenants(session) {
  if (!session?.token) {
    return [];
  }
  try {
    const res = await fetch("/api/v2/tenants", {
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${session.token}`,
      },
    });
    if (!res.ok) {
      return [];
    }
    const payload = await res.json();
    return Array.isArray(payload?.items) ? payload.items : [];
  } catch (err) {
    console.warn("Tenant directory unavailable", err);
    return [];
  }
}

async function fetchSessionSnapshot(session) {
  if (!session?.token) {
    return null;
  }
  try {
    const res = await fetch("/api/v1/auth/me", {
      headers: {
        Accept: "application/json",
        Authorization: `Bearer ${session.token}`,
      },
    });
    if (!res.ok) {
      return null;
    }
    return await res.json();
  } catch (err) {
    console.warn("Session snapshot unavailable", err);
    return null;
  }
}

export async function syncSessionFromServer(session = getAuth()) {
  const snapshot = await fetchSessionSnapshot(session);
  if (!snapshot?.active_tenant_id && !snapshot?.company_name && !snapshot?.role) {
    return session;
  }
  saveAuth({ ...session, ...snapshot });
  return getAuth();
}

async function switchActiveTenant(targetTenantId) {
  const session = getAuth();
  const res = await fetch("/api/v1/auth/switch-tenant", {
    method: "POST",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
      Authorization: `Bearer ${session.token}`,
    },
    body: JSON.stringify({ tenant_id: targetTenantId }),
  });
  const rawText = await res.text();
  let payload = null;
  try {
    payload = rawText ? JSON.parse(rawText) : null;
  } catch {
    payload = null;
  }
  if (!res.ok) {
    throw new Error(payload?.detail || payload?.message || rawText || `Erreur ${res.status}`);
  }
  return payload || {};
}

function normalizeTenantItems(session, items = []) {
  const activeTenant = String(session.active_tenant_id || session.tenant_id || "").trim().toLowerCase();
  const normalized = [];
  const seen = new Set();

  (Array.isArray(items) ? items : []).forEach((item) => {
    const tenantId = String(item?.tenant_id || "").trim().toLowerCase();
    const companyName = String(item?.company_name || "").trim();
    const hasCompanyProfile = Boolean(item?.has_company_profile) || Boolean(companyName);
    if (!tenantId || seen.has(tenantId)) {
      return;
    }
    if (!hasCompanyProfile && tenantId !== activeTenant) {
      return;
    }
    seen.add(tenantId);
    normalized.push({
      tenant_id: tenantId,
      company_name: companyName,
      documents_count: Number(item?.documents_count || 0),
      has_company_profile: hasCompanyProfile,
      is_active_context: Boolean(item?.is_active_context) || tenantId === activeTenant,
    });
  });

  if (activeTenant && !seen.has(activeTenant)) {
    normalized.unshift({
      tenant_id: activeTenant,
      company_name: String(session.company_name || "").trim(),
      documents_count: 0,
      has_company_profile: Boolean(session.company_name),
      is_active_context: true,
    });
  }
  normalized.sort((a, b) => {
    if (a.is_active_context && !b.is_active_context) return -1;
    if (!a.is_active_context && b.is_active_context) return 1;
    return String(a.company_name || "").localeCompare(String(b.company_name || ""), "fr", { sensitivity: "base" })
      || String(a.tenant_id || "").localeCompare(String(b.tenant_id || ""), "fr", { sensitivity: "base" });
  });
  return normalized;
}

function closeTenantMenus() {
  document.querySelectorAll("[data-auth-tenant].is-open").forEach((node) => {
    node.classList.remove("is-open");
    const button = node.querySelector(".tenant-switcher-button");
    if (button) {
      button.setAttribute("aria-expanded", "false");
    }
  });
}

function renderTenantNode(node, session, tenantItems = []) {
  const items = normalizeTenantItems(session, tenantItems);
  const activeTenant = String(session.active_tenant_id || session.tenant_id || "").trim().toLowerCase();
  const activeItem = items.find((item) => item.tenant_id === activeTenant) || {
    tenant_id: activeTenant,
    company_name: String(session.company_name || "").trim(),
  };
  const label = activeItem.company_name || companyLabel(session);

  node.textContent = "";
  node.classList.remove("is-open", "tenant-switcher");

  if (!session.is_super_admin) {
    node.removeAttribute("title");
    const link = document.createElement("a");
    link.href = "/ui/company.html";
    link.className = "tenant-pill-link";
    link.title = label || "Entreprise active";
    link.textContent = label || "Entreprise";
    node.appendChild(link);
    return;
  }

  node.classList.add("tenant-switcher");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "tenant-switcher-button";
  button.innerHTML = `
    <span class="tenant-switcher-label">${escapeHtml(label || "tenant")}</span>
    <span class="tenant-switcher-caret" aria-hidden="true">v</span>
  `;
  button.setAttribute("aria-haspopup", "menu");
  button.setAttribute("aria-expanded", "false");

  const menu = document.createElement("div");
  menu.className = "tenant-switcher-menu";
  menu.setAttribute("role", "menu");

  const onboardingLink = document.createElement("a");
  onboardingLink.className = "tenant-switcher-link";
  onboardingLink.href = "/ui/onboarding.html";
  onboardingLink.textContent = "Nouvelle entreprise";
  menu.appendChild(onboardingLink);

  const companyLink = document.createElement("a");
  companyLink.className = "tenant-switcher-link";
  companyLink.href = "/ui/company.html";
  companyLink.textContent = "Fiche entreprise";
  menu.appendChild(companyLink);

  items.forEach((item) => {
    const entry = document.createElement("button");
    entry.type = "button";
    entry.className = `tenant-switcher-item${item.tenant_id === activeTenant ? " is-active" : ""}`;
    entry.setAttribute("role", "menuitem");
    entry.innerHTML = `
      <span class="tenant-switcher-item-title">${escapeHtml(item.company_name || humanizeTenant(item.tenant_id))}</span>
      <span class="tenant-switcher-item-meta">${item.documents_count} doc(s) | profil entreprise ${item.has_company_profile ? "pret" : "absent"}</span>
    `;
    entry.addEventListener("click", async () => {
      closeTenantMenus();
      if (item.tenant_id === activeTenant) {
        window.location.href = "/ui/company.html";
        return;
      }
      button.disabled = true;
      try {
        const payload = await switchActiveTenant(item.tenant_id);
        saveAuth(payload);
        window.location.reload();
      } catch (err) {
        button.disabled = false;
        window.alert(err?.message || "Impossible de changer de contexte entreprise");
      }
    });
    menu.appendChild(entry);
  });

  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    const opening = !node.classList.contains("is-open");
    closeTenantMenus();
    node.classList.toggle("is-open", opening);
    button.setAttribute("aria-expanded", opening ? "true" : "false");
  });

  node.appendChild(button);
  node.appendChild(menu);
}

function renderUser(session, tenantItems = []) {
  document.querySelectorAll("[data-auth-tenant]").forEach((node) => {
    renderTenantNode(node, session, tenantItems);
  });
  document.querySelectorAll("[data-auth-user]").forEach((node) => {
    node.textContent = session.display_name || session.username || "Utilisateur";
  });
  document.querySelectorAll("[data-auth-avatar]").forEach((node) => {
    const source = session.display_name || session.username || "Q";
    const initials = source.split(" ").map((part) => part.trim()[0] || "").join("").slice(0, 2).toUpperCase() || "Q";
    node.textContent = initials;
  });
}

function applyShellLabels() {
  Object.entries(SHELL_LABELS).forEach(([key, label]) => {
    document.querySelectorAll(`[data-nav="${key}"]`).forEach((node) => {
      node.textContent = label;
    });
  });
  Object.entries(TOPBAR_LABELS).forEach(([key, label]) => {
    document.querySelectorAll(`[data-top="${key}"]`).forEach((node) => {
      node.textContent = label;
    });
  });
  document.querySelectorAll(".sidebar-title").forEach((node) => {
    if (String(node.textContent || "").trim().toLowerCase() === "pipeline ia") {
      node.textContent = "Modules";
    }
  });
}

function injectSuperAdminNavigation(session) {
  if (!session?.is_super_admin) {
    return;
  }
  document.querySelectorAll(".sidebar-group").forEach((group) => {
    const companyLink = group.querySelector('[data-nav="company"]');
    if (!companyLink || group.querySelector('[data-nav="onboarding"]')) {
      return;
    }
    const link = document.createElement("a");
    link.className = "sidebar-link";
    link.dataset.nav = "onboarding";
    link.href = "/ui/onboarding.html";
    link.textContent = "Onboarding";
    companyLink.parentNode?.insertBefore(link, companyLink);
  });
}

export async function logout({ redirect = true } = {}) {
  const session = getAuth();
  try {
    if (session.token) {
      await fetch("/api/v1/auth/logout", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${session.token}`,
        },
      });
    }
  } catch (err) {
    console.warn("Logout API error", err);
  }
  clearAuth();
  if (redirect) {
    window.location.href = "/ui/login.html";
  }
}

function bindLogout() {
  document.querySelectorAll('[data-action="logout"]').forEach((btn) => {
    if (btn.dataset.boundLogout === "1") {
      return;
    }
    btn.dataset.boundLogout = "1";
    btn.addEventListener("click", async (event) => {
      event.preventDefault();
      await logout();
    });
  });
}

function bindTenantMenuDismiss() {
  if (document.body.dataset.boundTenantDismiss === "1") {
    return;
  }
  document.body.dataset.boundTenantDismiss = "1";
  document.addEventListener("click", () => {
    closeTenantMenus();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeTenantMenus();
    }
  });
}

async function hydrateTenantShell(session) {
  Object.assign(session, await syncSessionFromServer(session));
  const tenants = normalizeTenantItems(session, await fetchVisibleTenants(session));
  const activeTenant = String(session.active_tenant_id || session.tenant_id || "").trim().toLowerCase();
  const activeItem = tenants.find((item) => item.tenant_id === activeTenant);
  if (activeItem?.company_name && activeItem.company_name !== session.company_name) {
    localStorage.setItem(COMPANY_NAME_KEY, activeItem.company_name);
    session.company_name = activeItem.company_name;
  }
  renderUser(session, tenants);
}

function refreshActivity() {
  const session = getAuth();
  if (session.token && !isExpired(session)) {
    localStorage.setItem(LOGIN_AT_KEY, String(Date.now()));
  }
}

function startExpiryWatcher() {
  let warnShown = false;
  setInterval(() => {
    const session = getAuth();
    if (!session.token) return;
    const elapsed = Date.now() - Number(session.login_at || 0);
    const remaining = TTL_MS - elapsed;
    if (remaining <= 0) {
      clearAuth();
      window.location.href = "/ui/login.html";
      return;
    }
    if (remaining <= WARN_BEFORE_MS && !warnShown) {
      warnShown = true;
      let banner = document.getElementById("_session_warn");
      if (!banner) {
        banner = document.createElement("div");
        banner.id = "_session_warn";
        banner.style.cssText = "position:fixed;top:0;left:0;right:0;z-index:99999;background:#F59E0B;color:#fff;text-align:center;padding:8px 16px;font-size:13px;font-weight:500";
        banner.innerHTML = `Votre session expire dans 5 minutes. <button onclick="window.location.reload()" style="margin-left:12px;padding:2px 10px;border:1.5px solid #fff;border-radius:6px;background:transparent;color:#fff;cursor:pointer;font-size:12px">Renouveler</button>`;
        document.body.prepend(banner);
      }
    }
  }, 30000);

  ["click", "keydown", "mousemove", "touchstart"].forEach((evt) => {
    document.addEventListener(evt, refreshActivity, { passive: true });
  });
}

export function initShell(pageName = "") {
  if (pageName) {
    document.body.dataset.page = pageName;
  }
  applyShellLabels();
  const session = requireAuth();
  if (!session) {
    return null;
  }
  renderUser(session);
  requireRole(session.role);
  injectSuperAdminNavigation(session);
  bindLogout();
  bindTenantMenuDismiss();
  startExpiryWatcher();
  hydrateTenantShell(session).catch((err) => {
    console.warn("Tenant shell hydration failed", err);
  });
  return session;
}
