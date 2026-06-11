import { getAuth, initShell } from "/ui/js/auth.js";
import { api } from "/ui/js/api.js";
import { escapeHtml, monthLabel, numberFmt, renderEmpty, renderError } from "/ui/js/utils.js";

const session = initShell("analytics");
if (!session) {
  throw new Error("Session absente");
}

const APPLICABLE_STATUSES = new Set(["APPLICABLE", "APPLICABLE_SOUS_CONDITIONS"]);
const CLOSED_ACTION_STATES = new Set(["REALISEE", "CLOTUREE", "ANNULEE", "DONE", "CLOSED"]);

const refs = {
  domainPills: document.getElementById("analytics-domain-pills"),
  periodPills: document.getElementById("analytics-period-pills"),
  scopeLabel: document.getElementById("analytics-scope-label"),
  lastUpdate: document.getElementById("analytics-last-update"),
  contextLine: document.getElementById("analytics-context-line"),
  refreshBtn: document.getElementById("analytics-refresh"),
  kpis: document.getElementById("analytics-kpis"),
  ratios: document.getElementById("analytics-ratios"),
  linePanel: document.getElementById("line-panel"),
  lineMeta: document.getElementById("line-meta"),
  donutPanel: document.getElementById("donut-panel"),
  statusList: document.getElementById("status-kpi-list"),
  funnel: document.getElementById("funnel-wrap"),
  topGaps: document.getElementById("top-gaps"),
  topDocs: document.getElementById("top-docs"),
  quality: document.getElementById("quality-wrap"),
  insights: document.getElementById("insights-panel"),
  heatmap: document.getElementById("heatmap-wrap"),
};

const state = {
  domain: "ALL",
  period: 12,
  payload: null,
  lineChart: null,
  donutChart: null,
};

function toNum(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

function sumValues(obj = {}) {
  return Object.values(obj || {}).reduce((acc, value) => acc + toNum(value), 0);
}

function norm(value) {
  return String(value || "").trim().toUpperCase();
}

function normDomain(value) {
  return norm(value);
}

function domainValue(byDomain, wantedDomain) {
  const target = normDomain(wantedDomain);
  for (const [domain, count] of Object.entries(byDomain || {})) {
    if (normDomain(domain) === target) {
      return toNum(count);
    }
  }
  return 0;
}

function matchSelectedDomain(value) {
  if (state.domain === "ALL") {
    return true;
  }
  return normDomain(value) === normDomain(state.domain);
}

function safeDate(value) {
  if (!value) {
    return null;
  }
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? null : d;
}

function ymKey(dateObj) {
  return `${dateObj.getFullYear()}-${String(dateObj.getMonth() + 1).padStart(2, "0")}`;
}

function periodStart(months) {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth() - Math.max(0, months - 1), 1);
}

function inSelectedPeriod(value) {
  const d = safeDate(value);
  if (!d) {
    return true;
  }
  return d >= periodStart(state.period);
}

function percentFrom(num, den) {
  if (den <= 0) {
    return "0%";
  }
  return `${((num / den) * 100).toFixed(1)}%`;
}

function percentValue(value) {
  const num = toNum(value);
  if (num <= 1) {
    return `${(num * 100).toFixed(1)}%`;
  }
  return `${num.toFixed(1)}%`;
}

function statusBucket(status) {
  const s = norm(status);
  if (s === "CONFORME") {
    return "CONFORME";
  }
  if (s === "NON_CONFORME") {
    return "NC";
  }
  if (s === "ABSENCE_DE_PREUVE") {
    return "ABSENCE";
  }
  if (s === "PARTIEL" || s === "PARTIELLEMENT_CONFORME") {
    return "PARTIEL";
  }
  return "OTHER";
}

function severityBucket(severity) {
  const s = norm(severity);
  if (s.includes("CRIT")) return "CRITICAL";
  if (s.includes("HIGH") || s.includes("MAJ") || s.includes("ELEV")) return "HIGH";
  if (s.includes("MED") || s.includes("MOD")) return "MEDIUM";
  return "LOW";
}

function severityLabel(bucket) {
  if (bucket === "CRITICAL") return "Critique";
  if (bucket === "HIGH") return "Haute";
  if (bucket === "MEDIUM") return "Moyenne";
  return "Faible";
}

function severityBadgeClass(bucket) {
  if (bucket === "CRITICAL" || bucket === "HIGH") return "badge badge-nc";
  if (bucket === "MEDIUM") return "badge badge-amber";
  return "badge badge-absence";
}

function heatClass(level, value) {
  const num = toNum(value);
  if (level === "CONFORME") {
    if (num <= 0) return "hm-0";
    if (num < 3) return "hm-g1";
    if (num < 8) return "hm-g2";
    return "hm-g3";
  }
  if (num <= 0) return "hm-0";
  if (num < 3) return "hm-1";
  if (num < 8) return "hm-2";
  return "hm-3";
}

function destroyCharts() {
  if (state.lineChart) {
    state.lineChart.destroy();
    state.lineChart = null;
  }
  if (state.donutChart) {
    state.donutChart.destroy();
    state.donutChart = null;
  }
}

function loadingCards(count) {
  return Array.from({ length: count }).map(() => `
    <article class="analytics-kpi-card">
      <div class="skeleton skeleton-line"></div>
      <div class="skeleton skeleton-line"></div>
      <div class="skeleton skeleton-line"></div>
    </article>
  `).join("");
}

