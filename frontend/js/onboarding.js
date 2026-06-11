import { getAuth, initShell, saveAuth, syncSessionFromServer } from "/ui/js/auth.js";
import { api } from "/ui/js/api.js";
import { escapeHtml, renderEmpty } from "/ui/js/utils.js";

let session = initShell("onboarding");
if (!session) {
  throw new Error("Session absente");
}

session = await syncSessionFromServer(session);

const root = document.getElementById("onboarding-root");
const denied = document.getElementById("super-admin-required");

if (!session.is_super_admin) {
  denied?.classList.remove("hidden");
  root?.classList.add("hidden");
  throw new Error("Accès SUPER_ADMIN requis");
}

const state = {
  step: 0,
  sites: [],
  processes: [],
  activities: [],
  products: [],
  importFiles: [],
  proofFiles: [],
  regulatoryFiles: [],
  submitting: false,
};

const IMPORT_DATASET_LABELS = {
  sites: "Sites",
  processes: "Processus",
  activities: "Activites",
  products: "Produits",
  chemicals: "Substances chimiques",
  equipment: "Equipements",
  environmental_aspects: "Aspects environnementaux",
  sst_risks: "Risques SST",
  sst_significant_risks: "Risques SST significatifs",
  strategic_objectives: "Objectifs strategiques",
  nonconformities: "Non-conformites",
  audit_reports_metadata: "Metadonnees audits",
  compliance_evidence_manifest: "Manifest de preuves",
};

const refs = {
  rail: Array.from(document.querySelectorAll("[data-step]")),
  panels: Array.from(document.querySelectorAll("[data-panel]")),
  prev: document.getElementById("btn-prev"),
  next: document.getElementById("btn-next"),
  submit: document.getElementById("btn-submit"),
  status: document.getElementById("status-box"),
  tenantId: document.getElementById("tenant-id"),
  companyName: document.getElementById("company-name"),
  sector: document.getElementById("company-sector"),
  subSector: document.getElementById("company-sub-sector"),
  country: document.getElementById("company-country"),
  headcount: document.getElementById("company-headcount"),
  mainActivities: document.getElementById("company-main-activities"),
  certifications: document.getElementById("company-certifications"),
  adminUsername: document.getElementById("admin-username"),
  adminPassword: document.getElementById("admin-password"),
  adminDisplayName: document.getElementById("admin-display-name"),
  adminRole: document.getElementById("admin-role"),
  siteCode: document.getElementById("site-code"),
  siteName: document.getElementById("site-name"),
  siteCity: document.getElementById("site-city"),
  siteRegion: document.getElementById("site-region"),
  siteType: document.getElementById("site-type"),
  siteEmployees: document.getElementById("site-employees"),
  siteMainActivities: document.getElementById("site-main-activities"),
  sitesList: document.getElementById("sites-list"),
  processName: document.getElementById("process-name"),
  processCode: document.getElementById("process-code"),
  processDescription: document.getElementById("process-description"),
  processesList: document.getElementById("processes-list"),
  activityName: document.getElementById("activity-name"),
  activityProcessName: document.getElementById("activity-process-name"),
  activityCode: document.getElementById("activity-code"),
  activityDescription: document.getElementById("activity-description"),
  activitiesList: document.getElementById("activities-list"),
  productDesignation: document.getElementById("product-designation"),
  productReference: document.getElementById("product-reference"),
  productFamily: document.getElementById("product-family"),
  productCategory: document.getElementById("product-category"),
  productType: document.getElementById("product-type"),
  productNature: document.getElementById("product-nature"),
  productUnit: document.getElementById("product-unit"),
  productsList: document.getElementById("products-list"),
  chemicalsText: document.getElementById("chemicals-text"),
  chemicalsPreview: document.getElementById("chemicals-preview"),
  proofAuditType: document.getElementById("proof-audit-type"),
  proofFiles: document.getElementById("proof-files"),
  proofFilesList: document.getElementById("proof-files-list"),
  regFiles: document.getElementById("reg-files"),
  regFilesList: document.getElementById("reg-files-list"),
  importDatasetType: document.getElementById("import-dataset-type"),
  importFile: document.getElementById("import-file"),
  importFilesList: document.getElementById("import-files-list"),
  summaryCompany: document.getElementById("summary-company"),
  summaryAdmin: document.getElementById("summary-admin"),
  summaryStructure: document.getElementById("summary-structure"),
  summaryDocs: document.getElementById("summary-docs"),
};

