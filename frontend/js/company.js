import { getAuth, initShell, isReadOnlyRole, saveAuth, syncSessionFromServer } from "/ui/js/auth.js";
import { api } from "/ui/js/api.js";
import { escapeHtml, formatDate, numberFmt, renderEmpty, renderError, renderSkeleton } from "/ui/js/utils.js";

const session = initShell("company");
if (!session) {
  throw new Error("Session absente");
}

const canWrite = !isReadOnlyRole(session.role);
const state = {
  profile: null,
  sites: [],
  processes: [],
  activities: [],
  products: [],
  proofs: [],
  importTypes: [],
  edit: {
    siteId: "",
    processId: "",
    activityId: "",
    productId: "",
  },
};

const refs = {
  tabs: Array.from(document.querySelectorAll("[data-tab]")),
  panels: Array.from(document.querySelectorAll("[data-panel]")),
  title: document.getElementById("company-title"),
  subtitle: document.getElementById("company-subtitle"),
  metaTenant: document.getElementById("company-meta-tenant"),
  metaRole: document.getElementById("company-meta-role"),
  metaCountry: document.getElementById("company-meta-country"),
  statSites: document.getElementById("stat-sites"),
  statProcesses: document.getElementById("stat-processes"),
  statActivities: document.getElementById("stat-activities"),
  statProducts: document.getElementById("stat-products"),
  statProofs: document.getElementById("stat-proofs"),
  statChemicals: document.getElementById("stat-chemicals"),
  profileForm: document.getElementById("company-form"),
  readOnlyProfile: document.getElementById("company-readonly"),
  companyName: document.getElementById("company-name"),
  companySector: document.getElementById("company-sector"),
  companySubsector: document.getElementById("company-subsector"),
  companyHeadcount: document.getElementById("company-headcount"),
  companyActivities: document.getElementById("company-activities"),
  companyCerts: document.getElementById("company-certs"),
  companyChemicals: document.getElementById("company-chemicals"),
  chemicalsSummary: document.getElementById("chemicals-summary"),
  siteCode: document.getElementById("site-code"),
  siteName: document.getElementById("site-name"),
  siteCity: document.getElementById("site-city"),
  siteRegion: document.getElementById("site-region"),
  siteType: document.getElementById("site-type"),
  siteEmployees: document.getElementById("site-employees"),
  siteMainActivities: document.getElementById("site-main-activities"),
  siteSave: document.getElementById("site-save"),
  siteCancel: document.getElementById("site-cancel"),
  sitesList: document.getElementById("sites-list"),
  processSite: document.getElementById("process-site"),
  processName: document.getElementById("process-name"),
  processCode: document.getElementById("process-code"),
  processDescription: document.getElementById("process-description"),
  processSave: document.getElementById("process-save"),
  processCancel: document.getElementById("process-cancel"),
  processesList: document.getElementById("processes-list"),
  activitySite: document.getElementById("activity-site"),
  activityProcess: document.getElementById("activity-process"),
  activityName: document.getElementById("activity-name"),
  activityCode: document.getElementById("activity-code"),
  activityDescription: document.getElementById("activity-description"),
  activitySave: document.getElementById("activity-save"),
  activityCancel: document.getElementById("activity-cancel"),
  activitiesList: document.getElementById("activities-list"),
  productSite: document.getElementById("product-site"),
  productDesignation: document.getElementById("product-designation"),
  productReference: document.getElementById("product-reference"),
  productFamily: document.getElementById("product-family"),
  productCategory: document.getElementById("product-category"),
  productType: document.getElementById("product-type"),
  productNature: document.getElementById("product-nature"),
  productUnit: document.getElementById("product-unit"),
  productSave: document.getElementById("product-save"),
  productCancel: document.getElementById("product-cancel"),
  productsList: document.getElementById("products-list"),
  importDatasetType: document.getElementById("import-dataset-type"),
  importFile: document.getElementById("import-file"),
  importSubmit: document.getElementById("import-submit"),
  importResult: document.getElementById("import-result"),
  proofForm: document.getElementById("proof-form"),
  proofReadOnly: document.getElementById("proof-readonly"),
  proofPdf: document.getElementById("proof-pdf"),
  proofRef: document.getElementById("proof-ref"),
  proofType: document.getElementById("proof-type"),
  proofList: document.getElementById("proof-list"),
};

