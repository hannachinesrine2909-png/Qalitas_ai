import { initShell, getAuth } from "/ui/js/auth.js";
import { api } from "/ui/js/api.js";
import { numberFmt, renderEmpty, renderError, renderSkeleton, statusBadge } from "/ui/js/utils.js";

const session = initShell("dashboard");
if (!session) {
  throw new Error("Session absente");
}

const kpiRoot = document.getElementById("dashboard-kpis");
const domainRoot = document.getElementById("domain-bars");
const alertRoot = document.getElementById("alerts-list");
const pipelineRoot = document.getElementById("pipeline-list");
const guidanceRoot = document.getElementById("dashboard-data-guidance");
const ACTIVE_RUN_STATUSES = new Set(["RUNNING", "QUEUED", "PENDING"]);

function runBadge(status) {
  const norm = String(status || "PENDING").toUpperCase();
  const mapping = {
    DONE: ["dot-green", "badge-done", "Termine"],
    RUNNING: ["dot-blue", "badge-running", "En cours"],
    QUEUED: ["dot-amber", "badge-pending", "En attente"],
    PENDING: ["dot-gray", "badge-pending", "En attente"],
    PAUSED: ["dot-amber", "badge-amber", "En pause"],
    REVIEW_REQUIRED: ["dot-amber", "badge-amber", "A revoir"],
    FAILED: ["dot-red", "badge-error", "Echec"],
    ERROR: ["dot-red", "badge-error", "Erreur"],
  };
  return mapping[norm] || ["dot-gray", "badge-unknown", norm.toLowerCase()];
}

function parseRunTimestamp(run) {
  const ts = Date.parse(String(run?.updated_at || run?.created_at || ""));
  return Number.isNaN(ts) ? 0 : ts;
}

function pickDashboardRun(runs = [], overview = {}) {
  const sorted = [...(runs || [])].sort((a, b) => parseRunTimestamp(b) - parseRunTimestamp(a));
  const activeRun = sorted.find((row) => ACTIVE_RUN_STATUSES.has(String(row?.status || "").toUpperCase()));
  if (activeRun) {
    return activeRun;
  }
  const nonPausedRun = sorted.find((row) => String(row?.status || "").toUpperCase() !== "PAUSED");
  if (nonPausedRun) {
    return nonPausedRun;
  }
  const pausedRun = sorted.find((row) => String(row?.status || "").toUpperCase() === "PAUSED");
  const requirementsTotal = Number(overview.requirements_total || 0);
  if (pausedRun && requirementsTotal === 0) {
    return pausedRun;
  }
  return null;
}

function computeExtractionStatus(overview = {}, latestRun = null) {
  const runStatus = String(latestRun?.status || "").toUpperCase();
  const docsTotal = Number(overview.documents_total || 0);
  const requirementsTotal = Number(overview.requirements_total || 0);
  const promotedTotal = Number(overview.promoted_total || 0);
  if (ACTIVE_RUN_STATUSES.has(runStatus)) {
    return runStatus;
  }
  if (runStatus === "PAUSED" && requirementsTotal === 0 && promotedTotal === 0) {
    return "PAUSED";
  }
  if (requirementsTotal > 0 || promotedTotal > 0) {
    return "DONE";
  }
  if (docsTotal > 0 && runStatus === "DONE") {
    return "DONE";
  }
  return "PENDING";
}

function computeReviewStatus(overview = {}) {
  const promotedTotal = Number(overview.promoted_total || 0);
  const draftTotal = Number(overview.draft_total || 0);
  const toValidateTotal = Number(overview.to_validate_total || 0);
  const pendingReview = draftTotal + toValidateTotal;
  if (pendingReview > 0) {
    return "REVIEW_REQUIRED";
  }
  if (promotedTotal > 0) {
    return "DONE";
  }
  return "PENDING";
}

function setKpi(id, value) {
  const node = document.querySelector(`[data-kpi='${id}']`);
  if (node) {
    node.textContent = numberFmt(value || 0);
  }
}

function setDonut(compliance = {}) {
  const breakdown = compliance.status_breakdown || {};
  const conforme = Number(breakdown.CONFORME?.count || 0);
  const partiel = Number((breakdown.PARTIELLEMENT_CONFORME || breakdown.PARTIEL || {}).count || 0);
  const nc = Number(breakdown.NON_CONFORME?.count || 0);
  const absence = Number(breakdown.ABSENCE_DE_PREUVE?.count || 0);
  const total = conforme + partiel + nc + absence;
  const pct = total > 0 ? Math.round((conforme / total) * 100) : 0;
  const circumference = 289;
  const progress = Math.round((pct / 100) * circumference);
  const ring = document.getElementById("donut-ring");
  const value = document.getElementById("donut-value");
  if (ring) {
    ring.setAttribute("stroke-dasharray", `${progress} ${circumference}`);
  }
  if (value) {
    value.textContent = `${pct}%`;
  }
  const map = { conforme, partiel, nc, absence };
  Object.entries(map).forEach(([key, val]) => {
    const node = document.querySelector(`[data-legend='${key}']`);
    if (node) {
      node.textContent = numberFmt(val);
    }
  });
}