function loadingBlock(lines = 4) {
  return Array.from({ length: lines }).map(() => `<div class="skeleton skeleton-line"></div>`).join("");
}

function setLoadingState() {
  destroyCharts();
  refs.kpis.innerHTML = loadingCards(6);
  refs.ratios.innerHTML = loadingCards(5);
  refs.linePanel.innerHTML = loadingBlock(10);
  refs.lineMeta.innerHTML = "";
  refs.donutPanel.innerHTML = loadingBlock(8);
  refs.statusList.innerHTML = loadingBlock(5);
  refs.funnel.innerHTML = loadingBlock(6);
  refs.topGaps.innerHTML = loadingBlock(6);
  refs.topDocs.innerHTML = loadingBlock(8);
  refs.quality.innerHTML = loadingBlock(9);
  refs.insights.innerHTML = loadingBlock(5);
  refs.heatmap.innerHTML = loadingBlock(10);
}

function renderAllErrors(error) {
  const targets = [
    refs.kpis,
    refs.ratios,
    refs.linePanel,
    refs.donutPanel,
    refs.statusList,
    refs.funnel,
    refs.topGaps,
    refs.topDocs,
    refs.quality,
    refs.insights,
    refs.heatmap,
  ];
  targets.forEach((target) => {
    renderError(target, error, "Reessayer", loadAnalytics);
  });
}

function ensureMonthBuckets(months = 12) {
  const out = [];
  const now = new Date();
  for (let i = months - 1; i >= 0; i -= 1) {
    const d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    out.push({
      key: ymKey(d),
      label: monthLabel(d.toISOString()),
      extracted: 0,
      promoted: 0,
      conforme: 0,
      checks: 0,
    });
  }
  return out;
}

function renderPills(domains) {
  const list = ["ALL", ...domains];
  refs.domainPills.innerHTML = list.map((domain) => `
    <button class="chip ${state.domain === domain ? "is-active" : ""}" data-domain="${escapeHtml(domain)}">
      ${domain === "ALL" ? "Tous domaines" : escapeHtml(domain)}
    </button>
  `).join("");

  refs.periodPills.innerHTML = [3, 6, 12].map((months) => `
    <button class="chip ${state.period === months ? "is-active" : ""}" data-period="${months}">
      ${months} mois
    </button>
  `).join("");

  refs.domainPills.querySelectorAll("[data-domain]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.domain = btn.dataset.domain || "ALL";
      rerender();
    });
  });

  refs.periodPills.querySelectorAll("[data-period]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.period = toNum(btn.dataset.period) || 12;
      rerender();
    });
  });
}

