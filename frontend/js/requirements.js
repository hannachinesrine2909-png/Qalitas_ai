import { getAuth, initShell, isReadOnlyRole, requireRole } from "/ui/js/auth.js";
import { api } from "/ui/js/api.js";
import {
  confidenceColor,
  confidenceText,
  domainBadge,
  escapeHtml,
  formatDate,
  numberFmt,
  renderEmpty,
  renderError,
  statusBadge,
  toggleModal,
  truncate,
  openBlob,
} from "/ui/js/utils.js";

const session = initShell("requirements");
if (!session) {
  throw new Error("Session absente");
}

const refs = {
  section: document.getElementById("requirements-section"),
  tbody: document.getElementById("requirements-body"),
  meta: document.getElementById("requirements-meta"),
  banner: document.getElementById("to-validate-banner"),
  statusCards: Array.from(document.querySelectorAll("#requirements-status-cards [data-status]")),
  statusCardCounts: Array.from(document.querySelectorAll("[data-req-card-count]")),
  filters: {
    search: document.getElementById("f-search"),
    domain: document.getElementById("f-domain"),
    type: document.getElementById("f-type"),
    severity: document.getElementById("f-severity"),
    status: document.getElementById("f-status"),
  },
  applyBtn: document.getElementById("btn-apply-filters"),
  exportBtn: document.getElementById("btn-export-csv"),
  modal: document.getElementById("validation-modal"),
  modalTarget: document.getElementById("validation-target"),
  modalContextRecommendation: document.getElementById("validation-context-recommendation"),
  modalContextMeta: document.getElementById("validation-context-meta"),
  modalContextSnippet: document.getElementById("validation-context-snippet"),
  modalContextRequirement: document.getElementById("validation-context-requirement"),
  modalContextStructure: document.getElementById("validation-context-structure"),
  modalContextGuidance: document.getElementById("validation-context-guidance"),
  modalContextSimilar: document.getElementById("validation-context-similar"),
  modalDecision: document.getElementById("validation-decision"),
  modalDecisionHint: document.getElementById("validation-decision-hint"),
  modalReasonField: document.getElementById("validation-reason-field"),
  modalReason: document.getElementById("validation-reason"),
  modalCorrectedField: document.getElementById("validation-corrected-field"),
  modalCorrectedText: document.getElementById("validation-corrected-text"),
  modalComment: document.getElementById("validation-comment"),
  modalClose: document.getElementById("close-validation-modal"),
  modalCancel: document.getElementById("validation-cancel"),
  modalConfirm: document.getElementById("validation-confirm"),
  drawer: document.getElementById("requirement-drawer"),
  drawerClose: document.getElementById("close-requirement-drawer"),
  drawerContent: document.getElementById("requirement-drawer-content"),
};

const state = {
  rows: [],
  filteredRows: [],
  selectedReqId: "",
  selectedDecision: "APPROVE",
  drawerReqId: "",
  drawerLoadToken: 0,
  overview: null,
  validationContext: {},
};

