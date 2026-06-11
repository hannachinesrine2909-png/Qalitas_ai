import { getAuth, initShell, isReadOnlyRole, requireRole } from "/ui/js/auth.js";
import { api } from "/ui/js/api.js";
import {
  escapeHtml,
  formatDate,
  numberFmt,
  openBlob,
  renderEmpty,
  renderError,
  statusBadge,
  toggleModal,
  truncate,
} from "/ui/js/utils.js";

const session = initShell("compliance");
if (!session) {
  throw new Error("Session absente");
}

const refs = {
  tableSection: document.getElementById("compliance-table-section"),
  gaps: document.getElementById("gaps-list"),
  actions: document.getElementById("actions-list"),
  btnExport: document.getElementById("btn-export-pdf"),
  btnRunA3: document.getElementById("btn-run-a3"),
  statusChips: document.querySelectorAll("#compliance-status-chips [data-filter]"),
  search: document.getElementById("compliance-search"),
  kpiCards: document.querySelectorAll("[data-kpi-card]"),
  proofHint: document.getElementById("proof-ratio-hint"),
  banner: document.getElementById("compliance-context-banner"),

  drawer: document.getElementById("compliance-drawer"),
  drawerContent: document.getElementById("compliance-drawer-content"),
  drawerClose: document.getElementById("close-compliance-drawer"),

  modal: document.getElementById("proof-modal"),
  modalTarget: document.getElementById("proof-target"),
  proofGuidance: document.getElementById("proof-guidance"),
  proofFile: document.getElementById("proof-file"),
  proofReference: document.getElementById("proof-reference"),
  proofType: document.getElementById("proof-type"),
  proofDomain: document.getElementById("proof-domain"),
  proofSubmit: document.getElementById("proof-submit"),
  closeProof: document.getElementById("close-proof-modal"),
  cancelProof: document.getElementById("proof-cancel"),
};

const CLOSED_ACTION_STATES = new Set(["REALISEE", "CLOTUREE", "ANNULEE"]);

const state = {
  summary: null,
  rows: [],
  filteredRows: [],
  rowFilter: "ALL",
  search: "",
  selected: null,
  selectedComplianceKey: "",
};

function renderContextBanner(message, tone = "info") {
  if (!refs.banner) {
    return;
  }
  if (!message || tone === "success") {
    refs.banner.classList.add("hidden");
    return;
  }
  refs.banner.classList.remove("hidden");
  refs.banner.textContent = message;
  if (tone === "danger") {
    refs.banner.style.background = "#fff1f2";
    refs.banner.style.borderColor = "rgba(220, 38, 38, 0.45)";
    refs.banner.style.color = "#991b1b";
    return;
  }
  if (tone === "success") {
    refs.banner.style.background = "#f0fdf4";
    refs.banner.style.borderColor = "rgba(22, 163, 74, 0.35)";
    refs.banner.style.color = "#166534";
    return;
  }
  refs.banner.style.background = "#fffbeb";
  refs.banner.style.borderColor = "rgba(245, 158, 11, 0.6)";
  refs.banner.style.color = "#92400e";
}

function complianceDocKey(suffix) {
  return `qalitas.${suffix}.${getAuth().tenant_id}`;
}

function parseRunTimeMs(value) {
  const timestamp = Date.parse(String(value || ""));
  return Number.isNaN(timestamp) ? 0 : timestamp;
}

function rememberComplianceDocContext({ docId = "", fileName = "", source = "" } = {}) {
  const safeDocId = String(docId || "").trim();
  const safeFileName = String(fileName || "").trim();
  const safeSource = String(source || "").trim();
  if (safeDocId) {
    localStorage.setItem(complianceDocKey("last_doc_id"), safeDocId);
  }
  if (safeFileName) {
    localStorage.setItem(complianceDocKey("last_doc_name"), safeFileName);
  }
  if (safeSource) {
    localStorage.setItem(complianceDocKey("last_doc_source"), safeSource);
  }
}