function prepareScope(payload) {
  const byDomain = payload.overview.by_domain || {};
  const byStatus = payload.overview.by_status || {};
  const domains = Object.entries(byDomain)
    .sort((a, b) => toNum(b[1]) - toNum(a[1]))
    .map(([name]) => name);

  if (state.domain !== "ALL" && !domains.some((d) => normDomain(d) === normDomain(state.domain))) {
    state.domain = "ALL";
  }

  const totalDomainVolume = sumValues(byDomain);
  const selectedDomainVolume = state.domain === "ALL"
    ? totalDomainVolume
    : domainValue(byDomain, state.domain);
  const ratio = totalDomainVolume > 0 ? selectedDomainVolume / totalDomainVolume : 1;

  const extractedBase = sumValues(byStatus) || totalDomainVolume;
  const extracted = state.domain === "ALL" ? extractedBase : selectedDomainVolume;
  const promoted = Math.round(toNum(byStatus.PROMOTED) * ratio);
  const toValidate = Math.round(toNum(byStatus.TO_VALIDATE) * ratio);
  const rejected = Math.round(toNum(byStatus.REJECT) * ratio);
  const draft = Math.round(toNum(byStatus.DRAFT) * ratio);

  const decisionsAll = Array.isArray(payload.applicability?.decisions) ? payload.applicability.decisions : [];
  const decisionsScoped = decisionsAll.filter((row) => matchSelectedDomain(row.qse_domain));
  const applicableFallback = Math.round(
    (toNum(payload.applicability?.counts?.APPLICABLE) + toNum(payload.applicability?.counts?.APPLICABLE_SOUS_CONDITIONS)) * ratio,
  );
  const nonApplicableFallback = Math.round(toNum(payload.applicability?.counts?.NON_APPLICABLE) * ratio);
  const uncertainFallback = Math.round(toNum(payload.applicability?.counts?.INCERTAIN) * ratio);

  const applicable = decisionsAll.length
    ? decisionsScoped.filter((row) => APPLICABLE_STATUSES.has(norm(row.status))).length
    : applicableFallback;
  const nonApplicable = decisionsAll.length
    ? decisionsScoped.filter((row) => norm(row.status) === "NON_APPLICABLE").length
    : nonApplicableFallback;
  const uncertain = decisionsAll.length
    ? decisionsScoped.filter((row) => norm(row.status) === "INCERTAIN").length
    : uncertainFallback;

  const checksRaw = Array.isArray(payload.compliance?.worst_items) ? payload.compliance.worst_items : [];
  const checksScoped = checksRaw.filter((row) => matchSelectedDomain(row.qse_domain || row.domain));
  const statusCounts = { CONFORME: 0, PARTIEL: 0, NC: 0, ABSENCE: 0 };
  checksScoped.forEach((row) => {
    const bucket = statusBucket(row.status);
    if (bucket === "CONFORME") statusCounts.CONFORME += 1;
    if (bucket === "PARTIEL") statusCounts.PARTIEL += 1;
    if (bucket === "NC") statusCounts.NC += 1;
    if (bucket === "ABSENCE") statusCounts.ABSENCE += 1;
  });

  if (!checksScoped.length) {
    const breakdown = payload.compliance?.status_breakdown || {};
    statusCounts.CONFORME = Math.round(toNum((breakdown.CONFORME || {}).count) * ratio);
    statusCounts.PARTIEL = Math.round(
      (toNum((breakdown.PARTIELLEMENT_CONFORME || {}).count) + toNum((breakdown.PARTIEL || {}).count)) * ratio,
    );
    statusCounts.NC = Math.round(toNum((breakdown.NON_CONFORME || {}).count) * ratio);
    statusCounts.ABSENCE = Math.round(toNum((breakdown.ABSENCE_DE_PREUVE || {}).count) * ratio);
  }

  const totalChecks = checksScoped.length || (statusCounts.CONFORME + statusCounts.PARTIEL + statusCounts.NC + statusCounts.ABSENCE);
  const conforme = statusCounts.CONFORME;
  const partiel = statusCounts.PARTIEL;
  const nonConforme = statusCounts.NC;
  const absence = statusCounts.ABSENCE;

  const gapsAll = Array.isArray(payload.compliance?.recent_gaps)
    ? payload.compliance.recent_gaps.filter((row) => matchSelectedDomain(row.qse_domain))
    : [];

  const actionsAll = Array.isArray(payload.compliance?.recent_actions)
    ? payload.compliance.recent_actions.filter((row) => matchSelectedDomain(row.qse_domain))
    : [];

  const actionsOpenFromRows = actionsAll.filter((row) => !CLOSED_ACTION_STATES.has(norm(row.state))).length;
  const actionsOpenFallback = Math.round(
    Object.entries(payload.compliance?.actions_breakdown || {}).reduce(
      (acc, [stateLabel, count]) => acc + (CLOSED_ACTION_STATES.has(norm(stateLabel)) ? 0 : toNum(count)),
      0,
    ) * ratio,
  );
  const actionsOpen = actionsAll.length ? actionsOpenFromRows : actionsOpenFallback;

  const proved = Math.round(toNum(payload.proofs?.total) * ratio);

  let low = 0;
  let medium = 0;
  let high = 0;
  let critical = 0;
  gapsAll.forEach((gap) => {
    const bucket = severityBucket(gap.severity);
    if (bucket === "LOW") low += 1;
    if (bucket === "MEDIUM") medium += 1;
    if (bucket === "HIGH") high += 1;
    if (bucket === "CRITICAL") critical += 1;
  });
  if (!gapsAll.length) {
    const nc = payload.compliance?.nc_reglementaire || {};
    critical = Math.round(toNum(nc.critical) * ratio);
    high = Math.round(toNum(nc.major) * ratio);
    medium = Math.round(toNum(nc.minor) * ratio);
  }

  const promotionRate = extracted > 0 ? promoted / extracted : 0;
  const applicabilityRate = promoted > 0 ? applicable / promoted : 0;
  const conformityRate = applicable > 0 ? conforme / applicable : 0;
  const proofCoverageRate = applicable > 0 ? proved / applicable : 0;
  const actionLoadRate = totalChecks > 0 ? actionsOpen / totalChecks : 0;
  const riskScore = (critical * 4) + (high * 3) + (medium * 2) + low;

  return {
    domains,
    byDomain,
    ratio,
    extracted,
    promoted,
    applicable,
    nonApplicable,
    uncertain,
    conforme,
    partiel,
    nonConforme,
    absence,
    totalChecks,
    proved,
    actionsOpen,
    toValidate,
    rejected,
    draft,
    low,
    medium,
    high,
    critical,
    promotionRate,
    applicabilityRate,
    conformityRate,
    proofCoverageRate,
    actionLoadRate,
    riskScore,
    decisionsScoped,
    checksScoped,
    gapsAll,
    actionsAll,
  };
}

function buildTrendSeries(scope, payload) {
  const buckets = ensureMonthBuckets(state.period);
  const map = new Map(buckets.map((bucket) => [bucket.key, bucket]));
  const periodMin = periodStart(state.period);

  const trendRows = Array.isArray(payload.overview?.trend) ? payload.overview.trend : [];
  trendRows.forEach((row) => {
    const d = safeDate(row.day);
    if (!d || d < periodMin) return;
    const key = ymKey(d);
    const slot = map.get(key);
    if (!slot) return;
    slot.extracted += Math.round(toNum(row.requirements_count) * scope.ratio);
  });

  const validations = Array.isArray(payload.validations?.validations) ? payload.validations.validations : [];
  validations.forEach((row) => {
    if (norm(row.decision) !== "APPROVE") return;
    const d = safeDate(row.created_at);
    if (!d || d < periodMin) return;
    const key = ymKey(d);
    const slot = map.get(key);
    if (!slot) return;
    slot.promoted += Math.max(1, Math.round(scope.ratio));
  });

  scope.checksScoped.forEach((row) => {
    const d = safeDate(row.updated_at);
    if (!d || d < periodMin) return;
    const key = ymKey(d);
    const slot = map.get(key);
    if (!slot) return;
    slot.checks += 1;
    if (norm(row.status) === "CONFORME") {
      slot.conforme += 1;
    }
  });

  const labels = [];
  const extracted = [];
  const promoted = [];
  const conforme = [];
  const rate = [];
  buckets.forEach((bucket) => {
    labels.push(bucket.label);
    extracted.push(bucket.extracted);
    promoted.push(bucket.promoted);
    conforme.push(bucket.conforme);
    rate.push(bucket.checks > 0 ? Number(((bucket.conforme / bucket.checks) * 100).toFixed(1)) : 0);
  });

  return {
    labels,
    extracted,
    promoted,
    conforme,
    rate,
  };
}