function currentTenant() {
  const auth = getAuth();
  return String(auth.active_tenant_id || auth.tenant_id || "").trim();
}

function currentCompanyName() {
  return String(state.profile?.company_name || getAuth().company_name || currentTenant() || "Entreprise active");
}

function parseList(value = "") {
  return String(value || "")
    .split(/[\n,;]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function showToast(message, ok = true) {
  const el = document.createElement("div");
  el.style.cssText = `position:fixed;bottom:20px;right:20px;z-index:9999;padding:10px 18px;border-radius:10px;font-size:13px;font-weight:500;color:#fff;background:${ok ? "#16a34a" : "#dc2626"};box-shadow:0 8px 20px rgba(15,23,42,.18)`;
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2800);
}

function syncTabs(nextTab) {
  refs.tabs.forEach((button) => {
    button.classList.toggle("is-active", button.dataset.tab === nextTab);
  });
  refs.panels.forEach((panel) => {
    panel.classList.toggle("is-active", panel.dataset.panel === nextTab);
  });
}

function updateHero() {
  const auth = getAuth();
  const companyName = currentCompanyName();
  refs.title.textContent = companyName;
  refs.subtitle.textContent = state.profile?.main_activities
    ? String(state.profile.main_activities)
    : "Le contexte visible ici alimente l'applicabilite, la conformite et l'assistant expert du tenant actif.";
  refs.metaTenant.textContent = `Tenant actif: ${currentTenant() || "n/a"}`;
  refs.metaRole.textContent = `Role: ${auth.role || "n/a"}`;
  refs.metaCountry.textContent = `Pays: ${state.profile?.country || "TN"}`;
}

function updateStats() {
  const chemicalCount = Array.isArray(state.profile?.chemicals) ? state.profile.chemicals.length : parseList(refs.companyChemicals?.value).length;
  refs.statSites.textContent = numberFmt(state.sites.length);
  refs.statProcesses.textContent = numberFmt(state.processes.length);
  refs.statActivities.textContent = numberFmt(state.activities.length);
  refs.statProducts.textContent = numberFmt(state.products.length);
  refs.statProofs.textContent = numberFmt(state.proofs.length);
  refs.statChemicals.textContent = numberFmt(chemicalCount);
}

function syncCompanyName(newName) {
  const name = String(newName || "").trim();
  if (!name) {
    return;
  }
  saveAuth({ ...getAuth(), company_name: name });
  updateHero();
}

function renderChemicalsSummary() {
  const chemicals = Array.isArray(state.profile?.chemicals) ? state.profile.chemicals : parseList(refs.companyChemicals.value);
  if (!chemicals.length) {
    refs.chemicalsSummary.innerHTML = "<p>Aucune substance declaree.</p>";
    return;
  }
  refs.chemicalsSummary.innerHTML = `
    <p>${chemicals.length} substance(s) actuellement visibles dans le contexte entreprise.</p>
    <div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:10px">
      ${chemicals.map((item) => `<span style="display:inline-flex;padding:6px 10px;border-radius:999px;background:rgba(21,101,192,.08);border:0.5px solid rgba(21,101,192,.18);font-size:11px">${escapeHtml(item)}</span>`).join("")}
    </div>
  `;
}

function renderSiteOptions() {
  const options = [`<option value="">Aucun site specifique</option>`]
    .concat(state.sites.map((site) => `<option value="${escapeHtml(site.site_id || "")}">${escapeHtml(site.name || site.site_name || "Site")}</option>`));
  refs.processSite.innerHTML = options.join("");
  refs.activitySite.innerHTML = options.join("");
  refs.productSite.innerHTML = [`<option value="">Aucun site specifique</option>`]
    .concat(state.sites.map((site) => `<option value="${escapeHtml(site.name || site.site_name || "")}">${escapeHtml(site.name || site.site_name || "Site")}</option>`))
    .join("");
}

function renderProcessOptions() {
  const options = [`<option value="">Aucun processus parent</option>`]
    .concat(state.processes.map((process) => `<option value="${escapeHtml(process.process_id || "")}">${escapeHtml(process.process_name || process.name || "Processus")}</option>`));
  refs.activityProcess.innerHTML = options.join("");
}

function resetSiteForm() {
  state.edit.siteId = "";
  refs.siteCode.value = "";
  refs.siteName.value = "";
  refs.siteCity.value = "";
  refs.siteRegion.value = "";
  refs.siteType.value = "";
  refs.siteEmployees.value = "";
  refs.siteMainActivities.value = "";
  refs.siteSave.textContent = "Enregistrer le site";
}

function resetProcessForm() {
  state.edit.processId = "";
  refs.processSite.value = "";
  refs.processName.value = "";
  refs.processCode.value = "";
  refs.processDescription.value = "";
  refs.processSave.textContent = "Enregistrer le processus";
}

function resetActivityForm() {
  state.edit.activityId = "";
  refs.activitySite.value = "";
  refs.activityProcess.value = "";
  refs.activityName.value = "";
  refs.activityCode.value = "";
  refs.activityDescription.value = "";
  refs.activitySave.textContent = "Enregistrer l'activite";
}

function resetProductForm() {
  state.edit.productId = "";
  refs.productSite.value = "";
  refs.productDesignation.value = "";
  refs.productReference.value = "";
  refs.productFamily.value = "";
  refs.productCategory.value = "";
  refs.productType.value = "";
  refs.productNature.value = "";
  refs.productUnit.value = "";
  refs.productSave.textContent = "Enregistrer le produit";
}

function renderSites() {
  if (!state.sites.length) {
    renderEmpty(refs.sitesList, "Aucun site enregistre pour l'entreprise active.");
    return;
  }
  refs.sitesList.innerHTML = state.sites.map((site) => `
    <div class="item-row">
      <div>
        <div class="item-title">${escapeHtml(site.name || site.site_name || "Site")}</div>
        <div class="item-meta">
          ${escapeHtml(site.city || "Ville n/a")} | ${escapeHtml(site.type || site.site_type || "Type n/a")} | ${numberFmt(site.employee_count || 0)} pers.
          ${site.main_activities ? `<br />${escapeHtml(site.main_activities)}` : ""}
        </div>
      </div>
      <div class="section-actions">
        ${canWrite ? `<button type="button" class="btn btn-secondary" data-site-edit="${escapeHtml(site.site_id || "")}">Editer</button>` : ""}
        ${canWrite ? `<button type="button" class="btn btn-ghost" data-site-delete="${escapeHtml(site.site_id || "")}">Supprimer</button>` : ""}
      </div>
    </div>
  `).join("");

  refs.sitesList.querySelectorAll("[data-site-edit]").forEach((button) => {
    button.addEventListener("click", () => {
      const site = state.sites.find((item) => item.site_id === button.dataset.siteEdit);
      if (!site) {
        return;
      }
      state.edit.siteId = String(site.site_id || "");
      refs.siteCode.value = site.site_code || "";
      refs.siteName.value = site.name || site.site_name || "";
      refs.siteCity.value = site.city || "";
      refs.siteRegion.value = site.region || "";
      refs.siteType.value = site.type || site.site_type || "";
      refs.siteEmployees.value = site.employee_count ?? "";
      refs.siteMainActivities.value = site.main_activities || "";
      refs.siteSave.textContent = "Mettre a jour le site";
    });
  });

  refs.sitesList.querySelectorAll("[data-site-delete]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!window.confirm("Supprimer ce site ?")) {
        return;
      }
      try {
        await api.deleteCompanySite(button.dataset.siteDelete);
        await loadSites();
        showToast("Site supprime.");
      } catch (error) {
        showToast(error.message || "Suppression impossible", false);
      }
    });
  });
}