async function resolveLatestExtractionDocContext() {
  const tenant = getAuth().tenant_id;
  const cachedDocId = String(localStorage.getItem(complianceDocKey("last_doc_id")) || "").trim();
  const cachedFileName = String(localStorage.getItem(complianceDocKey("last_doc_name")) || "").trim();
  const data = await api.listRuns(tenant, 60);
  const rows = (Array.isArray(data?.items) ? data.items : [])
    .slice()
    .sort((a, b) => parseRunTimeMs(b?.updated_at || b?.created_at) - parseRunTimeMs(a?.updated_at || a?.created_at));

  const preferred = rows.find((row) => {
    const type = String(row?.type || "").toLowerCase();
    const status = String(row?.status || "").toUpperCase();
    return ["applicability", "compliance"].includes(type)
      && ["PAUSED", "RUNNING", "PENDING", "DONE"].includes(status)
      && String(row?.doc_id || "").trim();
  }) || rows.find((row) => {
    const type = String(row?.type || "extraction").toLowerCase();
    const status = String(row?.status || "").toUpperCase();
    return (type === "extraction" || type === "")
      && ["DONE", "PAUSED", "RUNNING"].includes(status)
      && String(row?.doc_id || "").trim();
  });

  const docId = String(preferred?.doc_id || "").trim() || cachedDocId;
  const fileName = String(preferred?.file_name || "").trim() || cachedFileName;
  if (docId || fileName) {
    rememberComplianceDocContext({
      docId,
      fileName,
      source: String(preferred?.type || "").trim() || "cache",
    });
  }
  return { docId, fileName };
}

function norm(value) {
  return String(value || "").trim().toUpperCase();
}

function complianceKey(item) {
  return `${item?.requirement_id || ""}::${item?.scope_key || "ORGANIZATION"}`;
}

function scopeText(item) {
  const level = norm(item?.scope_level || "ORGANIZATION");
  const label = String(item?.scope_label || item?.scope_site || "ORGANIZATION");
  return `${level} | ${label}`;
}

function setKpi(key, value) {
  const node = document.querySelector(`[data-kpi='${key}']`);
  if (node) {
    node.textContent = value;
  }
}

function setKpiMeta(key, value) {
  const node = document.querySelector(`[data-kpi='${key}']`);
  if (node) {
    node.textContent = value;
  }
}

function complianceStatusBadge(status) {
  const s = norm(status);
  if (s === "ABSENCE_DE_PREUVE") return `<span class="badge badge-amber">Absence preuve</span>`;
  if (s === "NON_CONFORME") return `<span class="badge badge-nc">Non conforme</span>`;
  if (s === "PARTIELLEMENT_CONFORME" || s === "PARTIEL") return `<span class="badge badge-partiel">Partiel</span>`;
  if (s === "CONFORME") return `<span class="badge badge-conforme">Conforme</span>`;
  return statusBadge(status);
}

function severityBadge(severity) {
  const s = norm(severity);
  if (s === "CRITIQUE") return `<span class="badge badge-nc">Critique</span>`;
  if (s === "MAJEURE") return `<span class="badge badge-amber">Majeure</span>`;
  if (s === "MINEURE") return `<span class="badge badge-partiel">Mineure</span>`;
  return `<span class="badge badge-absence">-</span>`;
}