function renderPipeline(stages) {
  pipelineRoot.innerHTML = stages.map((stage) => {
    const [dot, badge, label] = runBadge(stage.status);
    return `
      <div class="pipeline-item">
        <span class="pipeline-left">
          <span class="dot ${dot}"></span>
          <span><strong>${stage.name}</strong><small>${stage.caption || ""}</small></span>
        </span>
        <span class="badge ${badge}">${label}</span>
      </div>
    `;
  }).join("");
}

function renderDomains(compliance = {}) {
  const items = compliance.worst_items || [];
  if (!items.length) {
    renderEmpty(domainRoot, "Aucun domaine calculé pour le moment.", "Actualiser", loadDashboard);
    return;
  }
  const agg = new Map();
  items.forEach((item) => {
    const key = String(item.domain || "Non classé");
    const score = Number(item.score || 0);
    if (!agg.has(key)) {
      agg.set(key, { total: 0, count: 0 });
    }
    const curr = agg.get(key);
    curr.total += score;
    curr.count += 1;
  });
  const rows = Array.from(agg.entries()).map(([domain, data]) => ({
    domain,
    rate: Math.round((data.total / Math.max(1, data.count)) * 100),
  })).sort((a, b) => b.rate - a.rate).slice(0, 6);

  domainRoot.innerHTML = rows.map((row) => `
    <div class="domain-row">
      <div class="domain-row-head"><span>${row.domain}</span><span>${row.rate}%</span></div>
      <div class="progress-track dashboard-progress"><div class="progress-bar" style="width:${row.rate}%"></div></div>
    </div>
  `).join("");
}

function renderAlerts(overview = {}, compliance = {}) {
  const pendingReview = Number(overview.to_validate_total || 0) + Number(overview.draft_total || 0);
  const critical = (compliance.gaps_breakdown || []).reduce((acc, gap) => {
    const sev = String(gap.severity || "").toUpperCase();
    if (sev.includes("CRIT") || sev.includes("MAJEUR") || sev.includes("HIGH") || sev.includes("ELEVEE")) {
      return acc + Number(gap.count || 0);
    }
    return acc;
  }, 0);

  const alerts = [
    {
      cls: pendingReview > 0 ? "" : "alert-green",
      msg: `${numberFmt(pendingReview)} exigence(s) en revue avant registre final`,
    },
    {
      cls: overview.reject_total > 0 ? "alert-red" : "alert-green",
      msg: `${numberFmt(overview.reject_total || 0)} rejet(s) détecté(s)`,
    },
    {
      cls: critical > 0 ? "alert-red" : "alert-green",
      msg: `${numberFmt(critical)} gap(s) critique(s) à traiter`,
    },
    {
      cls: "",
      msg: `${numberFmt((overview.recent_runs || []).length)} traitement(s) recents disponibles`,
    },
  ];

  if (!alerts.length) {
    renderEmpty(alertRoot, "Aucune alerte actuellement.", "Actualiser", loadDashboard);
    return;
  }

  alertRoot.innerHTML = alerts.map((alert) => `<div class="alert-item ${alert.cls}">${alert.msg}</div>`).join("");
}

function renderDataGuidance(overview = {}, applicability = {}, compliance = {}) {
  if (!guidanceRoot) {
    return;
  }
  const docsTotal = Number(overview.documents_total || 0);
  const requirementsTotal = Number(overview.requirements_total || 0);
  const promotedTotal = Number(overview.promoted_total || 0);
  const pendingReview = Number(overview.draft_total || 0) + Number(overview.to_validate_total || 0);
  const a2Total = Number(applicability.total || 0);
  const a3Total = Number(compliance.total_checks || 0);

  if (requirementsTotal === 0 && (a2Total > 0 || a3Total > 0)) {
    guidanceRoot.textContent = "Des résultats A2/A3 semblent encore présents alors qu'A1 est vide. Après purge/réimport, rejouez A2 et A3 pour réaligner toutes les pages.";
    guidanceRoot.style.background = "#fff1f2";
    guidanceRoot.style.borderColor = "rgba(220, 38, 38, 0.45)";
    guidanceRoot.style.color = "#991b1b";
    return;
  }
  if (docsTotal === 0 && requirementsTotal === 0) {
    guidanceRoot.textContent = "Le corpus réglementaire est vide. Réimporte les documents juridiques depuis “Upload & runs”, puis relance A2 et A3 après validation d'A1.";
    guidanceRoot.style.background = "#fffbeb";
    guidanceRoot.style.borderColor = "rgba(245, 158, 11, 0.6)";
    guidanceRoot.style.color = "#92400e";
    return;
  }
  if (docsTotal > 0 && requirementsTotal === 0) {
    guidanceRoot.textContent = "Des documents existent mais aucune exigence n'est encore disponible. Vérifie les runs A1 ou relance l'extraction depuis “Upload & runs”.";
    guidanceRoot.style.background = "#eff6ff";
    guidanceRoot.style.borderColor = "rgba(37, 99, 235, 0.3)";
    guidanceRoot.style.color = "#1d4ed8";
    return;
  }
  if (pendingReview > 0) {
    guidanceRoot.textContent = `Registre A1: ${numberFmt(promotedTotal)} validée(s), ${numberFmt(pendingReview)} à confirmer.`;
    guidanceRoot.style.background = "#fffbeb";
    guidanceRoot.style.borderColor = "rgba(245, 158, 11, 0.6)";
    guidanceRoot.style.color = "#92400e";
    return;
  }
  guidanceRoot.textContent = "Corpus A1 et résultats aval synchronisés.";
  guidanceRoot.style.background = "#f0fdf4";
  guidanceRoot.style.borderColor = "rgba(22, 163, 74, 0.35)";
  guidanceRoot.style.color = "#166534";
}