function renderProcesses() {
  if (!state.processes.length) {
    renderEmpty(refs.processesList, "Aucun processus enregistre.");
    return;
  }
  refs.processesList.innerHTML = state.processes.map((process) => `
    <div class="item-row">
      <div>
        <div class="item-title">${escapeHtml(process.process_name || process.name || "Processus")}</div>
        <div class="item-meta">
          ${escapeHtml(process.process_code || "Sans code")}
          ${process.site_name ? ` | Site: ${escapeHtml(process.site_name)}` : ""}
          ${process.description ? `<br />${escapeHtml(process.description)}` : ""}
        </div>
      </div>
      <div class="section-actions">
        ${canWrite ? `<button type="button" class="btn btn-secondary" data-process-edit="${escapeHtml(process.process_id || "")}">Editer</button>` : ""}
        ${canWrite ? `<button type="button" class="btn btn-ghost" data-process-delete="${escapeHtml(process.process_id || "")}">Supprimer</button>` : ""}
      </div>
    </div>
  `).join("");

  refs.processesList.querySelectorAll("[data-process-edit]").forEach((button) => {
    button.addEventListener("click", () => {
      const process = state.processes.find((item) => item.process_id === button.dataset.processEdit);
      if (!process) {
        return;
      }
      state.edit.processId = String(process.process_id || "");
      refs.processSite.value = process.site_id || "";
      refs.processName.value = process.process_name || process.name || "";
      refs.processCode.value = process.process_code || "";
      refs.processDescription.value = process.description || "";
      refs.processSave.textContent = "Mettre a jour le processus";
    });
  });

  refs.processesList.querySelectorAll("[data-process-delete]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!window.confirm("Supprimer ce processus ?")) {
        return;
      }
      try {
        await api.deleteCompanyProcess(button.dataset.processDelete);
        await loadProcesses();
        await loadActivities();
        showToast("Processus supprime.");
      } catch (error) {
        showToast(error.message || "Suppression impossible", false);
      }
    });
  });
}