function proofTextNorm(value) {
  return String(value || "")
    .trim()
    .toLowerCase()
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .replace(/[’']/g, " ")
    .replace(/\s+/g, " ");
}

function isNegativeProofSentence(value) {
  const raw = proofTextNorm(value);
  if (!raw) return true;
  return [
    "aucune preuve",
    "aucun document",
    "aucun registre",
    "aucune trace",
    "aucune donnee",
    "aucune information",
    "pas de preuve",
    "preuve manquante",
    "preuve introuvable",
    "non trouvee",
    "non trouvees",
    "n a ete trouvee",
    "n a ete trouve",
    "n ont ete trouvees",
    "n est disponible",
    "n est jointe",
    "aucune piece",
  ].some((marker) => raw.includes(marker));
}

function splitProofItems(text) {
  const raw = String(text || "").trim();
  if (!raw || isNegativeProofSentence(raw)) return [];
  const normalized = raw.replace(/\r/g, "\n").replace(/[•·]/g, "\n").replace(/[;|]/g, "\n");
  const items = normalized
    .split("\n")
    .map((part) => part.trim().replace(/^[-:\s]+/, ""))
    .filter((part) => part && part.length >= 4 && !isNegativeProofSentence(part));
  const out = [];
  const seen = new Set();
  items.forEach((item) => {
    const key = item.toLowerCase();
    if (!seen.has(key)) {
      seen.add(key);
      out.push(item);
    }
  });
  return out;
}

function proofProgress(item) {
  const builtIn = item?.proof_progress || {};
  const freshness = item?.evidence_freshness || {};
  const expectedRaw = String(item?.expected_proofs || "").trim();
  const foundRaw = String(item?.found_proofs || "").trim();
  const missingRaw = String(item?.missing_proofs || "").trim();
  const expectedItems = splitProofItems(expectedRaw);
  const foundItems = splitProofItems(foundRaw);
  const missingItems = splitProofItems(missingRaw);
  let expected = Number(builtIn.expected || 0);
  let found = Number(builtIn.found || 0);
  let missing = Number(builtIn.missing || 0);

  if (expectedItems.length > 0 || expectedRaw) {
    expected = expectedItems.length;
  }
  if (foundItems.length > 0 || (foundRaw && isNegativeProofSentence(foundRaw))) {
    found = foundItems.length;
  }
  if (missingItems.length > 0 || (missingRaw && isNegativeProofSentence(missingRaw))) {
    missing = missingItems.length;
  }

  if (expected <= 0) {
    expected = expectedItems.length;
  }
  if (found <= 0) {
    found = foundItems.length;
  }
  if (missing <= 0) {
    missing = missingItems.length;
  }

  if (expected <= 0) {
    expected = Math.max(found, missing);
  }
  if (expected <= 0 && norm(item?.status) === "ABSENCE_DE_PREUVE") {
    expected = 1;
  }
  expected = Math.max(expected, found);
  const effectiveMissing = missing > 0 ? missing : Math.max(0, expected - found);
  const pct = expected > 0 ? Math.round((found / expected) * 100) : (norm(item?.status) === "CONFORME" ? 100 : 0);

  return {
    expected,
    found,
    missing: effectiveMissing,
    pct: Math.max(0, Math.min(100, pct)),
    fresh: Number(freshness.fresh_count || 0),
    expired: Number(freshness.expired_count || 0),
    undated: Number(freshness.undated_count || 0),
    onlyExpired: Boolean(freshness.only_expired),
  };
}

function topSeverity(item) {
  const gaps = Array.isArray(item?.gaps) ? item.gaps : [];
  if (!gaps.length) {
    const s = norm(item?.status);
    if (s === "NON_CONFORME" || s === "ABSENCE_DE_PREUVE") return "CRITIQUE";
    if (s === "PARTIELLEMENT_CONFORME") return "MAJEURE";
    return "MINEURE";
  }
  if (gaps.some((g) => norm(g.severity) === "CRITIQUE")) return "CRITIQUE";
  if (gaps.some((g) => norm(g.severity) === "MAJEURE")) return "MAJEURE";
  return "MINEURE";
}

function matchesRowFilter(item) {
  const f = norm(state.rowFilter);
  if (f === "ALL") return true;
  if (f === "NC_REGLEMENTAIRE") {
    return (item.gaps || []).some((g) => norm(g.gap_type) === "NC_REGLEMENTAIRE");
  }
  if (f === "ACTIONS") {
    return (item.actions || []).some((a) => !CLOSED_ACTION_STATES.has(norm(a.state)));
  }
  return norm(item.status) === f;
}

function matchesSearch(item) {
  const q = String(state.search || "").trim().toLowerCase();
  if (!q) return true;
  const blob = [
    item.requirement_text,
    item.requirement,
    item.scope_label,
    item.scope_level,
    item.scope_site,
    item.scope_process,
    item.scope_activity,
    item.qse_domain,
    item.qse_sub_domain,
    item.citation_ref,
    item.citation_snippet,
    item.article_ref,
    item.doc_title,
    item.doc_source,
  ]
    .map((x) => String(x || ""))
    .join(" ")
    .toLowerCase();
  return blob.includes(q);
}

function applyFilters() {
  state.filteredRows = state.rows.filter((item) => matchesRowFilter(item) && matchesSearch(item));
}

function syncFilterUi() {
  refs.statusChips.forEach((chip) => {
    const active = norm(chip.dataset.filter) === norm(state.rowFilter);
    chip.classList.toggle("badge-applicable", active);
  });
  refs.kpiCards.forEach((card) => {
    const active = norm(card.dataset.kpiCard) === norm(state.rowFilter);
    card.style.cursor = "pointer";
    card.style.boxShadow = active ? "inset 0 0 0 1px var(--blue-primary)" : "";
    card.style.transform = active ? "translateY(-1px)" : "";
  });
}

function tableSkeleton() {
  refs.tableSection.innerHTML = `
    <div class="card-head">
      <h2 class="card-title">Table de conformite</h2>
      <span class="page-subtitle" id="proof-ratio-hint">Preuves = trouvees / attendues, avec suivi des pieces expirees.</span>
    </div>
    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>Domaine</th>
            <th>Exigence</th>
            <th>Statut</th>
            <th>Preuves</th>
            <th>Severite</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody>
          ${Array.from({ length: 6 }).map(() => `<tr><td colspan="6"><div class="skeleton skeleton-line"></div></td></tr>`).join("")}
        </tbody>
      </table>
    </div>
  `;
}

function rowActionButton(item, idx) {
  if (isReadOnlyRole(session.role)) {
    return `<span class="page-subtitle">Lecture seule</span>`;
  }
  return `<button class="btn btn-secondary" data-upload="${idx}">Uploader preuve</button>`;
}

function renderTable() {
  if (!state.filteredRows.length) {
    renderEmpty(refs.tableSection, "Aucune exigence ne correspond aux filtres actifs.", "Reinitialiser", () => {
      state.rowFilter = "ALL";
      state.search = "";
      if (refs.search) refs.search.value = "";
      renderAll();
    });
    return;
  }

  refs.tableSection.innerHTML = `
    <div class="card-head">
      <h2 class="card-title">Table de conformite</h2>
      <span class="page-subtitle">Détail par exigence</span>
    </div>
    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>Domaine</th>
            <th>Exigence</th>
            <th>Statut</th>
            <th>Preuves</th>
            <th>Severite</th>
            <th>Action</th>
          </tr>
        </thead>
        <tbody id="compliance-body"></tbody>
      </table>
    </div>
  `;

  const body = document.getElementById("compliance-body");
  body.innerHTML = state.filteredRows
    .map((item, idx) => {
      const pp = proofProgress(item);
      const severity = topSeverity(item);
      const updated = formatDate(item.updated_at);
      return `
        <tr data-row="${idx}" style="cursor:pointer">
          <td>${escapeHtml(item.qse_domain || item.domain || "-")}</td>
          <td title="${escapeHtml(item.requirement_text || item.requirement || "")}">
            ${escapeHtml(truncate(item.requirement_text || item.requirement || "", 135))}
            <div class="page-subtitle">${escapeHtml(item.citation_ref || item.article_ref || "Reference non renseignee")}</div>
            <div class="page-subtitle">${escapeHtml(scopeText(item))}</div>
          </td>
          <td>
            ${complianceStatusBadge(item.status)}
            <div class="page-subtitle">Maj: ${escapeHtml(updated)}</div>
          </td>
          <td>
            <strong>${numberFmt(pp.found)} / ${numberFmt(pp.expected)}</strong>
            <div class="progress-track" style="margin-top:5px"><div class="progress-bar" style="width:${pp.pct}%"></div></div>
            <div class="page-subtitle" style="margin-top:4px">${numberFmt(pp.missing)} preuve(s) manquante(s)</div>
            ${pp.expired > 0 ? `<div class="page-subtitle">${numberFmt(pp.expired)} expiree(s)${pp.onlyExpired ? " | a renouveler" : ""}</div>` : ""}
          </td>
          <td>
            ${severityBadge(severity)}
            <div class="page-subtitle">${numberFmt((item.gaps || []).length)} ecart(s)</div>
          </td>
          <td>${rowActionButton(item, idx)}</td>
        </tr>
      `;
    })
    .join("");

  body.querySelectorAll("tr[data-row]").forEach((tr) => {
    tr.addEventListener("click", () => {
      const idx = Number(tr.dataset.row);
      const row = state.filteredRows[idx];
      if (row) openDrawer(row);
    });
  });

  body.querySelectorAll("button[data-upload]").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      event.stopPropagation();
      const idx = Number(btn.dataset.upload);
      const row = state.filteredRows[idx];
      if (row) openProofModal(row);
    });
  });

  requireRole(session.role);
}