function renderContext(scope) {
  const scopeLabel = state.domain === "ALL" ? "Tous domaines" : state.domain;
  refs.scopeLabel.textContent = `Perimetre: ${scopeLabel}`;
  refs.lastUpdate.textContent = `MAJ: ${new Intl.DateTimeFormat("fr-FR", {
    day: "2-digit",
    month: "2-digit",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date())}`;
  refs.contextLine.textContent = `KPIs cumules tenant, periode active ${state.period} mois pour tendances et distribution de risque. Ratio domaine: ${percentFrom(scope.extracted, sumValues(scope.byDomain))}`;
}

function renderKpis(scope) {
  const cards = [
    { label: "Exigences extraites", value: scope.extracted, meta: `${numberFmt(scope.toValidate)} a valider` },
    { label: "Exigences promues", value: scope.promoted, meta: `${numberFmt(scope.rejected)} rejetees` },
    { label: "Exigences applicables", value: scope.applicable, meta: `${numberFmt(scope.uncertain)} incertaines` },
    { label: "Exigences conformes", value: scope.conforme, meta: `${numberFmt(scope.nonConforme)} non conformes` },
    { label: "Preuves liées", value: scope.proved, meta: `${numberFmt(scope.absence)} absences de preuve` },
    { label: "Actions ouvertes", value: scope.actionsOpen, meta: `${numberFmt(scope.critical)} ecarts critiques` },
  ];
  refs.kpis.innerHTML = cards.map((item) => `
    <article class="analytics-kpi-card">
      <p class="kpi-label">${escapeHtml(item.label)}</p>
      <p class="analytics-kpi-value">${numberFmt(item.value)}</p>
      <p class="analytics-kpi-meta">${escapeHtml(item.meta)}</p>
    </article>
  `).join("");
}

function renderRatios(scope) {
  const cards = [
    { label: "Tx promotion", value: percentValue(scope.promotionRate), meta: `${numberFmt(scope.promoted)} / ${numberFmt(scope.extracted)}` },
    { label: "Tx applicabilite", value: percentValue(scope.applicabilityRate), meta: `${numberFmt(scope.applicable)} / ${numberFmt(scope.promoted)}` },
    { label: "Tx conformite", value: percentValue(scope.conformityRate), meta: `${numberFmt(scope.conforme)} / ${numberFmt(scope.applicable)}` },
    { label: "Couverture preuves", value: percentValue(scope.proofCoverageRate), meta: `${numberFmt(scope.proved)} preuves reliees` },
    { label: "Charge actions", value: percentValue(scope.actionLoadRate), meta: `${numberFmt(scope.actionsOpen)} ouvertes / ${numberFmt(scope.totalChecks)} checks` },
  ];
  refs.ratios.innerHTML = cards.map((item) => `
    <article class="ratio-card">
      <p class="ratio-label">${escapeHtml(item.label)}</p>
      <p class="ratio-value">${escapeHtml(item.value)}</p>
      <p class="ratio-meta">${escapeHtml(item.meta)}</p>
    </article>
  `).join("");
}