function renderActivities() {
  if (!state.activities.length) {
    renderEmpty(refs.activitiesList, "Aucune activite enregistree.");
    return;
  }
  refs.activitiesList.innerHTML = state.activities.map((activity) => `
    <div class="item-row">
      <div>
        <div class="item-title">${escapeHtml(activity.activity_name || activity.name || "Activite")}</div>
        <div class="item-meta">
          ${activity.process_name ? `Processus: ${escapeHtml(activity.process_name)}` : "Sans processus"}
          ${activity.site_name ? ` | Site: ${escapeHtml(activity.site_name)}` : ""}
          ${activity.code ? ` | Code: ${escapeHtml(activity.code)}` : ""}
          ${activity.description ? `<br />${escapeHtml(activity.description)}` : ""}
        </div>
      </div>
      <div class="section-actions">
        ${canWrite ? `<button type="button" class="btn btn-secondary" data-activity-edit="${escapeHtml(activity.activity_id || "")}">Editer</button>` : ""}
        ${canWrite ? `<button type="button" class="btn btn-ghost" data-activity-delete="${escapeHtml(activity.activity_id || "")}">Supprimer</button>` : ""}
      </div>
    </div>
  `).join("");

  refs.activitiesList.querySelectorAll("[data-activity-edit]").forEach((button) => {
    button.addEventListener("click", () => {
      const activity = state.activities.find((item) => item.activity_id === button.dataset.activityEdit);
      if (!activity) {
        return;
      }
      state.edit.activityId = String(activity.activity_id || "");
      refs.activitySite.value = activity.site_id || "";
      refs.activityProcess.value = activity.process_id || "";
      refs.activityName.value = activity.activity_name || activity.name || "";
      refs.activityCode.value = activity.code || "";
      refs.activityDescription.value = activity.description || "";
      refs.activitySave.textContent = "Mettre a jour l'activite";
    });
  });

  refs.activitiesList.querySelectorAll("[data-activity-delete]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!window.confirm("Supprimer cette activite ?")) {
        return;
      }
      try {
        await api.deleteCompanyActivity(button.dataset.activityDelete);
        await loadActivities();
        showToast("Activite supprimee.");
      } catch (error) {
        showToast(error.message || "Suppression impossible", false);
      }
    });
  });
}