function getRowByComplianceKey(key) {
  if (!key) return null;
  return state.rows.find((r) => complianceKey(r) === String(key)) || null;
}

function openDrawer(item) {
  state.selectedComplianceKey = complianceKey(item);
  const pp = proofProgress(item);
  const gaps = Array.isArray(item.gaps) ? item.gaps : [];
  const actions = Array.isArray(item.actions) ? item.actions : [];
  const freshnessText = pp.expired > 0
    ? `${numberFmt(pp.fresh)} valide(s), ${numberFmt(pp.expired)} expiree(s), ${numberFmt(pp.undated)} sans date`
    : `${numberFmt(pp.fresh)} valide(s), ${numberFmt(pp.undated)} sans date`;

  const gapsHtml = gaps.length
    ? gaps
        .map(
          (g) => `
            <div class="alert-item ${norm(g.severity) === "CRITIQUE" ? "alert-red" : ""}">
              <div style="display:flex;align-items:center;justify-content:space-between;gap:6px">
                <strong>${escapeHtml(g.gap_type || "Ecart")}</strong>
                ${severityBadge(g.severity)}
              </div>
              <div class="page-subtitle" style="margin-top:4px">${escapeHtml(g.description || "-")}</div>
              <div class="page-subtitle" style="margin-top:4px">Priorite: ${escapeHtml(g.treatment_priority || "-")}</div>
            </div>
          `,
        )
        .join("")
    : `<div class="state">Aucun ecart enregistre pour cette exigence.</div>`;

  const actionsHtml = actions.length
    ? actions
        .map(
          (a) => `
            <div class="pipeline-item" style="align-items:flex-start;flex-direction:column;gap:4px">
              <div style="display:flex;align-items:center;justify-content:space-between;width:100%">
                <strong>${escapeHtml(a.action_title || "Action")}</strong>
                ${statusBadge(a.state || "PLANIFIEE")}
              </div>
              <div class="page-subtitle">Resp: ${escapeHtml(a.responsible || "A definir")}</div>
              <div class="page-subtitle">Echeance: ${escapeHtml(a.due_date || "-")}</div>
              <div class="page-subtitle">Preuve attendue: ${escapeHtml(a.expected_proof || "-" )}</div>
            </div>
          `,
        )
        .join("")
    : `<div class="state">Aucune action corrective liee.</div>`;

  refs.drawerContent.innerHTML = `
    <div><strong>Statut conformite:</strong> ${complianceStatusBadge(item.status)}</div>
    <div style="margin-top:8px"><strong>Perimetre:</strong> ${escapeHtml(scopeText(item))}</div>
    <div style="margin-top:8px"><strong>Scope cle:</strong> ${escapeHtml(item.scope_key || "ORGANIZATION")}</div>
    <div style="margin-top:8px"><strong>Domaine:</strong> ${escapeHtml(item.qse_domain || "-")} / ${escapeHtml(item.qse_sub_domain || "-")}</div>
    <div style="margin-top:8px"><strong>Type exigence:</strong> ${escapeHtml(item.req_type || "-")}</div>
    <div style="margin-top:8px"><strong>Source:</strong> ${escapeHtml(item.doc_title || "-")} (${escapeHtml(item.doc_source || "-")})</div>
    <div style="margin-top:8px"><strong>Article/Citation:</strong> ${escapeHtml(item.article_ref || item.citation_ref || "-")}</div>
    <div style="margin-top:8px"><strong>Applicabilite:</strong> ${statusBadge(item.applicability_status || "-")}</div>
    <div style="margin-top:8px"><strong>Score conformite:</strong> ${Math.round(Number(item.score || 0) * 100)}%</div>

    <div style="margin-top:12px"><strong>Texte exigence:</strong><br>${escapeHtml(item.requirement_text || "-")}</div>
    <div style="margin-top:8px"><strong>Extrait cite:</strong><br>${escapeHtml(item.citation_snippet || "-")}</div>

    <div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--border)">
      <strong>Preuves</strong>
      <div style="margin-top:6px">${numberFmt(pp.found)} / ${numberFmt(pp.expected)} trouvees</div>
      <div class="progress-track" style="margin-top:5px"><div class="progress-bar" style="width:${pp.pct}%"></div></div>
      <div class="page-subtitle" style="margin-top:6px">Fraicheur: ${escapeHtml(freshnessText)}</div>
      <div style="margin-top:8px"><strong>Preuves attendues:</strong><br>${escapeHtml(item.expected_proofs || "-")}</div>
      <div style="margin-top:8px"><strong>Preuves trouvees:</strong><br>${escapeHtml(item.found_proofs || "Aucune")}</div>
      <div style="margin-top:8px"><strong>Preuves manquantes:</strong><br>${escapeHtml(item.missing_proofs || "-")}</div>
      <div style="margin-top:8px"><strong>Analyse IA:</strong><br>${escapeHtml(item.analysis_detail || "-")}</div>
      ${
        isReadOnlyRole(session.role)
          ? ""
          : `<button id="drawer-upload-proof" class="btn btn-secondary" style="margin-top:10px">Uploader une preuve pour cette exigence</button>`
      }
    </div>

    <div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--border)">
      <strong>Ecarts lies</strong>
      <div style="display:grid;gap:8px;margin-top:8px">${gapsHtml}</div>
    </div>

    <div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--border)">
      <strong>Actions correctives liees</strong>
      <div style="display:grid;gap:8px;margin-top:8px">${actionsHtml}</div>
    </div>
  `;

  const drawerBtn = document.getElementById("drawer-upload-proof");
  if (drawerBtn) {
    drawerBtn.addEventListener("click", () => openProofModal(item));
  }

  refs.drawer.classList.add("is-open");
}