function renderLineChart(series) {
  const maxVolume = Math.max(
    0,
    ...series.extracted,
    ...series.promoted,
    ...series.conforme,
  );

  if (maxVolume <= 0) {
    destroyCharts();
    renderEmpty(refs.linePanel, "Pas assez de donnees temporelles pour la periode choisie.", "Actualiser", loadAnalytics);
    refs.lineMeta.innerHTML = "";
    return;
  }

  refs.linePanel.innerHTML = `<canvas id="line-trend" height="220"></canvas>`;
  const ctx = document.getElementById("line-trend");
  if (!ctx) return;

  if (state.lineChart) {
    state.lineChart.destroy();
  }

  state.lineChart = new Chart(ctx, {
    type: "bar",
    data: {
      labels: series.labels,
      datasets: [
        {
          type: "bar",
          label: "Extraites",
          data: series.extracted,
          backgroundColor: "rgba(21, 101, 192, 0.2)",
          borderColor: "rgba(21, 101, 192, 0.45)",
          borderWidth: 1,
          borderRadius: 6,
        },
        {
          type: "line",
          label: "Promues",
          data: series.promoted,
          borderColor: "#0ea5e9",
          backgroundColor: "rgba(14, 165, 233, 0.18)",
          tension: 0.34,
          pointRadius: 2.5,
          fill: false,
        },
        {
          type: "line",
          label: "Conformes",
          data: series.conforme,
          borderColor: "#16a34a",
          backgroundColor: "rgba(22, 163, 74, 0.18)",
          tension: 0.34,
          pointRadius: 2.5,
          fill: false,
        },
        {
          type: "line",
          label: "Tx conformite",
          data: series.rate,
          yAxisID: "y1",
          borderColor: "#7B1FA2",
          borderDash: [6, 4],
          tension: 0.3,
          pointRadius: 2,
          fill: false,
        },
      ],
    },
    options: {
      maintainAspectRatio: false,
      responsive: true,
      plugins: {
        legend: {
          position: "bottom",
          labels: { boxWidth: 14 },
        },
      },
      scales: {
        x: {
          grid: { display: false },
        },
        y: {
          beginAtZero: true,
          title: { display: true, text: "Volume" },
          ticks: {
            precision: 0,
          },
        },
        y1: {
          beginAtZero: true,
          position: "right",
          min: 0,
          max: 100,
          title: { display: true, text: "%" },
          grid: { drawOnChartArea: false },
          ticks: {
            callback: (value) => `${value}%`,
          },
        },
      },
    },
  });

  const totalExtracted = series.extracted.reduce((acc, value) => acc + toNum(value), 0);
  const totalPromoted = series.promoted.reduce((acc, value) => acc + toNum(value), 0);
  const totalConforme = series.conforme.reduce((acc, value) => acc + toNum(value), 0);
  const avgRate = series.rate.length
    ? (series.rate.reduce((acc, value) => acc + toNum(value), 0) / series.rate.length)
    : 0;
  refs.lineMeta.innerHTML = `
    <span class="chart-pill">Extraites periode: <strong>${numberFmt(totalExtracted)}</strong></span>
    <span class="chart-pill">Promues periode: <strong>${numberFmt(totalPromoted)}</strong></span>
    <span class="chart-pill">Conformes periode: <strong>${numberFmt(totalConforme)}</strong></span>
    <span class="chart-pill">Tx conformite moyen: <strong>${avgRate.toFixed(1)}%</strong></span>
  `;
}