function renderProducts() {
  if (!state.products.length) {
    renderEmpty(refs.productsList, "Aucun produit enregistre.");
    return;
  }
  refs.productsList.innerHTML = state.products.map((product) => `
    <div class="item-row">
      <div>
        <div class="item-title">${escapeHtml(product.designation || "Produit")}</div>
        <div class="item-meta">
          ${escapeHtml(product.reference || "Sans reference")}
          ${product.family ? ` | ${escapeHtml(product.family)}` : ""}
          ${product.product_type ? ` | ${escapeHtml(product.product_type)}` : ""}
          ${product.site_name ? ` | Site: ${escapeHtml(product.site_name)}` : ""}
          ${(product.nature || product.unit) ? `<br />${escapeHtml(product.nature || "Nature n/a")} | ${escapeHtml(product.unit || "Unite n/a")}` : ""}
        </div>
      </div>
      <div class="section-actions">
        ${canWrite ? `<button type="button" class="btn btn-secondary" data-product-edit="${escapeHtml(product.product_id || "")}">Editer</button>` : ""}
        ${canWrite ? `<button type="button" class="btn btn-ghost" data-product-delete="${escapeHtml(product.product_id || "")}">Supprimer</button>` : ""}
      </div>
    </div>
  `).join("");

  refs.productsList.querySelectorAll("[data-product-edit]").forEach((button) => {
    button.addEventListener("click", () => {
      const product = state.products.find((item) => item.product_id === button.dataset.productEdit);
      if (!product) {
        return;
      }
      state.edit.productId = String(product.product_id || "");
      refs.productSite.value = product.site_name || "";
      refs.productDesignation.value = product.designation || "";
      refs.productReference.value = product.reference || "";
      refs.productFamily.value = product.family || "";
      refs.productCategory.value = product.category || "";
      refs.productType.value = product.product_type || "";
      refs.productNature.value = product.nature || "";
      refs.productUnit.value = product.unit || "";
      refs.productSave.textContent = "Mettre a jour le produit";
    });
  });

  refs.productsList.querySelectorAll("[data-product-delete]").forEach((button) => {
    button.addEventListener("click", async () => {
      if (!window.confirm("Supprimer ce produit ?")) {
        return;
      }
      try {
        await api.deleteCompanyProduct(button.dataset.productDelete);
        await loadProducts();
        showToast("Produit supprime.");
      } catch (error) {
        showToast(error.message || "Suppression impossible", false);
      }
    });
  });
}

