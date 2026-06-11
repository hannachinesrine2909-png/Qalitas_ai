const STATUS_STYLE_MAP = {
  CONFORME: "badge-conforme",
  APPROVE: "badge-conforme",
  APPROVED: "badge-conforme",
  PARTIEL: "badge-partiel",
  PARTIELLEMENT_CONFORME: "badge-partiel",
  TO_VALIDATE: "badge-partiel",
  RUNNING: "badge-running",
  APPLICABLE_SOUS_CONDITIONS: "badge-amber",
  SOUS_CONDITIONS: "badge-amber",
  APPLICABLE_FUTUR: "badge-blue",
  NC: "badge-nc",
  NON_CONFORME: "badge-nc",
  REJECT: "badge-nc",
  EDIT: "badge-blue",
  FLAG: "badge-amber",
  ERROR: "badge-error",
  FAILED: "badge-error",
  ABSENCE: "badge-absence",
  ABSENCE_DE_PREUVE: "badge-absence",
  DRAFT: "badge-draft",
  PENDING: "badge-pending",
  NON_APPLICABLE: "badge-non_applicable",
  APPLICABLE: "badge-applicable",
  PROMOTED: "badge-promoted",
  DONE: "badge-done",
  PAUSED: "badge-amber",
  CANCELLED: "badge-amber",
};

const DOMAIN_STYLE = {
  HSE: "badge-blue",
  SECURITE: "badge-purple",
  SANTE: "badge-conforme",
  ENVIRONNEMENT: "badge-green",
  QUALITE: "badge-applicable",
  ENERGIE: "badge-amber",
};

export function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/\"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

export function truncate(text, len = 120) {
  const input = String(text || "");
  if (input.length <= len) {
    return input;
  }
  return `${input.slice(0, Math.max(0, len - 1))}…`;
}

export function formatDate(iso) {
  if (!iso) {
    return "-";
  }
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) {
    return "-";
  }
  return new Intl.DateTimeFormat("fr-FR", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function normalizeStatus(status) {
  return String(status || "").trim().toUpperCase();
}

function labelFromStatus(statusNorm) {
  const labels = {
    TO_VALIDATE: "A valider",
    DRAFT: "Pre-validee auto",
    PARTIELLEMENT_CONFORME: "Partiel",
    PARTIEL: "Partiel",
    NON_CONFORME: "Non conforme",
    ABSENCE_DE_PREUVE: "Absence preuve",
    APPLICABLE_SOUS_CONDITIONS: "Sous conditions",
    APPLICABLE_FUTUR: "Applicable futur",
    NON_APPLICABLE: "Non applicable",
    APPLICABLE: "Applicable",
    PROMOTED: "Validee",
    APPROVED: "Conforme",
    APPROVE: "Conforme",
    EDIT: "Corrigee",
    FLAG: "A revoir",
    REJECT: "Rejetee",
    PENDING: "En attente",
    RUNNING: "En cours",
    DONE: "Termine",
    FAILED: "Echec",
    ERROR: "Erreur",
    PAUSED: "Pause",
    CANCELLED: "Annule",
  };
  return labels[statusNorm] || statusNorm.replace(/_/g, " ") || "Inconnu";
}

export function statusBadge(status) {
  const norm = normalizeStatus(status);
  const cls = STATUS_STYLE_MAP[norm] || "badge-unknown";
  const label = labelFromStatus(norm);
  return `<span class="badge ${cls}">${escapeHtml(label)}</span>`;
}

export function domainBadge(domain) {
  const raw = String(domain || "").trim();
  const norm = raw.toUpperCase();
  let cls = DOMAIN_STYLE[norm];
  if (!cls) {
    if (norm.includes("ENV")) {
      cls = "badge-green";
    } else if (norm.includes("SANTE") || norm.includes("SST")) {
      cls = "badge-conforme";
    } else if (norm.includes("SECUR")) {
      cls = "badge-purple";
    } else if (norm.includes("QUAL")) {
      cls = "badge-blue";
    } else {
      cls = "badge-absence";
    }
  }
  return `<span class="badge ${cls}">${escapeHtml(raw || "N/A")}</span>`;
}

export function confidenceColor(score) {
  const num = Number(score || 0);
  const normalized = num > 1 ? num / 100 : num;
  if (normalized >= 0.76) {
    return "var(--green)";
  }
  if (normalized >= 0.65) {
    return "var(--amber)";
  }
  return "var(--red)";
}

export function confidenceText(score) {
  const num = Number(score || 0);
  const normalized = num > 1 ? num / 100 : num;
  return `${Math.round(Math.max(0, Math.min(1, normalized)) * 100)}%`;
}

export function numberFmt(value) {
  return new Intl.NumberFormat("fr-FR").format(Number(value || 0));
}

export function renderSkeleton(container, { rows = 5, kpi = false } = {}) {
  if (!container) {
    return;
  }
  const lineClass = kpi ? "skeleton-kpi" : "skeleton-line";
  const html = Array.from({ length: rows }).map(() => `<div class="skeleton ${lineClass}"></div>`).join("");
  container.innerHTML = `<div>${html}</div>`;
}

export function renderError(container, error, retryLabel = "Réessayer", onRetry = null) {
  if (!container) {
    return;
  }
  container.innerHTML = `
    <div class="state">
      <strong>Erreur de chargement</strong>
      <div style="margin-top:6px">${escapeHtml(error?.message || error || "Erreur inconnue")}</div>
      ${onRetry ? `<button class="btn btn-secondary" data-retry="1">${escapeHtml(retryLabel)}</button>` : ""}
    </div>
  `;
  if (onRetry) {
    const btn = container.querySelector("[data-retry='1']");
    if (btn) {
      btn.addEventListener("click", onRetry);
    }
  }
}

export function renderEmpty(container, message = "Aucune donnée.", ctaLabel = "Actualiser", onCta = null) {
  if (!container) {
    return;
  }
  container.innerHTML = `
    <div class="state">
      <strong>État vide</strong>
      <div style="margin-top:6px">${escapeHtml(message)}</div>
      ${onCta ? `<button class="btn btn-secondary" data-cta="1">${escapeHtml(ctaLabel)}</button>` : ""}
    </div>
  `;
  if (onCta) {
    const btn = container.querySelector("[data-cta='1']");
    if (btn) {
      btn.addEventListener("click", onCta);
    }
  }
}

export function setElementText(selector, value, fallback = "-") {
  const node = document.querySelector(selector);
  if (!node) {
    return;
  }
  node.textContent = value === undefined || value === null || value === "" ? fallback : String(value);
}

export function openBlob(blob, filename = "export.pdf") {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

export function toggleModal(modalId, open) {
  const modal = document.getElementById(modalId);
  if (!modal) {
    return;
  }
  modal.classList.toggle("is-open", Boolean(open));
}

export function monthLabel(isoDate) {
  const d = new Date(isoDate);
  if (Number.isNaN(d.getTime())) {
    return "-";
  }
  return new Intl.DateTimeFormat("fr-FR", { month: "short", year: "2-digit" }).format(d);
}