function renderGaps() {
  const rowsByKey = new Set(state.filteredRows.map((row) => complianceKey(row)));
  let items = Array.isArray(state.summary?.recent_gaps) ? [...state.summary.recent_gaps] : [];
  if (state.rowFilter !== "ALL") {
    items = items.filter((gap) => rowsByKey.has(`${gap.requirement_id || ""}::${gap.scope_key || "ORGANIZATION"}`));
  }
  if (norm(state.rowFilter) === "NC_REGLEMENTAIRE") {
    items = items.filter((gap) => norm(gap.gap_type) === "NC_REGLEMENTAIRE");
  }

  if (!items.length) {
    renderEmpty(refs.gaps, "Aucun ecart pour le filtre actif.", "Afficher tout", () => {
      state.rowFilter = "ALL";
      renderAll();
    });
    return;
  }

  refs.gaps.innerHTML = items.slice(0, 14).map((gap) => `
    <div class="alert-item ${norm(gap.severity) === "CRITIQUE" ? "alert-red" : ""}" data-gap-key="${escapeHtml(`${gap.requirement_id || ""}::${gap.scope_key || "ORGANIZATION"}`)}" style="cursor:pointer">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
        <div style="font-weight:600">${escapeHtml(gap.gap_type || "Ecart")}</div>
        ${severityBadge(gap.severity)}
      </div>
      <div class="page-subtitle" style="margin-top:4px">${escapeHtml(gap.qse_domain || "-")} | ${escapeHtml(truncate(gap.description || "", 110))}</div>
    </div>
  `).join("");

  refs.gaps.querySelectorAll("[data-gap-key]").forEach((node) => {
    node.addEventListener("click", () => {
      const key = node.getAttribute("data-gap-key");
      const row = getRowByComplianceKey(key);
      if (row) openDrawer(row);
    });
  });
}