function renderDonut(scope) {
  const labels = ["Conforme", "Partiel", "NC", "Absence"];
  const values = [scope.conforme, scope.partiel, scope.nonConforme, scope.absence];
  const total = values.reduce((acc, value) => acc + toNum(value), 0);

  if (total <= 0) {
    if (state.donutChart) {
      state.donutChart.destroy();
      state.donutChart = null;
    }
    renderEmpty(refs.donutPanel, "Aucune verification exploitable pour ce perimetre.", "Actualiser", loadAnalytics);
    refs.statusList.innerHTML = "";
    return;
  }

  refs.donutPanel.innerHTML = `
    <div class="donut-wrap">
      <canvas id="donut-status"></canvas>
      <div class="donut-center">
        <div class="donut-center-value">${numberFmt(total)}</div>
        <div class="donut-center-label">checks</div>
      </div>
    </div>
  `;

  const ctx = document.getElementById("donut-status");
  if (!ctx) return;

  if (state.donutChart) {
    state.donutChart.destroy();
  }

  state.donutChart = new Chart(ctx, {
    type: "doughnut",
    data: {
      labels,
      datasets: [{
        data: values,
        backgroundColor: ["#16a34a", "#f59e0b", "#dc2626", "#94a3b8"],
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
      },
      cutout: "72%",
    },
  });

  const dots = ["dot-green", "dot-amber", "dot-red", "dot-gray"];
  refs.statusList.innerHTML = labels.map((label, idx) => `
    <div class="status-row">
      <span class="status-label"><i class="dot ${dots[idx]}"></i>${escapeHtml(label)}</span>
      <span class="status-count">${numberFmt(values[idx])}</span>
      <span class="status-share">${percentFrom(values[idx], total)}</span>
    </div>
  `).join("");
}

function renderFunnel(scope) {
  const stages = [
    { name: "Extraites", label: "Extraction brute", value: scope.extracted },
    { name: "Promues", label: "Validation humaine", value: scope.promoted },
    { name: "Applicables", label: "Applicabilite retenue", value: scope.applicable },
    { name: "Conformes", label: "Conformite validee", value: scope.conforme },
    { name: "Preuves", label: "Preuves liées", value: scope.proved },
  ];

  const first = Math.max(1, toNum(stages[0].value));
  const toneByStage = ["cyan", "blue", "indigo", "green", "slate"];
  refs.funnel.innerHTML = stages.map((stage, index) => {
    const baselineRatio = toNum(stage.value) / first;
    const width = Math.max(6, Math.min(100, Math.round(baselineRatio * 100)));
    const prev = index === 0 ? toNum(stage.value) : toNum(stages[index - 1].value);
    const conv = prev > 0 ? `${((toNum(stage.value) / prev) * 100).toFixed(1)}%` : "0%";
    const baselineLabel = baselineRatio > 1
      ? `+${((baselineRatio - 1) * 100).toFixed(1)}% vs flux initial`
      : `${(baselineRatio * 100).toFixed(1)}% du flux initial`;
    const tone = toneByStage[index] || "blue";
    return `
      <div class="funnel-row ${baselineRatio > 1 ? "is-overflow" : ""}">
        <div class="funnel-stage">
          <span class="funnel-rank">S${index + 1}</span>
          <div class="funnel-stage-copy">
            <strong class="stage-name">${escapeHtml(stage.name)}</strong>
            <span class="stage-label">${escapeHtml(stage.label)}</span>
          </div>
        </div>
        <div class="funnel-body">
          <div class="funnel-bar">
            <div class="funnel-fill funnel-fill--${tone}" style="width:${width}%"></div>
          </div>
          <div class="funnel-meta">
            <span class="funnel-meta-item">Conversion précédente: <strong>${conv}</strong></span>
            <span class="funnel-meta-item ${baselineRatio > 1 ? "is-overflow" : ""}">${baselineLabel}</span>
          </div>
        </div>
        <div class="funnel-value">
          <strong>${numberFmt(stage.value)}</strong>
          <span class="small">Volume</span>
        </div>
      </div>
    `;
  }).join("");
}

function aggregateTopGaps(scope, payload) {
  const rows = [];
  if (scope.gapsAll.length) {
    const grouped = new Map();
    scope.gapsAll.forEach((gap) => {
      const sev = severityBucket(gap.severity);
      const type = String(gap.gap_type || "GAP").trim() || "GAP";
      const key = `${type}__${sev}`;
      const current = grouped.get(key) || { gap_type: type, severity: sev, count: 0 };
      current.count += 1;
      grouped.set(key, current);
    });
    rows.push(...grouped.values());
  } else {
    const raw = Array.isArray(payload.compliance?.gaps_breakdown) ? payload.compliance.gaps_breakdown : [];
    raw.forEach((gap) => {
      const count = Math.round(toNum(gap.count) * scope.ratio);
      if (count <= 0) return;
      rows.push({
        gap_type: String(gap.gap_type || "GAP"),
        severity: severityBucket(gap.severity),
        count,
      });
    });
  }
  return rows.sort((a, b) => toNum(b.count) - toNum(a.count)).slice(0, 5);
}

function renderTopGaps(scope, payload) {
  const rows = aggregateTopGaps(scope, payload);
  if (!rows.length) {
    renderEmpty(refs.topGaps, "Aucun ecart NC pour le perimetre courant.", "Actualiser", loadAnalytics);
    return;
  }
  const max = Math.max(1, ...rows.map((row) => toNum(row.count)));
  const total = rows.reduce((acc, row) => acc + toNum(row.count), 0);
  refs.topGaps.innerHTML = rows.map((row, index) => {
    const width = Math.round((toNum(row.count) / max) * 100);
    const sev = String(row.severity || "LOW");
    const share = total > 0 ? `${((toNum(row.count) / total) * 100).toFixed(1)}%` : "0%";
    const sevClass = sev.toLowerCase();
    return `
      <article class="gap-item gap-item--${escapeHtml(sevClass)}">
        <div class="gap-head">
          <div class="gap-title-stack">
            <div class="gap-title-line">
              <span class="gap-rank">#${index + 1}</span>
              <span class="gap-title">${escapeHtml(row.gap_type || "GAP")}</span>
            </div>
            <div class="gap-tags">
              <span class="${severityBadgeClass(sev)}">${escapeHtml(severityLabel(sev))}</span>
              <span class="gap-share">${share} du top 5</span>
            </div>
          </div>
          <div class="gap-metric">
            <span class="gap-count">${numberFmt(row.count)}</span>
            <span class="gap-count-label">écarts</span>
          </div>
        </div>
        <div class="gap-meter">
          <div class="progress-track gap-progress-track">
            <div class="progress-bar gap-progress-bar" style="width:${Math.max(10, width)}%"></div>
          </div>
        </div>
      </article>
    `;
  }).join("");
}

function renderTopDocuments(scope, payload) {
  const docs = Array.isArray(payload.overview?.top_documents) ? payload.overview.top_documents : [];
  if (!docs.length) {
    renderEmpty(refs.topDocs, "Aucun document source detecte.", "Actualiser", loadAnalytics);
    return;
  }

  const rows = docs.slice(0, 8).map((doc, idx) => {
    const raw = toNum(doc.requirements_count);
    const value = state.domain === "ALL" ? raw : Math.round(raw * scope.ratio);
    return {
      rank: idx + 1,
      title: String(doc.title || "(sans titre)"),
      value: Math.max(0, value),
    };
  });
  const max = Math.max(1, ...rows.map((row) => row.value));

  refs.topDocs.innerHTML = `
    <table class="mini-table">
      <thead>
        <tr>
          <th>#</th>
          <th>Document</th>
          <th>Exigences</th>
        </tr>
      </thead>
      <tbody>
        ${rows.map((row) => `
          <tr>
            <td>${row.rank}</td>
            <td>
              <div class="doc-title">${escapeHtml(row.title)}</div>
              <div class="doc-sub">
                <div class="progress-track" style="margin-top:4px">
                  <div class="progress-bar" style="width:${Math.round((row.value / max) * 100)}%"></div>
                </div>
              </div>
            </td>
            <td>${numberFmt(row.value)}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
    ${state.domain === "ALL" ? "" : `<p class="card-subtitle" style="margin-top:8px">Vue estimee par ratio domaine (${escapeHtml(state.domain)}).</p>`}
  `;
}

function renderQuality(scope, payload) {
  const q = payload.overview?.quality_snapshot || {};
  const gate = payload.gate || {};
  const families = Array.isArray(payload.families?.items) ? payload.families.items : [];

  const cards = [
    {
      label: "Conversion brut -> final",
      value: percentValue(q.raw_to_final_conversion_rate),
    },
    {
      label: "Part moyenne a valider",
      value: percentValue(q.to_validate_share_avg),
    },
    {
      label: "Part draft courante",
      value: percentValue(q.current_draft_share),
    },
    {
      label: "Docs benchmark succes",
      value: numberFmt(q.docs_success || 0),
    },
  ];

  const policyCounts = Object.entries(gate.doc_gate_policy_counts || {}).sort((a, b) => toNum(b[1]) - toNum(a[1])).slice(0, 4);
  const dropReasons = Object.entries(gate.drop_share_by_reason_code || {}).sort((a, b) => toNum(b[1]) - toNum(a[1])).slice(0, 4);
  const topFamilies = [...families]
    .sort((a, b) => toNum(b.requirements_count) - toNum(a.requirements_count))
    .slice(0, 4);

  refs.quality.innerHTML = `
    <div class="quality-grid">
      ${cards.map((card) => `
        <div class="quality-card">
          <div class="label">${escapeHtml(card.label)}</div>
          <div class="value">${escapeHtml(card.value)}</div>
        </div>
      `).join("")}
    </div>

    <div class="quality-subsection">
      <div class="quality-subsection-title">Gate policy counts</div>
      <div class="quality-list">
        ${policyCounts.length ? policyCounts.map(([name, value]) => `
          <div class="quality-item">
            <span>${escapeHtml(name)}</span>
            <strong>${numberFmt(value)}</strong>
          </div>
        `).join("") : `<div class="state">Aucune donnee gate disponible.</div>`}
      </div>
    </div>

    <div class="quality-subsection">
      <div class="quality-subsection-title">Top drop reasons</div>
      <div class="quality-list">
        ${dropReasons.length ? dropReasons.map(([name, value]) => `
          <div class="quality-item">
            <span>${escapeHtml(name)}</span>
            <strong>${percentValue(value)}</strong>
          </div>
        `).join("") : `<div class="state">Aucun motif de rejet trace.</div>`}
      </div>
    </div>

    <div class="quality-subsection">
      <div class="quality-subsection-title">Top families</div>
      <div class="quality-list">
        ${topFamilies.length ? topFamilies.map((row) => `
          <div class="quality-item">
            <span>${escapeHtml(String(row.family || "UNKNOWN"))}</span>
            <strong>${numberFmt(row.requirements_count || 0)}</strong>
          </div>
        `).join("") : `<div class="state">Aucune famille document detectee.</div>`}
      </div>
    </div>
  `;
}

function buildHeatmapRows(scope, payload) {
  const sourceDomains = state.domain === "ALL" ? scope.domains : [state.domain];
  if (!sourceDomains.length) {
    return [];
  }

  const decisions = Array.isArray(payload.applicability?.decisions) ? payload.applicability.decisions : [];
  const checks = Array.isArray(payload.compliance?.worst_items) ? payload.compliance.worst_items : [];
  const gaps = Array.isArray(payload.compliance?.recent_gaps) ? payload.compliance.recent_gaps : [];

  const rows = sourceDomains.map((domain) => {
    const extracted = domainValue(scope.byDomain, domain);
    const applicable = decisions.filter(
      (row) => normDomain(row.qse_domain) === normDomain(domain) && APPLICABLE_STATUSES.has(norm(row.status)),
    ).length;

    const checksDomain = checks.filter(
      (row) => normDomain(row.qse_domain || row.domain) === normDomain(domain) && inSelectedPeriod(row.updated_at),
    );
    const conforme = checksDomain.filter((row) => norm(row.status) === "CONFORME").length;

    let low = 0;
    let medium = 0;
    let high = 0;
    let critical = 0;
    gaps.forEach((row) => {
      if (normDomain(row.qse_domain) !== normDomain(domain)) return;
      if (!inSelectedPeriod(row.created_at)) return;
      const sev = severityBucket(row.severity);
      if (sev === "LOW") low += 1;
      if (sev === "MEDIUM") medium += 1;
      if (sev === "HIGH") high += 1;
      if (sev === "CRITICAL") critical += 1;
    });

    const riskScore = (critical * 4) + (high * 3) + (medium * 2) + low;
    return {
      domain,
      extracted,
      applicable,
      conforme,
      low,
      medium,
      high,
      critical,
      riskScore,
    };
  });

  return rows.sort(
    (a, b) => (b.riskScore - a.riskScore)
      || (b.critical - a.critical)
      || (b.high - a.high)
      || (b.extracted - a.extracted),
  );
}

function renderHeatmap(rows) {
  if (!rows.length) {
    renderEmpty(refs.heatmap, "Aucune donnee domaine exploitable.", "Actualiser", loadAnalytics);
    return;
  }
  refs.heatmap.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>Domaine</th>
          <th>Extraites</th>
          <th>Applicables</th>
          <th>Conformes</th>
          <th>Low</th>
          <th>Medium</th>
          <th>High</th>
          <th>Critical</th>
          <th>Score</th>
        </tr>
      </thead>
      <tbody>
        ${rows.map((row) => `
          <tr>
            <td style="text-align:left">${escapeHtml(row.domain)}</td>
            <td>${numberFmt(row.extracted)}</td>
            <td>${numberFmt(row.applicable)}</td>
            <td class="${heatClass("CONFORME", row.conforme)}">${numberFmt(row.conforme)}</td>
            <td class="${heatClass("LOW", row.low)}">${numberFmt(row.low)}</td>
            <td class="${heatClass("MEDIUM", row.medium)}">${numberFmt(row.medium)}</td>
            <td class="${heatClass("HIGH", row.high)}">${numberFmt(row.high)}</td>
            <td class="${heatClass("CRITICAL", row.critical)}">${numberFmt(row.critical)}</td>
            <td class="hm-score">${numberFmt(row.riskScore)}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>
  `;
}

function buildInsights(scope, heatRows, payload) {
  const insights = [];
  const topRiskDomain = heatRows.length ? heatRows[0] : null;

  if (scope.conformityRate < 0.3) {
    insights.push({
      tone: "critical",
      title: "Conformite operationnelle faible",
      text: `Le taux de conformite est a ${percentValue(scope.conformityRate)}. Prioriser la fermeture des NC critiques/majeures avant une nouvelle analyse.`,
    });
  } else if (scope.conformityRate < 0.6) {
    insights.push({
      tone: "warning",
      title: "Conformite partielle",
      text: `Le taux de conformite reste limite (${percentValue(scope.conformityRate)}). Un plan d'actions cible par domaine est recommande.`,
    });
  } else {
    insights.push({
      tone: "",
      title: "Conformite en progression",
      text: `Le niveau de conformite atteint ${percentValue(scope.conformityRate)}. Stabiliser la cadence de preuve pour consolider ce niveau.`,
    });
  }

  if (scope.proofCoverageRate < 0.45) {
    insights.push({
      tone: "warning",
      title: "Couverture de preuve insuffisante",
      text: `Seules ${percentValue(scope.proofCoverageRate)} des exigences applicables sont reliees a des preuves. Renforcer la collecte documentaire.`,
    });
  } else {
    insights.push({
      tone: "",
      title: "Couverture de preuve acceptable",
      text: `${percentValue(scope.proofCoverageRate)} des exigences applicables disposent d'elements de preuve relies.`,
    });
  }

  if (scope.actionsOpen > scope.conforme) {
    insights.push({
      tone: "warning",
      title: "Backlog d'actions a surveiller",
      text: `${numberFmt(scope.actionsOpen)} actions restent ouvertes pour ${numberFmt(scope.conforme)} exigences conformes. Ajuster les responsables et echeances.`,
    });
  }

  if (topRiskDomain && topRiskDomain.riskScore > 0) {
    insights.push({
      tone: topRiskDomain.critical > 0 ? "critical" : "warning",
      title: `Domaine prioritaire: ${topRiskDomain.domain}`,
      text: `Score risque ${numberFmt(topRiskDomain.riskScore)} (Critiques ${numberFmt(topRiskDomain.critical)}, High ${numberFmt(topRiskDomain.high)}).`,
    });
  }

  const quality = payload.overview?.quality_snapshot || {};
  if (toNum(quality.raw_to_final_conversion_rate) > 0 && toNum(quality.raw_to_final_conversion_rate) < 0.4) {
    insights.push({
      tone: "warning",
      title: "Rendement d'extraction a ameliorer",
      text: `Le taux brut -> final est de ${percentValue(quality.raw_to_final_conversion_rate)}. Revoir calibration extraction et validation humaine.`,
    });
  }

  return insights.slice(0, 6);
}