function renderProofs() {
  if (!state.proofs.length) {
    renderEmpty(refs.proofList, "Aucune preuve uploadee pour cette entreprise.");
    return;
  }
  refs.proofList.innerHTML = `
    <div class="table-wrap">
      <table class="proof-table">
        <thead>
          <tr>
            <th>Reference</th>
            <th>Type</th>
            <th>Fichier</th>
            <th>Date</th>
          </tr>
        </thead>
        <tbody>
          ${state.proofs.map((item) => `
            <tr>
              <td>${escapeHtml(item.reference || "n/a")}</td>
              <td>${escapeHtml(item.audit_type || "n/a")}</td>
              <td>${escapeHtml((item.source_file || "").split("/").pop() || "n/a")}</td>
              <td>${escapeHtml(formatDate(item.created_at))}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function renderImportResult(message, tone = "info") {
  const palette = {
    info: "#0f172a",
    success: "#166534",
    error: "#991b1b",
  };
  refs.importResult.style.color = palette[tone] || palette.info;
  refs.importResult.innerHTML = message;
}

async function loadProfile() {
  try {
    const profile = await api.companyProfile();
    state.profile = profile;
    refs.companyName.value = profile.company_name || "";
    refs.companySector.value = profile.sector || "";
    refs.companySubsector.value = profile.sub_sector || "";
    refs.companyHeadcount.value = profile.headcount_total ?? profile.headcount ?? "";
    refs.companyActivities.value = profile.main_activities || "";
    refs.companyCerts.value = Array.isArray(profile.certifications) ? profile.certifications.join(", ") : "";
    refs.companyChemicals.value = Array.isArray(profile.chemicals) ? profile.chemicals.join("\n") : "";
    syncCompanyName(profile.company_name);
  } catch (error) {
    state.profile = null;
    if (!String(error?.message || "").toLowerCase().includes("tenant")) {
      console.warn("Company profile unavailable", error);
    }
  }
  renderChemicalsSummary();
  updateHero();
  updateStats();
}

async function loadSites() {
  renderSkeleton(refs.sitesList, { rows: 3 });
  try {
    const data = await api.companySites();
    state.sites = Array.isArray(data.items) ? data.items : [];
    renderSiteOptions();
    renderSites();
  } catch (error) {
    renderError(refs.sitesList, error, "Reessayer", loadSites);
  }
  updateStats();
}

async function loadProcesses() {
  renderSkeleton(refs.processesList, { rows: 3 });
  try {
    const data = await api.companyProcesses();
    state.processes = Array.isArray(data.items) ? data.items : [];
    renderProcessOptions();
    renderProcesses();
  } catch (error) {
    renderError(refs.processesList, error, "Reessayer", loadProcesses);
  }
  updateStats();
}

async function loadActivities() {
  renderSkeleton(refs.activitiesList, { rows: 3 });
  try {
    const data = await api.companyActivities();
    state.activities = Array.isArray(data.items) ? data.items : [];
    renderActivities();
  } catch (error) {
    renderError(refs.activitiesList, error, "Reessayer", loadActivities);
  }
  updateStats();
}

async function loadProducts() {
  renderSkeleton(refs.productsList, { rows: 3 });
  try {
    const data = await api.companyProducts();
    state.products = Array.isArray(data.items) ? data.items : [];
    renderProducts();
  } catch (error) {
    renderError(refs.productsList, error, "Reessayer", loadProducts);
  }
  updateStats();
}

async function loadProofs() {
  renderSkeleton(refs.proofList, { rows: 4 });
  try {
    const data = await api.companyProofs("", 80);
    state.proofs = Array.isArray(data.items) ? data.items : [];
    renderProofs();
  } catch (error) {
    renderError(refs.proofList, error, "Reessayer", loadProofs);
  }
  updateStats();
}

async function loadImportTypes() {
  try {
    const response = await api.companyImportTypes();
    state.importTypes = Array.isArray(response?.items) ? response.items : [];
    if (!state.importTypes.length) {
      refs.importDatasetType.innerHTML = `<option value="sites">Sites</option>`;
      return;
    }
    refs.importDatasetType.innerHTML = state.importTypes
      .map((item) => `<option value="${escapeHtml(item.dataset_type || "")}">${escapeHtml(item.label || item.dataset_type || "")}</option>`)
      .join("");
  } catch (error) {
    refs.importDatasetType.innerHTML = `<option value="sites">Sites</option>`;
    renderImportResult(`Catalogue d'import indisponible : ${escapeHtml(error.message || error)}`, "error");
  }
}

async function saveCompanyProfile(event) {
  event.preventDefault();
  const companyName = refs.companyName.value.trim();
  if (!companyName) {
    showToast("Le nom entreprise est obligatoire.", false);
    return;
  }
  const button = document.getElementById("save-company");
  button.disabled = true;
  button.textContent = "Enregistrement...";
  try {
    const payload = {
      company_name: companyName,
      sector: refs.companySector.value.trim(),
      sub_sector: refs.companySubsector.value.trim(),
      headcount: refs.companyHeadcount.value ? Number(refs.companyHeadcount.value) : null,
      main_activities: refs.companyActivities.value.trim(),
      certifications: parseList(refs.companyCerts.value),
      chemicals: parseList(refs.companyChemicals.value),
      country: state.profile?.country || "TN",
    };
    await api.upsertCompanyProfile(payload);
    state.profile = { ...(state.profile || {}), ...payload };
    syncCompanyName(companyName);
    renderChemicalsSummary();
    updateStats();
    showToast("Profil entreprise enregistre.");
  } catch (error) {
    showToast(error.message || "Enregistrement impossible", false);
  } finally {
    button.disabled = false;
    button.textContent = "Enregistrer le profil";
  }
}

async function saveSite() {
  const name = refs.siteName.value.trim();
  if (!name) {
    showToast("Le nom du site est obligatoire.", false);
    return;
  }
  refs.siteSave.disabled = true;
  try {
    await api.upsertCompanySite({
      site_id: state.edit.siteId || undefined,
      site_code: refs.siteCode.value.trim(),
      name,
      city: refs.siteCity.value.trim(),
      region: refs.siteRegion.value.trim(),
      type: refs.siteType.value.trim(),
      employee_count: refs.siteEmployees.value ? Number(refs.siteEmployees.value) : null,
      main_activities: refs.siteMainActivities.value.trim(),
    });
    resetSiteForm();
    await loadSites();
    await loadProcesses();
    await loadActivities();
    showToast("Site enregistre.");
  } catch (error) {
    showToast(error.message || "Enregistrement impossible", false);
  } finally {
    refs.siteSave.disabled = false;
  }
}

async function saveProcess() {
  const name = refs.processName.value.trim();
  if (!name) {
    showToast("Le nom du processus est obligatoire.", false);
    return;
  }
  refs.processSave.disabled = true;
  try {
    await api.upsertCompanyProcess({
      process_id: state.edit.processId || undefined,
      site_id: refs.processSite.value || undefined,
      name,
      process_code: refs.processCode.value.trim(),
      description: refs.processDescription.value.trim(),
    });
    resetProcessForm();
    await loadProcesses();
    await loadActivities();
    showToast("Processus enregistre.");
  } catch (error) {
    showToast(error.message || "Enregistrement impossible", false);
  } finally {
    refs.processSave.disabled = false;
  }
}

async function saveActivity() {
  const name = refs.activityName.value.trim();
  if (!name) {
    showToast("Le nom de l'activite est obligatoire.", false);
    return;
  }
  refs.activitySave.disabled = true;
  try {
    const selectedProcess = state.processes.find((item) => item.process_id === refs.activityProcess.value);
    await api.upsertCompanyActivity({
      activity_id: state.edit.activityId || undefined,
      site_id: refs.activitySite.value || undefined,
      process_id: refs.activityProcess.value || undefined,
      process_name: selectedProcess?.process_name || selectedProcess?.name || "",
      name,
      code: refs.activityCode.value.trim(),
      description: refs.activityDescription.value.trim(),
    });
    resetActivityForm();
    await loadActivities();
    showToast("Activite enregistree.");
  } catch (error) {
    showToast(error.message || "Enregistrement impossible", false);
  } finally {
    refs.activitySave.disabled = false;
  }
}

async function saveProduct() {
  const designation = refs.productDesignation.value.trim();
  if (!designation) {
    showToast("La designation du produit est obligatoire.", false);
    return;
  }
  refs.productSave.disabled = true;
  try {
    await api.upsertCompanyProduct({
      product_id: state.edit.productId || undefined,
      designation,
      reference: refs.productReference.value.trim(),
      family: refs.productFamily.value.trim(),
      category: refs.productCategory.value.trim(),
      product_type: refs.productType.value.trim(),
      nature: refs.productNature.value.trim(),
      unit: refs.productUnit.value.trim(),
      site_name: refs.productSite.value || "",
      is_active: true,
    });
    resetProductForm();
    await loadProducts();
    showToast("Produit enregistre.");
  } catch (error) {
    showToast(error.message || "Enregistrement impossible", false);
  } finally {
    refs.productSave.disabled = false;
  }
}

async function importDataset() {
  const datasetType = refs.importDatasetType.value;
  const file = refs.importFile.files?.[0];
  if (!datasetType) {
    showToast("Selectionne un type de donnees.", false);
    return;
  }
  if (!file) {
    showToast("Selectionne un fichier CSV ou XLSX.", false);
    return;
  }
  refs.importSubmit.disabled = true;
  renderImportResult("Import en cours...", "info");
  try {
    const form = new FormData();
    form.append("tenant_id", currentTenant());
    form.append("dataset_type", datasetType);
    form.append("import_file", file);
    const response = await api.importCompanyDataset(form);
    const report = response?.report || {};
    const warnings = Array.isArray(report.warnings) ? report.warnings : [];
    renderImportResult(`
      <strong>Import termine</strong><br />
      Dataset: ${escapeHtml(datasetType)}<br />
      Fichier: ${escapeHtml(file.name)}<br />
      Lignes lues: ${escapeHtml(String(report.rows_seen ?? "-"))}<br />
      Lignes creees: ${escapeHtml(String(report.inserted ?? "-"))}<br />
      Lignes mises a jour: ${escapeHtml(String(report.updated ?? "-"))}
      ${warnings.length ? `<br /><br /><strong>Avertissements</strong><br />${warnings.map((item) => escapeHtml(item)).join("<br />")}` : ""}
    `, "success");
    refs.importFile.value = "";
    await Promise.all([loadProfile(), loadSites(), loadProcesses(), loadActivities(), loadProducts(), loadProofs()]);
    showToast("Import termine.");
  } catch (error) {
    renderImportResult(`<strong>Import en echec</strong><br />${escapeHtml(error.message || error)}`, "error");
    showToast(error.message || "Import impossible", false);
  } finally {
    refs.importSubmit.disabled = false;
  }
}

async function uploadProof(event) {
  event.preventDefault();
  const file = refs.proofPdf.files?.[0];
  if (!file) {
    showToast("Selectionne un PDF de preuve.", false);
    return;
  }
  const submitButton = refs.proofForm.querySelector('button[type="submit"]');
  submitButton.disabled = true;
  submitButton.textContent = "Upload...";
  try {
    const form = new FormData();
    form.append("proof_pdf", file);
    form.append("tenant_id", currentTenant());
    form.append("reference", refs.proofRef.value.trim() || `proof_${Date.now()}`);
    form.append("audit_type", refs.proofType.value.trim() || "audit");
    form.append("category", "company");
    form.append("nature", "document");
    form.append("system_scope", "QHSE");
    form.append("state", "ACTIVE");
    await api.uploadCompanyProof(form);
    refs.proofPdf.value = "";
    refs.proofRef.value = "";
    await loadProofs();
    showToast("Preuve uploadee.");
  } catch (error) {
    showToast(error.message || "Upload impossible", false);
  } finally {
    submitButton.disabled = false;
    submitButton.textContent = "Uploader la preuve";
  }
}

function bindEvents() {
  refs.tabs.forEach((button) => {
    button.addEventListener("click", () => syncTabs(button.dataset.tab));
  });
  refs.profileForm.addEventListener("submit", saveCompanyProfile);
  refs.siteSave.addEventListener("click", saveSite);
  refs.siteCancel.addEventListener("click", resetSiteForm);
  refs.processSave.addEventListener("click", saveProcess);
  refs.processCancel.addEventListener("click", resetProcessForm);
  refs.activitySave.addEventListener("click", saveActivity);
  refs.activityCancel.addEventListener("click", resetActivityForm);
  refs.productSave.addEventListener("click", saveProduct);
  refs.productCancel.addEventListener("click", resetProductForm);
  refs.importSubmit.addEventListener("click", importDataset);
  refs.proofForm.addEventListener("submit", uploadProof);
  refs.companyChemicals.addEventListener("input", () => {
    renderChemicalsSummary();
    updateStats();
  });
}

function applyRoleUX() {
  if (canWrite) {
    refs.readOnlyProfile.classList.add("hidden");
    refs.proofReadOnly.classList.add("hidden");
    return;
  }
  refs.profileForm.classList.add("hidden");
  refs.proofForm.classList.add("hidden");
  refs.readOnlyProfile.classList.remove("hidden");
  refs.proofReadOnly.classList.remove("hidden");
}

async function bootstrap() {
  await syncSessionFromServer(session);
  bindEvents();
  applyRoleUX();
  renderSiteOptions();
  renderProcessOptions();
  updateHero();
  updateStats();
  await Promise.all([
    loadProfile(),
    loadSites(),
    loadProcesses(),
    loadActivities(),
    loadProducts(),
    loadProofs(),
    loadImportTypes(),
  ]);
}

bootstrap().catch((error) => {
  console.error("Company page bootstrap failed", error);
  showToast(error.message || "Chargement impossible", false);
});