function renderActions() {
  const rowsByKey = new Set(state.filteredRows.map((row) => complianceKey(row)));
  let items = Array.isArray(state.summary?.recent_actions) ? [...state.summary.recent_actions] : [];
  if (state.rowFilter !== "ALL") {
    items = items.filter((act) => rowsByKey.has(`${act.requirement_id || ""}::${act.scope_key || "ORGANIZATION"}`));
  }
  if (norm(state.rowFilter) === "ACTIONS") {
    items = items.filter((act) => !CLOSED_ACTION_STATES.has(norm(act.state)));
  }

  if (!items.length) {
    renderEmpty(refs.actions, "Aucune action corrective pour le filtre actif.", "Afficher tout", () => {
      state.rowFilter = "ALL";
      renderAll();
    });
    return;
  }

  refs.actions.innerHTML = items.slice(0, 14).map((action) => {
    const pct = Math.max(0, Math.min(100, Number(action.completion_pct || 0)));
    return `
      <div class="pipeline-item" data-action-key="${escapeHtml(`${action.requirement_id || ""}::${action.scope_key || "ORGANIZATION"}`)}" style="cursor:pointer;display:grid;gap:5px">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
          <strong>${escapeHtml(truncate(action.action_title || "Action corrective", 48))}</strong>
          ${statusBadge(action.state || "PLANIFIEE")}
        </div>
        <div class="page-subtitle">${escapeHtml(action.qse_domain || "-")} | Resp: ${escapeHtml(action.responsible || "A definir")}</div>
        <div class="page-subtitle">Echeance: ${escapeHtml(action.due_date || "-")}</div>
        <div class="progress-track"><div class="progress-bar" style="width:${pct}%"></div></div>
      </div>
    `;
  }).join("");

  refs.actions.querySelectorAll("[data-action-key]").forEach((node) => {
    node.addEventListener("click", () => {
      const key = node.getAttribute("data-action-key");
      const row = getRowByComplianceKey(key);
      if (row) openDrawer(row);
    });
  });
}