async function loadDashboard() {
  renderSkeleton(domainRoot, { rows: 4 });
  renderSkeleton(alertRoot, { rows: 4 });

  try {
    const tenant = getAuth().tenant_id;
    const [overviewRes, applicabilityRes, complianceRes, runsRes, statusRes] = await Promise.allSettled([
      api.dashboardOverview(tenant),
      api.applicabilitySummary(tenant),
      api.complianceSummary(tenant),
      api.listRuns(tenant, 6),
      api.systemStatus(),
    ]);

    const overview = overviewRes.status === "fulfilled" ? overviewRes.value : {};
    const applicability = applicabilityRes.status === "fulfilled" ? applicabilityRes.value : {};
    const compliance = complianceRes.status === "fulfilled" ? complianceRes.value : {};
    const runs = runsRes.status === "fulfilled" ? (runsRes.value.items || []) : [];
    const system = statusRes.status === "fulfilled" ? statusRes.value : {};

    const applicableCount = Number(applicability.counts?.APPLICABLE || 0) + Number(applicability.counts?.APPLICABLE_SOUS_CONDITIONS || 0);
    const conformCount = Number(compliance.status_breakdown?.CONFORME?.count || 0);
    const criticalGaps = (compliance.gaps_breakdown || []).reduce((acc, item) => {
      const sev = String(item.severity || "").toUpperCase();
      if (sev.includes("CRIT") || sev.includes("HIGH") || sev.includes("MAJ")) {
        return acc + Number(item.count || 0);
      }
      return acc;
    }, 0);

    setKpi("total", overview.requirements_total || 0);
    setKpi("applicable", applicableCount);
    setKpi("conforme", conformCount);
    setKpi("gaps", criticalGaps);

    setDonut(compliance);
    renderDomains(compliance);
    renderAlerts(overview, compliance);
    renderDataGuidance(overview, applicability, compliance);

    const latestRun = pickDashboardRun(runs, overview)
      || pickDashboardRun(overview.recent_runs || [], overview)
      || null;
    const a1Status = computeExtractionStatus(overview, latestRun);
    const reviewStatus = computeReviewStatus(overview);
    const a2Status = applicability.total > 0 ? "DONE" : "PENDING";
    const a3Status = compliance.total_checks > 0 ? "DONE" : "PENDING";
    const a4Status = system.status === "ok" ? "DONE" : "PENDING";
    renderPipeline([
      { name: "Extraction", caption: "Corpus vers exigences", status: a1Status },
      { name: "Validation humaine", caption: "Revue du registre", status: reviewStatus },
      { name: "Applicabilité", caption: "Contexte entreprise", status: a2Status },
      { name: "Conformité", caption: "Preuves et écarts", status: a3Status },
      { name: "Assistant expert", caption: "RAG et aide à la décision", status: a4Status },
    ]);

    const globalNode = document.getElementById("pipeline-global");
    if (globalNode) {
      let globalStatus = "PARTIEL";
      if (ACTIVE_RUN_STATUSES.has(a1Status)) {
        globalStatus = "RUNNING";
      } else if (a1Status === "PAUSED") {
        globalStatus = "PAUSED";
      } else if (reviewStatus === "REVIEW_REQUIRED") {
        globalStatus = "TO_VALIDATE";
      } else if ([a1Status, reviewStatus, a2Status, a3Status, a4Status].every((x) => x === "DONE")) {
        globalStatus = "CONFORME";
      }
      globalNode.innerHTML = statusBadge(globalStatus);
    }
  } catch (error) {
    renderDataGuidance({}, {}, {});
    if (guidanceRoot) {
      guidanceRoot.textContent = "Le contexte du corpus n'a pas pu être chargé. Les indicateurs ci-dessous peuvent être incomplets.";
    }
    renderError(domainRoot, error, "Réessayer", loadDashboard);
    renderError(alertRoot, error, "Réessayer", loadDashboard);
  }
}

loadDashboard();