function renderInsights(scope, heatRows, payload) {
  const insights = buildInsights(scope, heatRows, payload);
  if (!insights.length) {
    renderEmpty(refs.insights, "Aucune recommandation generee.", "Actualiser", loadAnalytics);
    return;
  }
  refs.insights.innerHTML = insights.map((insight) => `
    <article class="insight-item ${escapeHtml(insight.tone || "")}">
      <div class="insight-title">${escapeHtml(insight.title)}</div>
      <div class="insight-text">${escapeHtml(insight.text)}</div>
    </article>
  `).join("");
}

function rerender() {
  if (!state.payload) {
    return;
  }

  const scope = prepareScope(state.payload);
  renderPills(scope.domains);
  renderContext(scope);
  renderKpis(scope);
  renderRatios(scope);
  renderLineChart(buildTrendSeries(scope, state.payload));
  renderDonut(scope);
  renderFunnel(scope);
  renderTopGaps(scope, state.payload);
  renderTopDocuments(scope, state.payload);
  renderQuality(scope, state.payload);
  const heatRows = buildHeatmapRows(scope, state.payload);
  renderHeatmap(heatRows);
  renderInsights(scope, heatRows, state.payload);
}

async function loadAnalytics() {
  setLoadingState();
  try {
    const tenant = getAuth().tenant_id;
    const [overview, applicability, compliance, validations, proofs, families, gate] = await Promise.all([
      api.analyticsOverview(tenant),
      api.applicabilitySummary(tenant),
      api.complianceSummary(tenant),
      api.validations(tenant, 800),
      api.companyProofs(tenant, 200),
      api.analyticsFamilies(tenant),
      api.analyticsGate(tenant),
    ]);
    state.payload = { overview, applicability, compliance, validations, proofs, families, gate };
    rerender();
  } catch (error) {
    destroyCharts();
    renderAllErrors(error);
  }
}

refs.refreshBtn?.addEventListener("click", loadAnalytics);

await loadAnalytics();