function renderAll() {
  applyFilters();
  syncFilterUi();
  renderTable();
  renderGaps();
  renderActions();
}

function applySummaryToKpis(summary) {
  const totalChecks = Number(summary.total_checks || 0);
  const rate = Math.round(Number(summary.compliance_rate || 0) * 100);
  const nc = summary.nc_reglementaire || {};
  const actionsCount = Object.values(summary.actions_breakdown || {}).reduce((acc, val) => acc + Number(val || 0), 0);

  setKpi("total", numberFmt(totalChecks));
  setKpi("rate", `${rate}%`);
  setKpi("nc", numberFmt(nc.total || 0));
  setKpiMeta("nc-breakdown", `Majeures ${numberFmt(nc.major || 0)} | Mineures ${numberFmt(nc.minor || 0)} | Critiques ${numberFmt(nc.critical || 0)}`);
  setKpi("actions", numberFmt(actionsCount));
}

async function loadCompliance() {
  tableSkeleton();
  refs.gaps.innerHTML = `<div class="skeleton skeleton-line"></div><div class="skeleton skeleton-line"></div>`;
  refs.actions.innerHTML = `<div class="skeleton skeleton-line"></div><div class="skeleton skeleton-line"></div>`;

  try {
    const tenant = getAuth().tenant_id;
    const [summary, overview, applicability] = await Promise.all([
      api.complianceSummary(tenant),
      api.dashboardOverview(tenant),
      api.applicabilitySummary(tenant),
    ]);
    state.summary = summary;
    state.rows = Array.isArray(summary.worst_items) ? summary.worst_items : [];

    const requirementsTotal = Number(overview?.requirements_total || 0);
    const complianceTotal = Number(summary?.total_checks || 0);
    const applicableTotal = Number(applicability?.counts?.APPLICABLE || 0)
      + Number(applicability?.counts?.APPLICABLE_SOUS_CONDITIONS || 0);

    if (requirementsTotal === 0 && complianceTotal > 0) {
      renderContextBanner(
        "Résultats A3 hérités sans corpus A1 actif.",
        "danger",
      );
    } else if (requirementsTotal === 0) {
      renderContextBanner(
        "Aucune exigence A1 active.",
        "info",
      );
    } else if (applicableTotal === 0) {
      renderContextBanner(
        "Aucune exigence applicable prête pour A3.",
        "info",
      );
    } else {
      renderContextBanner(
        "",
        "success",
      );
    }

    applySummaryToKpis(summary);

    if (!state.rows.length) {
      const message = requirementsTotal === 0
        ? "Aucune analyse de conformité disponible: le corpus A1 est vide."
        : applicableTotal === 0
          ? "Aucune analyse de conformité disponible: A2 ne fournit pas encore d'exigences applicables."
          : "Aucune analyse de conformité disponible. Relance A3 après la reconstruction du corpus.";
      renderEmpty(refs.tableSection, message, "Ouvrir Upload & runs", () => {
        window.location.href = "/ui/upload.html";
      });
      renderEmpty(refs.gaps, "Aucun écart à afficher tant que la conformité n'a pas été recalculée.", "Actualiser", loadCompliance);
      renderEmpty(refs.actions, "Aucune action corrective disponible pour le moment.", "Actualiser", loadCompliance);
      return;
    }
    renderAll();

    if (state.selectedComplianceKey) {
      const refreshed = getRowByComplianceKey(state.selectedComplianceKey);
      if (refreshed) {
        openDrawer(refreshed);
      }
    }
  } catch (error) {
    renderError(refs.tableSection, error, "Reessayer", loadCompliance);
    renderError(refs.gaps, error, "Reessayer", loadCompliance);
    renderError(refs.actions, error, "Reessayer", loadCompliance);
  }
}

function openProofModal(item) {
  state.selected = item;
  refs.modalTarget.textContent = `Exigence: ${truncate(item?.requirement_text || item?.requirement || "-", 120)}`;
  refs.proofDomain.value = item?.qse_domain || item?.domain || "HSE";
  refs.proofReference.value = `${item?.citation_ref || item?.article_ref || "preuve"}_${Date.now()}`;
  refs.proofGuidance.textContent = `Preuve attendue: ${truncate(item?.missing_proofs || item?.expected_proofs || "Document date, signe et tracable", 240)}`;
  toggleModal("proof-modal", true);
}