function slugifyTenant(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "_")
    .replace(/_+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 64);
}

function fileStem(fileName = "") {
  return String(fileName || "").replace(/\.pdf$/i, "") || `document_${Date.now()}`;
}

function parseList(value = "") {
  return String(value || "")
    .split(/[\n,;]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function cleanMojibakeText(value = "") {
  return String(value ?? "")
    .replace(/ÃƒÂ©/g, "e")
    .replace(/Ã©/g, "e")
    .replace(/Ã¨/g, "e")
    .replace(/Ãª/g, "e")
    .replace(/Ã«/g, "e")
    .replace(/Ã‰/g, "E")
    .replace(/Ã€/g, "A")
    .replace(/Ã /g, "a")
    .replace(/Ã¢/g, "a")
    .replace(/Ã®/g, "i")
    .replace(/Ã¯/g, "i")
    .replace(/Ã´/g, "o")
    .replace(/Ã¶/g, "o")
    .replace(/Ã¹/g, "u")
    .replace(/Ã»/g, "u")
    .replace(/Ã¼/g, "u")
    .replace(/Ã§/g, "c")
    .replace(/â€™/g, "'")
    .replace(/â€œ|â€/g, "\"")
    .replace(/â€“|â€”/g, "-")
    .replace(/â€¢/g, "-")
    .replace(/Â/g, "");
}

function showStatus(message, tone = "info", extraHtml = "") {
  const palette = {
    info: "#1565C0",
    success: "#16a34a",
    warning: "#F59E0B",
    error: "#DC2626",
  };
  const safeMessage = decodePossibleMojibake(message);
  const safeExtraHtml = decodePossibleMojibake(extraHtml);
  refs.status.innerHTML = `
    <div class="state" style="border-color:${palette[tone] || palette.info};background:rgba(255,255,255,.92)">
      <strong>${escapeHtml(safeMessage)}</strong>
      ${safeExtraHtml ? `<div style="margin-top:8px">${safeExtraHtml}</div>` : ""}
    </div>
  `;
}

function clearStatus() {
  refs.status.innerHTML = "";
}

function decodePossibleMojibake(value = "") {
  const input = String(value ?? "");
  if (!/[ÃÂâ]/.test(input)) {
    return input;
  }
  try {
    const encoded = Array.from(input)
      .map((char) => {
        const code = char.charCodeAt(0);
        return code <= 0xff ? `%${code.toString(16).padStart(2, "0")}` : char;
      })
      .join("");
    const decoded = decodeURIComponent(encoded);
    return /[ÃÂâ]/.test(decoded) ? decoded.replace(/\u00c2/g, "") : decoded;
  } catch {
    return input.replace(/\u00c2/g, "");
  }
}

function autoFillTenant() {
  if (refs.tenantId.value.trim()) {
    return;
  }
  refs.tenantId.value = slugifyTenant(refs.companyName.value);
}

function renderCollection(container, items, renderItem, emptyMessage) {
  if (!items.length) {
    renderEmpty(container, emptyMessage);
    return;
  }
  container.innerHTML = items.map(renderItem).join("");
  container.querySelectorAll("[data-remove-index]").forEach((button) => {
    button.addEventListener("click", () => {
      const index = Number(button.dataset.removeIndex);
      const bucket = button.dataset.bucket;
      if (!Number.isInteger(index) || index < 0) {
        return;
      }
      state[bucket].splice(index, 1);
      renderAllCollections();
    });
  });
}

function renderAllCollections() {
  renderCollection(
    refs.sitesList,
    state.sites,
    (item, index) => `
      <div class="mini-row">
        <div>
          <div class="mini-row-title">${escapeHtml(item.name || "Site")}</div>
          <div class="mini-row-sub">${escapeHtml(item.city || "—")} | ${escapeHtml(item.type || "—")} | ${escapeHtml(String(item.employee_count || 0))} pers.</div>
        </div>
        <button type="button" class="btn btn-ghost" data-bucket="sites" data-remove-index="${index}">Supprimer</button>
      </div>
    `,
    "Aucun site ajouté pour l’instant.",
  );

  renderCollection(
    refs.processesList,
    state.processes,
    (item, index) => `
      <div class="mini-row">
        <div>
          <div class="mini-row-title">${escapeHtml(item.name || "Processus")}</div>
          <div class="mini-row-sub">${escapeHtml(item.process_code || "Sans code")} | ${escapeHtml(item.description || "Sans description")}</div>
        </div>
        <button type="button" class="btn btn-ghost" data-bucket="processes" data-remove-index="${index}">Supprimer</button>
      </div>
    `,
    "Aucun processus ajouté pour l’instant.",
  );

  renderCollection(
    refs.activitiesList,
    state.activities,
    (item, index) => `
      <div class="mini-row">
        <div>
          <div class="mini-row-title">${escapeHtml(item.name || "Activité")}</div>
          <div class="mini-row-sub">${escapeHtml(item.process_name || "Sans processus")} | ${escapeHtml(item.code || "Sans code")}</div>
        </div>
        <button type="button" class="btn btn-ghost" data-bucket="activities" data-remove-index="${index}">Supprimer</button>
      </div>
    `,
    "Aucune activité ajoutée pour l’instant.",
  );

  renderCollection(
    refs.productsList,
    state.products,
    (item, index) => `
      <div class="mini-row">
        <div>
          <div class="mini-row-title">${escapeHtml(item.designation || "Produit")}</div>
          <div class="mini-row-sub">${escapeHtml(item.reference || "Sans référence")} | ${escapeHtml(item.family || "Sans famille")} | ${escapeHtml(item.product_type || "Sans type")}</div>
        </div>
        <button type="button" class="btn btn-ghost" data-bucket="products" data-remove-index="${index}">Supprimer</button>
      </div>
    `,
    "Aucun produit ajouté pour l’instant.",
  );
}

function renderChemicalsPreview() {
  const chemicals = parseList(refs.chemicalsText.value);
  if (!chemicals.length) {
    refs.chemicalsPreview.innerHTML = "";
    return;
  }
  refs.chemicalsPreview.innerHTML = chemicals.map((item) => `<span>${escapeHtml(item)}</span>`).join("");
}

function renderFileList(container, files, bucketName) {
  if (!files.length) {
    renderEmpty(container, "Aucun fichier sélectionné.");
    return;
  }
  container.innerHTML = files.map((file, index) => `
    <div class="upload-item">
      <div>
        <div class="mini-row-title">${escapeHtml(file.name)}</div>
        <div class="mini-row-sub">${Math.round(file.size / 1024)} Ko</div>
      </div>
      <button type="button" class="btn btn-ghost" data-file-bucket="${bucketName}" data-file-index="${index}">Retirer</button>
    </div>
  `).join("");
  container.querySelectorAll("[data-file-index]").forEach((button) => {
    button.addEventListener("click", () => {
      const bucket = button.dataset.fileBucket;
      const index = Number(button.dataset.fileIndex);
      state[bucket].splice(index, 1);
      renderUploads();
    });
  });
}

function renderUploads() {
  renderFileList(refs.proofFilesList, state.proofFiles, "proofFiles");
  renderFileList(refs.regFilesList, state.regulatoryFiles, "regulatoryFiles");
  renderImportQueue();
}

function renderImportQueue() {
  if (!state.importFiles.length) {
    renderEmpty(refs.importFilesList, "Aucun fichier d'import en attente.");
    return;
  }
  refs.importFilesList.innerHTML = state.importFiles.map((item, index) => `
    <div class="upload-item">
      <div>
        <div class="mini-row-title">${escapeHtml(item.label)}</div>
        <div class="mini-row-sub">${escapeHtml(item.file.name)} | ${Math.round(item.file.size / 1024)} Ko</div>
      </div>
      <button type="button" class="btn btn-ghost" data-import-index="${index}">Retirer</button>
    </div>
  `).join("");
  refs.importFilesList.querySelectorAll("[data-import-index]").forEach((button) => {
    button.addEventListener("click", () => {
      const index = Number(button.dataset.importIndex);
      if (!Number.isInteger(index) || index < 0) {
        return;
      }
      state.importFiles.splice(index, 1);
      renderUploads();
    });
  });
}

function currentPayload() {
  return {
    tenant_id: slugifyTenant(refs.tenantId.value),
    company_name: refs.companyName.value.trim(),
    sector: refs.sector.value.trim(),
    sub_sector: refs.subSector.value.trim(),
    country: refs.country.value.trim() || "TN",
    certifications: parseList(refs.certifications.value),
    headcount: refs.headcount.value ? Number(refs.headcount.value) : null,
    main_activities: refs.mainActivities.value.trim(),
    sites: [...state.sites],
    processes: [...state.processes],
    activities: [...state.activities],
    products: [...state.products],
    chemicals: parseList(refs.chemicalsText.value),
    initial_admin: {
      username: refs.adminUsername.value.trim().toLowerCase(),
      password: refs.adminPassword.value,
      display_name: refs.adminDisplayName.value.trim(),
      role: refs.adminRole.value,
    },
  };
}

function validateIdentity() {
  const payload = currentPayload();
  if (!payload.tenant_id) {
    throw new Error("Le tenant_id est obligatoire.");
  }
  if (!payload.company_name) {
    throw new Error("Le nom d’entreprise est obligatoire.");
  }
  if (!payload.initial_admin.username) {
    throw new Error("Le username de l’administrateur initial est obligatoire.");
  }
  if ((payload.initial_admin.password || "").length < 8) {
    throw new Error("Le mot de passe initial doit contenir au moins 8 caractères.");
  }
return payload;
}

function renderSummaryLegacy_DoNotUse() {
  const payload = currentPayload();
  refs.summaryCompany.innerHTML = `
    <ul>
      <li><strong>Tenant:</strong> ${escapeHtml(payload.tenant_id || "—")}</li>
      <li><strong>Entreprise:</strong> ${escapeHtml(payload.company_name || "—")}</li>
      <li><strong>Secteur:</strong> ${escapeHtml(payload.sector || "—")}</li>
      <li><strong>Pays:</strong> ${escapeHtml(payload.country || "—")}</li>
    </ul>
  `;
  refs.summaryAdmin.innerHTML = `
    <ul>
      <li><strong>Username:</strong> ${escapeHtml(payload.initial_admin.username || "—")}</li>
      <li><strong>Affichage:</strong> ${escapeHtml(payload.initial_admin.display_name || payload.initial_admin.username || "—")}</li>
      <li><strong>Rôle:</strong> ${escapeHtml(payload.initial_admin.role || "—")}</li>
    </ul>
  `;
  refs.summaryStructure.innerHTML = `
    <ul>
      <li>${state.sites.length} site(s)</li>
      <li>${state.processes.length} processus</li>
      <li>${state.activities.length} activité(s)</li>
      <li>${state.products.length} produit(s)</li>
      <li>${parseList(refs.chemicalsText.value).length} substance(s) chimique(s)</li>
    </ul>
  `;
  refs.summaryDocs.innerHTML = `
    <ul>
      <li>${state.importFiles.length} import(s) CSV/XLSX en file d'attente</li>
      <li>${state.proofFiles.length} preuve(s) PDF à uploader</li>
      <li>${state.regulatoryFiles.length} document(s) réglementaire(s) A1 à lancer</li>
      <li>Type preuve par défaut: ${escapeHtml(refs.proofAuditType.value || "audit_initial")}</li>
    </ul>
  `;
}

function renderSummary() {
  const payload = currentPayload();
  refs.summaryCompany.innerHTML = `
    <ul>
      <li><strong>Tenant:</strong> ${escapeHtml(payload.tenant_id || "-")}</li>
      <li><strong>Entreprise:</strong> ${escapeHtml(payload.company_name || "-")}</li>
      <li><strong>Secteur:</strong> ${escapeHtml(payload.sector || "-")}</li>
      <li><strong>Pays:</strong> ${escapeHtml(payload.country || "-")}</li>
    </ul>
  `;
  refs.summaryAdmin.innerHTML = `
    <ul>
      <li><strong>Identifiant:</strong> ${escapeHtml(payload.initial_admin.username || "-")}</li>
      <li><strong>Affichage:</strong> ${escapeHtml(payload.initial_admin.display_name || payload.initial_admin.username || "-")}</li>
      <li><strong>Role d'acces:</strong> ${escapeHtml(payload.initial_admin.role || "-")}</li>
    </ul>
  `;
  refs.summaryStructure.innerHTML = `
    <ul>
      <li>${state.sites.length} site(s)</li>
      <li>${state.processes.length} processus</li>
      <li>${state.activities.length} activite(s)</li>
      <li>${state.products.length} produit(s)</li>
      <li>${parseList(refs.chemicalsText.value).length} substance(s) chimique(s)</li>
    </ul>
  `;
  refs.summaryDocs.innerHTML = `
    <ul>
      <li>${state.importFiles.length} import(s) CSV/XLSX en file d'attente</li>
      <li>${state.proofFiles.length ? "1 PDF audit / preuve initiale" : "0 PDF audit / preuve initiale"}</li>
      <li>${state.regulatoryFiles.length} document(s) reglementaire(s) a lancer</li>
      <li>Type de document initial: ${escapeHtml(refs.proofAuditType.value || "audit_initial")}</li>
    </ul>
  `;
}

function syncWizard() {
  refs.rail.forEach((stepButton) => {
    stepButton.classList.toggle("is-active", Number(stepButton.dataset.step) === state.step);
  });
  refs.panels.forEach((panel) => {
    panel.classList.toggle("is-active", Number(panel.dataset.panel) === state.step);
  });
  refs.prev.disabled = state.step === 0 || state.submitting;
  refs.next.classList.toggle("hidden", state.step === refs.panels.length - 1);
  refs.submit.classList.toggle("hidden", state.step !== refs.panels.length - 1);
  refs.next.disabled = state.submitting;
  refs.submit.disabled = state.submitting;
  if (state.step === refs.panels.length - 1) {
    renderSummary();
  }
}

function moveToStep(nextStep) {
  state.step = Math.max(0, Math.min(refs.panels.length - 1, nextStep));
  syncWizard();
}

function addSite() {
  const name = refs.siteName.value.trim();
  if (!name) {
    showStatus("Le nom du site est obligatoire.", "warning");
    return;
  }
  state.sites.push({
    site_code: refs.siteCode.value.trim(),
    name,
    city: refs.siteCity.value.trim(),
    region: refs.siteRegion.value.trim(),
    type: refs.siteType.value.trim(),
    employee_count: refs.siteEmployees.value ? Number(refs.siteEmployees.value) : null,
    main_activities: refs.siteMainActivities.value.trim(),
  });
  refs.siteCode.value = "";
  refs.siteName.value = "";
  refs.siteCity.value = "";
  refs.siteRegion.value = "";
  refs.siteType.value = "";
  refs.siteEmployees.value = "";
  refs.siteMainActivities.value = "";
  clearStatus();
  renderAllCollections();
}

function addProcess() {
  const name = refs.processName.value.trim();
  if (!name) {
    showStatus("Le nom du processus est obligatoire.", "warning");
    return;
  }
  state.processes.push({
    name,
    process_code: refs.processCode.value.trim(),
    description: refs.processDescription.value.trim(),
  });
  refs.processName.value = "";
  refs.processCode.value = "";
  refs.processDescription.value = "";
  clearStatus();
  renderAllCollections();
}

function addActivity() {
  const name = refs.activityName.value.trim();
  if (!name) {
    showStatus("Le nom de l’activité est obligatoire.", "warning");
    return;
  }
  state.activities.push({
    name,
    process_name: refs.activityProcessName.value.trim(),
    code: refs.activityCode.value.trim(),
    description: refs.activityDescription.value.trim(),
  });
  refs.activityName.value = "";
  refs.activityProcessName.value = "";
  refs.activityCode.value = "";
  refs.activityDescription.value = "";
  clearStatus();
  renderAllCollections();
}

function addProduct() {
  const designation = refs.productDesignation.value.trim();
  if (!designation) {
    showStatus("La désignation du produit est obligatoire.", "warning");
    return;
  }
  state.products.push({
    designation,
    reference: refs.productReference.value.trim(),
    family: refs.productFamily.value.trim(),
    category: refs.productCategory.value.trim(),
    product_type: refs.productType.value.trim(),
    nature: refs.productNature.value.trim(),
    unit: refs.productUnit.value.trim(),
    is_active: true,
  });
  refs.productDesignation.value = "";
  refs.productReference.value = "";
  refs.productFamily.value = "";
  refs.productCategory.value = "";
  refs.productType.value = "";
  refs.productNature.value = "";
  refs.productUnit.value = "";
  clearStatus();
  renderAllCollections();
}

function addImportFile() {
  const file = refs.importFile.files?.[0];
  const datasetType = refs.importDatasetType.value;
  if (!datasetType) {
    showStatus("Le type de donnees a importer est obligatoire.", "warning");
    return;
  }
  if (!file) {
    showStatus("Choisis un fichier CSV, XLSX ou XLS.", "warning");
    return;
  }
  const lowerName = String(file.name || "").toLowerCase();
  if (!(lowerName.endsWith(".csv") || lowerName.endsWith(".xlsx") || lowerName.endsWith(".xls"))) {
    showStatus("Le fichier d'import doit etre au format CSV, XLSX ou XLS.", "warning");
    return;
  }
  state.importFiles.push({
    dataset_type: datasetType,
    label: IMPORT_DATASET_LABELS[datasetType] || datasetType,
    file,
  });
  refs.importFile.value = "";
  clearStatus();
  renderUploads();
}

async function uploadProofs(tenantId) {
  const uploaded = [];
  for (const file of state.proofFiles) {
    const form = new FormData();
    form.append("proof_pdf", file);
    form.append("tenant_id", tenantId);
    form.append("reference", fileStem(file.name));
    form.append("audit_type", refs.proofAuditType.value.trim() || "audit_initial");
    form.append("category", "onboarding");
    form.append("nature", "document");
    form.append("system_scope", "QHSE");
    form.append("state", "ACTIVE");
    await api.uploadCompanyProof(form);
    uploaded.push(file.name);
  }
  return uploaded;
}

async function launchRegulatoryRuns(tenantId) {
  const jobs = [];
  for (const file of state.regulatoryFiles) {
    const form = new FormData();
    form.append("pdf", file);
    form.append("tenant", tenantId);
    form.append("title", fileStem(file.name));
    form.append("source", "onboarding");
    form.append("jurisdiction", "TN");
    form.append("document_family", "REGLEMENTAIRE");
    const result = await api.createRun(form);
    jobs.push({ file_name: file.name, job_id: result.job_id });
  }
  return jobs;
}

async function importQueuedDatasets(tenantId) {
  const imported = [];
  const warnings = [];
  for (const item of state.importFiles) {
    try {
      const form = new FormData();
      form.append("tenant_id", tenantId);
      form.append("dataset_type", item.dataset_type);
      form.append("import_file", item.file);
      const result = await api.importCompanyDataset(form);
      imported.push({
        dataset_type: item.dataset_type,
        label: item.label,
        file_name: item.file.name,
        report: result.report || {},
      });
      const itemWarnings = Array.isArray(result.report?.warnings) ? result.report.warnings : [];
      itemWarnings.forEach((warning) => {
        warnings.push(`${item.file.name}: ${warning}`);
      });
    } catch (error) {
      warnings.push(`Import ${item.file.name} en echec: ${error.message || error}`);
    }
  }
  return { imported, warnings };
}

async function hydrateImportTypes() {
  if (!refs.importDatasetType) {
    return;
  }
  try {
    const response = await api.companyImportTypes();
    const items = Array.isArray(response?.items) ? response.items : [];
    if (!items.length) {
      return;
    }
    refs.importDatasetType.innerHTML = items.map((item) => {
      const datasetType = String(item.dataset_type || "").trim();
      const label = String(item.label || datasetType).trim();
      return `<option value="${escapeHtml(datasetType)}">${escapeHtml(label)}</option>`;
    }).join("");
  } catch {
  }
}

async function submitOnboarding() {
  if (state.submitting) {
    return;
  }
  let payload;
  try {
    payload = validateIdentity();
  } catch (error) {
    showStatus(error.message || "Validation impossible.", "warning");
    return;
  }

  state.submitting = true;
  syncWizard();
  showStatus("Création du tenant et du profil entreprise en cours...", "info");

  try {
    const created = await api.onboardCompany(payload);
    showStatus("Entreprise créée. Bascule du contexte actif...", "info");

    const switched = await api.switchTenant(created.tenant_id);
    saveAuth(switched);

    let importedDatasets = [];
    let uploadedProofs = [];
    let launchedRuns = [];
    const warnings = [];

    if (state.importFiles.length) {
      try {
        showStatus(`Import de ${state.importFiles.length} fichier(s) CSV/XLSX...`, "info");
        const importedResult = await importQueuedDatasets(created.tenant_id);
        importedDatasets = importedResult.imported;
        warnings.push(...importedResult.warnings);
      } catch (error) {
        warnings.push(`Imports bulk non entierement appliques: ${error.message || error}`);
      }
    }

    if (state.proofFiles.length) {
      try {
        showStatus(`Upload de ${state.proofFiles.length} preuve(s) documentaire(s)...`, "info");
        uploadedProofs = await uploadProofs(created.tenant_id);
      } catch (error) {
        warnings.push(`Preuves non entièrement uploadées: ${error.message || error}`);
      }
    }

    if (state.regulatoryFiles.length) {
      try {
        showStatus(`Lancement de ${state.regulatoryFiles.length} traitement(s) d'extraction...`, "info");
        launchedRuns = await launchRegulatoryRuns(created.tenant_id);
      } catch (error) {
        warnings.push(`Documents réglementaires non entièrement lancés: ${error.message || error}`);
      }
    }

    if (warnings.length) {
      showStatus(
        "Entreprise créée avec avertissements.",
        "warning",
        `
          <div>${warnings.map((item) => `<div>${escapeHtml(item)}</div>`).join("")}</div>
          <div style="margin-top:8px">${importedDatasets.length} import(s) bulk traite(s), ${uploadedProofs.length} preuve(s) uploadee(s), ${launchedRuns.length} traitement(s) d'extraction lance(s).</div>
          <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
            <a class="btn btn-primary" href="/ui/company.html">Ouvrir la fiche entreprise</a>
            <a class="btn btn-secondary" href="/ui/upload.html">Voir Documents & traitements</a>
          </div>
        `,
      );
      return;
    }

    showStatus(
      "Entreprise créée et contexte basculé avec succès.",
      "success",
      `
        <div>${importedDatasets.length} import(s) bulk traite(s).</div>
        <div>${uploadedProofs.length} preuve(s) uploadee(s) et ${launchedRuns.length} traitement(s) d'extraction lance(s).</div>
        <div style="margin-top:10px"><a class="btn btn-primary" href="/ui/company.html">Ouvrir la fiche entreprise</a></div>
      `,
    );
    window.setTimeout(() => {
      window.location.href = "/ui/company.html";
    }, 1400);
  } catch (error) {
    showStatus(error.message || "Échec de l’onboarding.", "error");
  } finally {
    state.submitting = false;
    syncWizard();
  }
}

refs.companyName.addEventListener("blur", autoFillTenant);
refs.chemicalsText.addEventListener("input", renderChemicalsPreview);
refs.proofFiles.addEventListener("change", () => {
  state.proofFiles = Array.from(refs.proofFiles.files || []).slice(0, 1);
  if ((refs.proofFiles.files || []).length > 1) {
    showStatus("L'onboarding accepte un seul PDF d'audit / preuve initiale. Les autres documents de conformite pourront etre ajoutes apres la creation de l'entreprise.", "warning");
  } else {
    clearStatus();
  }
  renderUploads();
});
refs.regFiles.addEventListener("change", () => {
  state.regulatoryFiles = Array.from(refs.regFiles.files || []);
  renderUploads();
});

refs.rail.forEach((button) => {
  button.addEventListener("click", () => {
    if (state.submitting) {
      return;
    }
    moveToStep(Number(button.dataset.step));
  });
});

refs.prev.addEventListener("click", () => moveToStep(state.step - 1));
refs.next.addEventListener("click", () => {
  if (state.step === 0) {
    try {
      validateIdentity();
    } catch (error) {
      showStatus(error.message || "Validation impossible.", "warning");
      return;
    }
  }
  moveToStep(state.step + 1);
});
refs.submit.addEventListener("click", submitOnboarding);

document.getElementById("add-site").addEventListener("click", addSite);
document.getElementById("add-process").addEventListener("click", addProcess);
document.getElementById("add-activity").addEventListener("click", addActivity);
document.getElementById("add-product").addEventListener("click", addProduct);
document.getElementById("add-import-file").addEventListener("click", addImportFile);

renderAllCollections();
renderChemicalsPreview();
renderUploads();
syncWizard();
hydrateImportTypes();