function humanizeToken(value) {
  const raw = String(value || "").trim();
  if (!raw) {
    return "";
  }
  const upper = raw.toUpperCase();
  const labels = {
    OBLIGATION: "Obligation",
    INTERDICTION: "Interdiction",
    CONDITION: "Condition",
    EXCEPTION: "Exception",
    DECLARATION: "Déclaration",
    REGISTRE: "Registre",
    CONTROLE: "Contrôle",
    RESPONSABILITE: "Responsabilité",
  };
  if (labels[upper]) {
    return labels[upper];
  }
  return raw
    .toLowerCase()
    .replace(/_/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

function refreshSelectOptions(selectEl, values, emptyLabel = "Tous") {
  if (!selectEl) {
    return;
  }
  const currentValue = selectEl.value;
  const uniqueValues = [...new Set((values || []).map((value) => String(value || "").trim()).filter(Boolean))].sort((a, b) => a.localeCompare(b, "fr"));
  selectEl.innerHTML = [`<option value="">${escapeHtml(emptyLabel)}</option>`]
    .concat(uniqueValues.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(humanizeToken(value))}</option>`))
    .join("");
  if (uniqueValues.includes(currentValue)) {
    selectEl.value = currentValue;
  }
}

function severityFromConfidence(conf) {
  const n = Number(conf || 0);
  if (n < 0.65) {
    return "HIGH";
  }
  if (n < 0.76) {
    return "MEDIUM";
  }
  return "LOW";
}

function severityBadge(level) {
  const norm = String(level || "LOW").toUpperCase();
  const map = {
    HIGH: "badge badge-nc",
    MEDIUM: "badge badge-amber",
    LOW: "badge badge-conforme",
  };
  const label = norm === "HIGH" ? "Haute" : norm === "MEDIUM" ? "Moyenne" : "Faible";
  return `<span class="${map[norm] || map.LOW}">${label}</span>`;
}

function renderSkeletonRows(count = 8) {
  refs.tbody.innerHTML = Array.from({ length: count }).map(() => `
    <tr>
      <td colspan="9"><div class="skeleton skeleton-line"></div></td>
    </tr>
  `).join("");
}

function currentFilters() {
  return {
    search: refs.filters.search.value.trim(),
    qse_domain: refs.filters.domain.value,
    req_type: refs.filters.type.value,
    status: refs.filters.status.value,
    severity: refs.filters.severity.value,
  };
}

function findRow(reqId) {
  return state.rows.find((row) => row.requirement_id === reqId) || null;
}

function syncFilterCatalog(rows) {
  const sourceRows = Array.isArray(rows) ? rows : [];
  refreshSelectOptions(refs.filters.domain, sourceRows.map((row) => row.qse_domain), "Tous");
  refreshSelectOptions(refs.filters.type, sourceRows.map((row) => row.req_type), "Tous");
}

function openValidation(reqId, decision, label) {
  const row = findRow(reqId);
  state.selectedReqId = reqId;
  state.selectedDecision = decision;
  refs.modalDecision.value = decision;
  refs.modalReason.value = "";
  refs.modalCorrectedText.value = row?.requirement_text || "";
  refs.modalComment.value = "";
  refs.modalTarget.textContent = `Exigence: ${label}`;
  refs.modalContextRecommendation.innerHTML = renderRecommendation(null);
  refs.modalContextMeta.textContent = "Chargement du contexte...";
  refs.modalContextSnippet.textContent = row?.citation_snippet || "Chargement...";
  refs.modalContextRequirement.textContent = row?.requirement_text || "Chargement...";
  refs.modalContextStructure.innerHTML = renderStructureCards(null);
  refs.modalContextGuidance.innerHTML = renderGuidanceItems([]);
  refs.modalContextSimilar.textContent = "Analyse en cours...";
  updateValidationDecisionUi();
  toggleModal("validation-modal", true);
  void loadValidationContext(reqId, { force: true }).then((context) => {
    if (!context || state.selectedReqId !== reqId) {
      return;
    }
    refs.modalContextRecommendation.innerHTML = renderRecommendation(context);
    refs.modalContextMeta.innerHTML = `
      <strong>${escapeHtml(humanizeToken(context.req_type || "-"))}</strong>
      - Force normative: ${escapeHtml(humanizeNormativeStrength(context.normative_strength))}
      - Blocage auto-promotion: ${escapeHtml(humanizeBlockedReason(context.promotion_blocked_reason))}
    `;
    refs.modalContextSnippet.textContent = context.source_snippet || "Aucun extrait source.";
    refs.modalContextRequirement.textContent = context.requirement_text || "Aucune formulation proposée.";
    refs.modalContextStructure.innerHTML = renderStructureCards(context.review_structure);
    refs.modalContextGuidance.innerHTML = renderGuidanceItems(context.review_guidance);
    refs.modalContextSimilar.innerHTML = renderSimilarExisting(context.similar_existing);
    if (decision === "EDIT" && context.review_recommended_decision === "APPROVE") {
      refs.modalDecision.value = "APPROVE";
      updateValidationDecisionUi();
    }
    if (!refs.modalCorrectedText.value.trim()) {
      refs.modalCorrectedText.value = context.requirement_text || "";
    }
  });
}

function closeValidation() {
  toggleModal("validation-modal", false);
  state.selectedReqId = "";
}

function closeDrawer() {
  refs.drawer?.classList.remove("is-open");
}

function renderField(label, value) {
  return `
    <div style="display:grid;grid-template-columns:120px 1fr;gap:8px;font-size:12px;margin-bottom:7px">
      <span style="color:var(--text-secondary)">${escapeHtml(label)}</span>
      <span>${value || "-"}</span>
    </div>
  `;
}

function humanizeNormativeStrength(value) {
  const norm = String(value || "").trim().toUpperCase();
  const labels = {
    IMPERATIF: "Impératif",
    CONDITIONNEL: "Conditionnel",
    FACULTATIF: "Facultatif",
  };
  return labels[norm] || value || "-";
}

function humanizeBlockedReason(value) {
  const norm = String(value || "").trim().toUpperCase();
  const labels = {
    PRE_VALIDATED_AUTO: "Pre-validee automatiquement, hors registre final tant qu'elle n'est pas confirmee",
    QUALITY_AND_GROUNDING_TOO_LOW: "Qualité et ancrage source insuffisants",
    QUALITY_TOO_LOW: "Qualité de formulation insuffisante",
    GROUNDING_TOO_WEAK: "Ancrage source trop faible",
    LOW_CONFIDENCE: "Confiance trop faible",
    POLICY_OR_TYPE: "Règle non promue automatiquement",
    TEXT_TOO_SHORT: "Texte trop court",
    TEXT_TOO_LONG: "Texte trop long",
    NO_LEGAL_SUBJECT: "Sujet juridique absent",
    NO_NORMATIVE_VERB: "Verbe normatif absent",
  };
  return labels[norm] || value || "-";
}

function deriveRegistryCounts(overview = null) {
  const source = overview || state.overview || {};
  return {
    total: Number(source.requirements_total || 0),
    promoted: Number(source.promoted_total || 0),
    draft: Number(source.draft_total || 0),
    toValidate: Number(source.to_validate_total || 0),
    reject: Number(source.reject_total || 0),
  };
}

function updateStatusCards(status = refs.filters.status?.value || "") {
  refs.statusCards.forEach((card) => {
    const isActive = String(card.dataset.status || "") === String(status || "");
    card.classList.toggle("is-active", isActive);
    card.setAttribute("aria-pressed", isActive ? "true" : "false");
  });
}

function updateStatusCardCounts(counts) {
  const map = {
    total: counts.total,
    promoted: counts.promoted,
    toValidate: counts.toValidate,
    draft: counts.draft,
    reject: counts.reject,
  };
  refs.statusCardCounts.forEach((node) => {
    const key = node.dataset.reqCardCount;
    node.textContent = numberFmt(map[key] || 0);
  });
}

function humanizeValidationFlag(value) {
  const norm = String(value || "").trim().toUpperCase();
  const labels = {
    HUMAN_EDITED: "Corrigée par validation humaine",
  };
  return labels[norm] || value || "-";
}

function humanizeSourceMode(value) {
  const norm = String(value || "").trim().toUpperCase();
  const labels = {
    NON_PRECISE: "Non précisé",
    VERBATIM: "Reprise quasi verbatim",
    REFORMULE_LEGERE: "Reformulation légère",
    RECONSTRUCTION_CONTROLEE: "Reconstruction contrôlée",
  };
  return labels[norm] || value || "-";
}

function decisionBadgeLabel(value) {
  const norm = String(value || "").trim().toUpperCase();
  const labels = {
    APPROVE: "Approbation directe",
    EDIT: "Correction recommandée",
    REJECT: "Rejet recommandé",
    FLAG: "Signalement recommandé",
  };
  return labels[norm] || value || "-";
}

function renderSimilarExisting(items) {
  const rows = Array.isArray(items) ? items : [];
  if (!rows.length) {
    return "Aucun doublon signalé.";
  }
  return rows.map((item) => {
    const ratio = Number(item.similarity || 0);
    const percent = Number.isFinite(ratio) ? `${Math.round(ratio * 100)}%` : "-";
    return `
      <div style="border:0.5px solid rgba(15,23,42,0.12);border-radius:9px;padding:8px;background:#fff;margin-top:6px">
        <div style="font-weight:600">${escapeHtml(item.req_id || "-")}</div>
        <div class="page-subtitle" style="margin-top:4px">Similarité estimée: ${escapeHtml(percent)}</div>
        <div style="margin-top:4px;font-size:12px">${escapeHtml(item.text || "-")}</div>
      </div>
    `;
  }).join("");
}

function renderStructureCards(reviewStructure) {
  const structure = reviewStructure || {};
  const cards = [
    ["Sujet juridique", structure.legal_subject || "À confirmer"],
    ["Verbe normatif", structure.normative_verb || "À confirmer"],
    ["Action / objet", structure.action_object || "À confirmer"],
    ["Condition", structure.condition_text || "Aucune détectée"],
    ["Exception", structure.exception_text || "Aucune détectée"],
    ["Mode source", humanizeSourceMode(structure.source_mode || "NON_PRECISE")],
  ];
  return cards.map(([label, value]) => `
    <div style="border:0.5px solid rgba(15,23,42,0.12);border-radius:10px;padding:9px;background:#fff">
      <div class="field-label" style="margin-bottom:4px">${escapeHtml(label)}</div>
      <div style="font-size:12px">${escapeHtml(value)}</div>
    </div>
  `).join("");
}

function renderGuidanceItems(items) {
  const rows = Array.isArray(items) ? items : [];
  if (!rows.length) {
    return `<div class="page-subtitle">Aucun point de vigilance supplémentaire.</div>`;
  }
  return rows.map((item) => {
    const level = String(item.level || "info").toLowerCase();
    const palette = level === "warning"
      ? { border: "rgba(245,158,11,0.35)", bg: "#fff7ed" }
      : level === "success"
        ? { border: "rgba(34,197,94,0.28)", bg: "#f0fdf4" }
        : { border: "rgba(59,130,246,0.2)", bg: "#eff6ff" };
    return `
      <div style="border:0.5px solid ${palette.border};background:${palette.bg};border-radius:10px;padding:9px;margin-top:6px">
        <div style="font-weight:600">${escapeHtml(item.title || "Point de contrôle")}</div>
        <div class="page-subtitle" style="margin-top:4px">${escapeHtml(item.message || "")}</div>
      </div>
    `;
  }).join("");
}

function renderRecommendation(context) {
  const decision = String(context?.review_recommended_decision || "EDIT").toUpperCase();
  const reason = context?.review_recommended_reason || "Relire l'exigence avant promotion.";
  const theme = decision === "APPROVE"
    ? { bg: "#ecfdf5", border: "rgba(34,197,94,0.25)" }
    : decision === "FLAG"
      ? { bg: "#fff7ed", border: "rgba(245,158,11,0.35)" }
      : decision === "REJECT"
        ? { bg: "#fef2f2", border: "rgba(239,68,68,0.25)" }
        : { bg: "#eff6ff", border: "rgba(59,130,246,0.25)" };
  return `
    <div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-start;border:0.5px solid ${theme.border};background:${theme.bg};border-radius:12px;padding:10px 12px">
      <div>
        <div style="font-weight:700">${escapeHtml(decisionBadgeLabel(decision))}</div>
        <div class="page-subtitle" style="margin-top:4px">${escapeHtml(reason)}</div>
      </div>
      <span class="badge badge-blue">${escapeHtml(decision)}</span>
    </div>
  `;
}

function updateValidationDecisionUi() {
  const decision = String(refs.modalDecision?.value || "APPROVE").toUpperCase();
  state.selectedDecision = decision;
  const config = {
    APPROVE: {
      label: "Promouvoir telle quelle",
      className: "btn btn-primary",
      hint: "À utiliser si le texte est déjà clair, complet et prêt pour le registre final.",
    },
    REJECT: {
      label: "Rejeter l'exigence",
      className: "btn btn-danger",
      hint: "À utiliser si le texte n'est pas une exigence exploitable ou sort du périmètre du registre.",
    },
    EDIT: {
      label: "Corriger puis promouvoir",
      className: "btn btn-primary",
      hint: "Voie recommandée quand la règle est correcte sur le fond mais doit être reformulée avant d'entrer dans le registre final.",
    },
    FLAG: {
      label: "Signaler pour revue",
      className: "btn btn-secondary",
      hint: "À utiliser en cas de doute persistant, de doublon probable ou de besoin d'arbitrage métier.",
    },
  }[decision] || { label: "Confirmer", className: "btn btn-primary" };

  refs.modalConfirm.textContent = config.label;
  refs.modalConfirm.className = config.className;
  if (refs.modalDecisionHint) {
    refs.modalDecisionHint.textContent = config.hint || "";
  }
  refs.modalReasonField.style.display = decision === "REJECT" || decision === "FLAG" ? "" : "none";
  refs.modalCorrectedField.style.display = decision === "EDIT" ? "" : "none";
}

async function loadValidationContext(reqId, { force = false } = {}) {
  if (!reqId) {
    return null;
  }
  if (!force && state.validationContext[reqId]) {
    return state.validationContext[reqId];
  }
  try {
    const context = await api.validationContext(reqId, getAuth().tenant_id);
    state.validationContext[reqId] = context;
    return context;
  } catch {
    return null;
  }
}

async function openRequirementDetails(reqId) {
  const row = findRow(reqId);
  if (!row || !refs.drawer || !refs.drawerContent) {
    return;
  }

  state.drawerReqId = reqId;
  refs.drawer.classList.add("is-open");

  const token = state.drawerLoadToken + 1;
  state.drawerLoadToken = token;

  refs.drawerContent.innerHTML = `
    <div class="skeleton skeleton-line"></div>
    <div class="skeleton skeleton-line"></div>
    <div class="skeleton skeleton-line"></div>
  `;

  let validations = [];
  let validationContext = null;
  try {
    const [payload, context] = await Promise.all([
      api.requirementValidations(reqId, getAuth().tenant_id),
      loadValidationContext(reqId),
    ]);
    validations = payload.items || [];
    validationContext = context;
  } catch {
    validations = [];
    validationContext = null;
  }

  if (token !== state.drawerLoadToken) {
    return;
  }

  const confidence = confidenceText(row.confidence);
  const confColor = confidenceColor(row.confidence);

  const validationsHtml = validations.length
    ? validations.map((v) => `
      <div style="border:0.5px solid rgba(15,23,42,0.12);border-radius:9px;padding:8px;margin-top:7px;background:#fff">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
          <span>${statusBadge(v.decision)}</span>
          <span class="page-subtitle">${escapeHtml(formatDate(v.created_at))}</span>
        </div>
        <div class="page-subtitle" style="margin-top:5px">${escapeHtml(v.validator_username || "-")} (${escapeHtml(v.validator_role || "-")})</div>
        ${v.rejection_reason ? `<div class="page-subtitle" style="margin-top:4px">Raison: ${escapeHtml(humanizeToken(v.rejection_reason))}</div>` : ""}
        <div style="margin-top:5px;font-size:12px">${escapeHtml(v.comment || "Sans commentaire")}</div>
      </div>
    `).join("")
    : `<div class="state">Aucune validation enregistrée pour cette exigence.</div>`;

  refs.drawerContent.innerHTML = `
    <div style="margin-bottom:8px;display:flex;justify-content:space-between;gap:8px;align-items:center">
      <strong>${escapeHtml(row.requirement_no || row.requirement_id?.slice(0, 8) || "Exigence")}</strong>
      ${statusBadge(row.status)}
    </div>

    ${renderField("Domaine", domainBadge(row.qse_domain))}
    ${renderField("Sous-domaine", `<span class="badge badge-absence">${escapeHtml(row.qse_sub_domain || "N/A")}</span>`)}
    ${renderField("Type", `<span class="badge badge-blue">${escapeHtml(row.req_type || "-")}</span>`)}
    ${renderField("Force normative", `<span class="badge badge-blue">${escapeHtml(humanizeNormativeStrength(validationContext?.normative_strength || row.normative_strength || "-"))}</span>`)}
    ${renderField("Confiance", `<span style="color:${confColor};font-weight:600">${confidence}</span>`)}
    ${row.grounding_score != null ? renderField("Ancrage source", `<span style="color:${confidenceColor(row.grounding_score)};font-weight:600">${Math.round(row.grounding_score * 100)}%</span>`) : ""}
    ${row.quality_score != null ? renderField("Score qualité", `<span style="color:${confidenceColor(row.quality_score)};font-weight:600">${Math.round(row.quality_score * 100)}%</span>`) : ""}
    ${validationContext?.promotion_blocked_reason ? renderField("Blocage auto-promotion", escapeHtml(humanizeBlockedReason(validationContext.promotion_blocked_reason))) : ""}
    ${renderField("Article", escapeHtml(row.article_label || "-"))}
    ${renderField("Citation ref", escapeHtml(row.citation_ref || "-"))}
    ${renderField("Source doc", escapeHtml(row.document_title || row.source || "-"))}
    ${renderField("Extraction", escapeHtml(row.extraction_source || "-"))}
    ${validationContext?.human_validation_flag || row.human_validation_flag ? renderField("Validation humaine", `<span class="badge badge-blue">${escapeHtml(humanizeValidationFlag(validationContext?.human_validation_flag || row.human_validation_flag || "-"))}</span>`) : ""}
    ${renderField("Date", escapeHtml(formatDate(row.extracted_at)))}

    <div style="margin-top:10px">
      <div class="field-label" style="margin-bottom:5px">Citation snippet</div>
      <div style="border:0.5px solid rgba(15,23,42,0.12);border-radius:9px;padding:9px;font-size:12px;background:#f8fafc">${escapeHtml(row.citation_snippet || "Aucun extrait")}</div>
    </div>

    <div style="margin-top:10px">
      <div class="field-label" style="margin-bottom:5px">Lecture juridique dérivée</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px">
        ${renderStructureCards(validationContext?.review_structure)}
      </div>
    </div>

    <div style="margin-top:10px">
      <div class="field-label" style="margin-bottom:5px">Texte exigence</div>
      <div style="border:0.5px solid rgba(15,23,42,0.12);border-radius:9px;padding:9px;font-size:12px;background:#f8fafc">${escapeHtml(row.requirement_text || "-")}</div>
    </div>

    <div style="margin-top:10px">
      <div class="field-label" style="margin-bottom:5px">Points de contrôle</div>
      <div>${renderGuidanceItems(validationContext?.review_guidance)}</div>
    </div>

    <div style="margin-top:10px">
      <div class="field-label" style="margin-bottom:5px">Doublons potentiels</div>
      <div>${renderSimilarExisting(validationContext?.similar_existing)}</div>
    </div>

    <div style="margin-top:12px">
      <div class="field-label">Historique validation humaine</div>
      ${validationsHtml}
    </div>
  `;
}

async function loadQueueBanner() {
  try {
    const overview = state.overview || await api.dashboardOverview(getAuth().tenant_id);
    const counts = deriveRegistryCounts(overview);
    updateStatusCardCounts(counts);
    updateStatusCards();
    refs.banner?.classList.add("hidden");
  } catch {
    updateStatusCardCounts(deriveRegistryCounts({}));
    updateStatusCards();
    refs.banner?.classList.add("hidden");
  }
}

function filterRows(rows) {
  const filters = currentFilters();
  if (!filters.severity) {
    return rows;
  }
  return rows.filter((row) => severityFromConfidence(row.confidence) === filters.severity);
}

function csvEscape(value) {
  const normalized = String(value ?? "")
    .replace(/\r?\n/g, " ")
    .replace(/"/g, "\"\"");
  return `"${normalized}"`;
}

function buildRequirementsCsv(rows) {
  const headers = [
    "Ref",
    "Article",
    "Domaine",
    "Sous-domaine",
    "Type",
    "Texte exigence",
    "Sévérité",
    "Confidence",
    "Statut",
    "Citation ref",
    "Citation snippet",
    "Source document",
    "Date extraction",
  ];

  const lines = [headers.map((h) => csvEscape(h)).join(";")];
  rows.forEach((row) => {
    const severity = severityFromConfidence(row.confidence);
    const severityLabel = severity === "HIGH" ? "Haute" : severity === "MEDIUM" ? "Moyenne" : "Faible";
    const values = [
      row.requirement_no || row.requirement_id || "",
      row.article_label || row.citation_ref || "",
      row.qse_domain || "",
      row.qse_sub_domain || "",
      row.req_type || "",
      row.requirement_text || "",
      severityLabel,
      confidenceText(row.confidence),
      row.status || "",
      row.citation_ref || "",
      row.citation_snippet || "",
      row.document_title || row.source || "",
      formatDate(row.extracted_at),
    ];
    lines.push(values.map((v) => csvEscape(v)).join(";"));
  });
  return lines.join("\n");
}

function exportCsv() {
  const rows = state.filteredRows || [];
  if (!rows.length) {
    alert("Aucune exigence à exporter avec les filtres actuels.");
    return;
  }
  const tenant = getAuth().tenant_id || "tenant";
  const datePart = new Date().toISOString().slice(0, 10);
  const csv = buildRequirementsCsv(rows);
  const blob = new Blob([`\uFEFF${csv}`], { type: "text/csv;charset=utf-8" });
  openBlob(blob, `a1_exigences_${tenant}_${datePart}.csv`);
}

function renderRows(rows) {
  state.filteredRows = rows;
  if (!rows.length) {
    const hasCorpus = Number(state.overview?.requirements_total || 0) > 0;
    if (!hasCorpus) {
      const docsTotal = Number(state.overview?.documents_total || 0);
      const message = docsTotal === 0
        ? "Aucune exigence disponible: le corpus juridique est vide. Réimporte d'abord les PDF via “Upload & runs”."
        : "Aucune exigence disponible pour le moment. Les documents existent, mais A1 doit encore être relancé ou finalisé.";
      refs.section.innerHTML = `
        <div class="state">
          <strong>Corpus A1 vide</strong>
          <div style="margin-top:6px">${escapeHtml(message)}</div>
          <button class="btn btn-secondary" data-open-upload="1">Ouvrir Upload & runs</button>
        </div>
      `;
      refs.section.querySelector("[data-open-upload='1']")?.addEventListener("click", () => {
        window.location.href = "/ui/upload.html";
      });
      closeDrawer();
      return;
    }
    renderEmpty(refs.section, "Aucune exigence ne correspond aux filtres.", "Réinitialiser", async () => {
      refs.filters.search.value = "";
      refs.filters.domain.value = "";
      refs.filters.type.value = "";
      refs.filters.status.value = "";
      refs.filters.severity.value = "";
      await loadRequirements();
    });
    closeDrawer();
    return;
  }

  refs.section.innerHTML = `
    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr>
            <th>Ref</th>
            <th>Article</th>
            <th>Domaine</th>
            <th>Type</th>
            <th>Texte</th>
            <th>Sévérité</th>
            <th>Confidence</th>
            <th>Statut</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody id="requirements-body"></tbody>
      </table>
    </div>
    <div id="requirements-meta" class="page-subtitle" style="margin-top:8px"></div>
  `;

  refs.tbody = document.getElementById("requirements-body");
  refs.meta = document.getElementById("requirements-meta");

  refs.tbody.innerHTML = rows.map((row) => {
    const severity = severityFromConfidence(row.confidence);
    const confidence = confidenceText(row.confidence);
    const confColor = confidenceColor(row.confidence);
    const ref = row.requirement_no || row.requirement_id?.slice(0, 8) || "-";
    const article = row.article_label || row.citation_ref || "-";
    const text = truncate(row.requirement_text || "", 130);
    const actionsHtml = isReadOnlyRole(session.role)
      ? `<span class="page-subtitle">Lecture seule</span>`
      : (row.status === "TO_VALIDATE" || row.status === "DRAFT")
        ? `
          <div class="actions" data-role="WRITE">
            <button class="btn btn-primary" data-action="review" data-req="${row.requirement_id}" data-label="${escapeHtml(ref)}">Ouvrir revue</button>
          </div>
        `
        : `<span class="page-subtitle">Voir détail</span>`;

    return `
      <tr data-row-req="${row.requirement_id}" style="cursor:pointer">
        <td>${escapeHtml(ref)}</td>
        <td>${escapeHtml(article)}</td>
        <td>${domainBadge(row.qse_domain)}</td>
        <td>${escapeHtml(row.req_type || "-")}</td>
        <td title="${escapeHtml(row.requirement_text || "")}">${escapeHtml(text)}</td>
        <td>${severityBadge(severity)}</td>
        <td style="color:${confColor};font-weight:600">${confidence}</td>
        <td>${statusBadge(row.status)}</td>
        <td>${actionsHtml}</td>
      </tr>
    `;
  }).join("");

  refs.meta.textContent = `${numberFmt(rows.length)} résultat(s)`;
  requireRole(session.role);

  refs.tbody.querySelectorAll("tr[data-row-req]").forEach((tr) => {
    tr.addEventListener("click", () => {
      openRequirementDetails(tr.dataset.rowReq);
    });
  });

  refs.tbody.querySelectorAll("button[data-action='review']").forEach((btn) => {
    btn.addEventListener("click", (event) => {
      event.stopPropagation();
      openValidation(btn.dataset.req, "EDIT", btn.dataset.label || "-");
    });
  });

  if (state.drawerReqId) {
    const stillVisible = rows.some((r) => r.requirement_id === state.drawerReqId);
    if (stillVisible) {
      openRequirementDetails(state.drawerReqId);
    }
  }
}

async function loadRequirements() {
  refs.section.innerHTML = `
    <div class="table-wrap">
      <table class="table">
        <thead>
          <tr><th>Chargement</th></tr>
        </thead>
        <tbody id="requirements-body"></tbody>
      </table>
    </div>
  `;
  refs.tbody = document.getElementById("requirements-body");
  renderSkeletonRows();

  try {
    const filters = currentFilters();
    const tenant = getAuth().tenant_id;
    const [payload, overview] = await Promise.all([
      api.listRequirements({
        limit: 250,
        search: filters.search,
        qse_domain: filters.qse_domain,
        req_type: filters.req_type,
        status: filters.status,
        tenant_id: tenant,
      }),
      api.dashboardOverview(tenant),
    ]);

    const incoming = Array.isArray(payload.items) ? payload.items : [];
    state.overview = overview || null;
    state.rows = incoming;
    await loadQueueBanner();
    syncFilterCatalog(incoming);
    const filtered = filterRows(incoming);
    renderRows(filtered);
  } catch (error) {
    state.filteredRows = [];
    state.overview = null;
    renderError(refs.section, error, "Réessayer", loadRequirements);
  }
}

refs.applyBtn.addEventListener("click", loadRequirements);
refs.statusCards.forEach((card) => {
  card.addEventListener("click", async () => {
    refs.filters.status.value = String(card.dataset.status || "");
    updateStatusCards(refs.filters.status.value);
    await loadRequirements();
  });
});
refs.filters.status?.addEventListener("change", () => updateStatusCards(refs.filters.status.value));
refs.exportBtn?.addEventListener("click", exportCsv);
refs.modalClose.addEventListener("click", closeValidation);
refs.modalCancel.addEventListener("click", closeValidation);
refs.modalDecision?.addEventListener("change", updateValidationDecisionUi);
refs.modal.addEventListener("click", (event) => {
  if (event.target === refs.modal) {
    closeValidation();
  }
});
refs.drawerClose?.addEventListener("click", closeDrawer);

refs.modalConfirm.addEventListener("click", async () => {
  if (!state.selectedReqId) {
    return;
  }
  refs.modalConfirm.disabled = true;
  const previousText = refs.modalConfirm.textContent;
  refs.modalConfirm.textContent = "Traitement...";

  try {
    const handledReqId = state.selectedReqId;
    const decision = String(refs.modalDecision.value || state.selectedDecision || "APPROVE").toUpperCase();
    const payload = {
      decision,
      comment: refs.modalComment.value.trim(),
      rejection_reason: refs.modalReason.value || null,
      corrected_text: refs.modalCorrectedText.value.trim() || null,
    };

    if ((decision === "REJECT" || decision === "FLAG") && !payload.rejection_reason) {
      throw new Error("Sélectionne une raison structurée avant de confirmer.");
    }
    if (decision === "EDIT" && !payload.corrected_text) {
      throw new Error("Le texte corrigé est obligatoire pour une correction humaine.");
    }

    const result = await api.validateRequirement(handledReqId, payload, "", getAuth().tenant_id);

    const row = findRow(handledReqId);
    if (row && result?.new_status) {
      row.status = result.new_status;
      if (decision === "EDIT" && payload.corrected_text) {
        row.requirement_text = payload.corrected_text;
      }
    }

    closeValidation();
    await loadRequirements();

    if (state.drawerReqId === handledReqId) {
      await openRequirementDetails(handledReqId);
    }
  } catch (error) {
    alert(error.message || "Validation impossible");
  } finally {
    refs.modalConfirm.disabled = false;
    refs.modalConfirm.textContent = previousText;
  }
});

await loadRequirements();