function closeProofModal() {
  toggleModal("proof-modal", false);
  refs.proofFile.value = "";
}

refs.drawerClose?.addEventListener("click", () => {
  refs.drawer.classList.remove("is-open");
});

refs.search?.addEventListener("input", () => {
  state.search = refs.search.value || "";
  renderAll();
});

refs.statusChips.forEach((chip) => {
  chip.addEventListener("click", () => {
    state.rowFilter = norm(chip.dataset.filter || "ALL");
    renderAll();
  });
});

refs.kpiCards.forEach((card) => {
  const applyCardFilter = () => {
    const key = norm(card.dataset.kpiCard || "ALL");
    state.rowFilter = key || "ALL";
    renderAll();
    if (key === "NC_REGLEMENTAIRE") {
      refs.gaps.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
    if (key === "ACTIONS") {
      refs.actions.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  };
  card.addEventListener("click", applyCardFilter);
  card.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      applyCardFilter();
    }
  });
});

refs.btnExport.addEventListener("click", async () => {
  refs.btnExport.disabled = true;
  const text = refs.btnExport.textContent;
  refs.btnExport.textContent = "Export...";
  try {
    const blob = await api.downloadCompliancePdf(getAuth().tenant_id);
    openBlob(blob, `compliance_${getAuth().tenant_id}.pdf`);
  } catch (error) {
    alert(error.message || "Export impossible");
  } finally {
    refs.btnExport.disabled = false;
    refs.btnExport.textContent = text;
  }
});

refs.btnRunA3?.addEventListener("click", async () => {
  refs.btnRunA3.disabled = true;
  const label = refs.btnRunA3.textContent;
  refs.btnRunA3.textContent = "Lancement...";
  try {
    const payload = { tenant_id: getAuth().tenant_id };
    const docContext = await resolveLatestExtractionDocContext();
      if (docContext.docId) {
        payload.doc_id = docContext.docId;
      }
      payload.mode = "full";
      payload.force = true;
      const run = await api.runCompliance(payload);
    if (docContext.docId) {
      const targetedLabel = docContext.fileName ? `${docContext.fileName} (${docContext.docId})` : docContext.docId;
      alert(`Analyse relancee: ${run.job_id}\nDocument cible: ${targetedLabel}`);
    } else {
      alert(`Analyse relancee: ${run.job_id}\nAucun doc_id recent trouve, scope tenant utilise.`);
    }
  } catch (error) {
    alert(error.message || "Impossible de relancer l'analyse");
  } finally {
    refs.btnRunA3.disabled = false;
    refs.btnRunA3.textContent = label;
  }
});

refs.proofSubmit.addEventListener("click", async () => {
  const file = refs.proofFile.files?.[0];
  if (!file) {
    alert("Selectionner un PDF de preuve.");
    return;
  }
  refs.proofSubmit.disabled = true;
  const label = refs.proofSubmit.textContent;
  refs.proofSubmit.textContent = "Upload...";
  try {
    const form = new FormData();
    form.append("proof_pdf", file);
    form.append("tenant_id", getAuth().tenant_id);
    form.append("reference", refs.proofReference.value || `proof_${Date.now()}`);
    form.append("audit_type", refs.proofType.value || "preuve_generale");
    form.append("category", "compliance");
    form.append("nature", "proof");
    form.append("system_scope", refs.proofDomain.value || state.selected?.qse_domain || "HSE");
    form.append("state", "ACTIVE");
    form.append("requirement_id", state.selected?.requirement_id || "");
    form.append("scope_level", state.selected?.scope_level || "ORGANIZATION");
    form.append("scope_label", state.selected?.scope_label || state.selected?.scope_site || "ORGANIZATION");
    form.append("site_id", state.selected?.site_id || "");
    form.append("process_id", state.selected?.process_id || "");
    form.append("activity_id", state.selected?.activity_id || "");

    await api.uploadCompanyProof(form);
    closeProofModal();
    await loadCompliance();
  } catch (error) {
    alert(error.message || "Upload impossible");
  } finally {
    refs.proofSubmit.disabled = false;
    refs.proofSubmit.textContent = label;
  }
});

refs.closeProof.addEventListener("click", closeProofModal);
refs.cancelProof.addEventListener("click", closeProofModal);
refs.modal.addEventListener("click", (event) => {
  if (event.target === refs.modal) {
    closeProofModal();
  }
});

await loadCompliance();


