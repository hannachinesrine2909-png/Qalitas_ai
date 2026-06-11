import { getAuth, initShell, isReadOnlyRole } from "/ui/js/auth.js";
import { api } from "/ui/js/api.js";
import { escapeHtml, formatDate, numberFmt, renderEmpty, renderError, statusBadge } from "/ui/js/utils.js";

const session = initShell("upload");
if (!session) {
  throw new Error("Session absente");
}

const refs = {
  form: document.getElementById("upload-form"),
  dropzone: document.getElementById("dropzone"),
  dropzoneTitle: document.getElementById("dropzone-title"),
  dropzoneFile: document.getElementById("dropzone-file"),
  file: document.getElementById("pdf-file"),
  docName: document.getElementById("doc-name"),
  docType: document.getElementById("doc-type"),
  docDomain: document.getElementById("doc-domain"),
  docReference: document.getElementById("doc-reference"),
  duplicateMode: document.getElementById("duplicate-mode"),
  stepA2: document.getElementById("step-a2"),
  stepA3: document.getElementById("step-a3"),
  stepEmbed: document.getElementById("step-embed"),
  extractLimit: document.getElementById("extract-limit"),
  timeoutSegmentSec: document.getElementById("timeout-segment-sec"),
  timeoutExtractSec: document.getElementById("timeout-extract-sec"),
  submit: document.getElementById("submit-run"),
  stopBtn: document.getElementById("stop-run"),
  resumeBtn: document.getElementById("resume-run"),
  liveSteps: document.getElementById("live-steps"),
  progressBar: document.getElementById("progress-bar"),
  progressText: document.getElementById("progress-text"),
  liveLog: document.getElementById("live-log"),
  recentRuns: document.getElementById("recent-runs"),
  clearRunsHistory: document.getElementById("clear-runs-history"),
  runStatusText: document.getElementById("run-status-text"),
  runErrorBox: document.getElementById("run-error-box"),
  resetGuidance: document.getElementById("upload-reset-guidance"),
  workflowStageBadge: document.getElementById("workflow-stage-badge"),
  workflowStateTitle: document.getElementById("workflow-state-title"),
  workflowStateCopy: document.getElementById("workflow-state-copy"),
  workflowStateMeta: document.getElementById("workflow-state-meta"),
  workflowOpenReview: document.getElementById("workflow-open-review"),
  workflowRunA2: document.getElementById("workflow-run-a2"),
  workflowRunA3: document.getElementById("workflow-run-a3"),
  workflowRunA2A3: document.getElementById("workflow-run-a2a3"),
  workflowRunA4: document.getElementById("workflow-run-a4"),
  workflowOpenAssistant: document.getElementById("workflow-open-assistant"),
};

const state = {
  steps: {
    A1: "pending",
    REVIEW: "pending",
    A2: "pending",
    A3: "pending",
    EMB: "pending",
  },
  isExecuting: false,
  resumeJobId: "",
  activeJobId: "",
  pausedJobId: "",
  currentJobId: "",
  currentPlan: {
    A1: true,
    A2: false,
    A3: false,
    EMB: false,
  },
  expandedRunId: "",
  runSummaryCache: {},
  workflowData: {
    overview: null,
    applicability: null,
    compliance: null,
  },
  recentRuns: [],
  latestAvailableDoc: null,
};

const POLL_INTERVAL_MS = 2500;
const RUNNING_HEARTBEAT_MS = 30000;
const MAX_LOG_LINES = 250;
const MAX_FILE_SIZE_MB = 50;
const MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024;
const RESUME_MAX_WAIT_MS = 12 * 60 * 60 * 1000;
const ACTIVE_RUN_STATUSES = new Set(["QUEUED", "PENDING", "RUNNING"]);
const PAUSED_RUN_STATUSES = new Set(["PAUSED"]);
const PIPELINE_ORDER = ["A1", "A2", "A3", "EMB"];
const STEP_LABELS = {
  A1: "Extraction",
  REVIEW: "Validation A1",
  A2: "Applicabilite",
  A3: "Conformite",
  EMB: "Indexation",
};
const DEFAULT_TIMEOUTS = {
  ingest: 900,
  segment: 7200,
  extract: 10800,
  backfill: 900,
  export: 900,
};
const WAIT_BUDGETS_MS = {
  A2: 45 * 60 * 1000,
  A3: 45 * 60 * 1000,
  Embeddings: 30 * 60 * 1000,
};

function deriveRegistryCounts(overview = null) {
  const source = overview || {};
  return {
    total: Number(source.requirements_total || 0),
    promoted: Number(source.promoted_total || 0),
    draft: Number(source.draft_total || 0),
    toValidate: Number(source.to_validate_total || 0),
    reject: Number(source.reject_total || 0),
  };
}

function readPipelineSelection() {
  const a3 = Boolean(refs.stepA3?.checked);
  const a2 = Boolean(refs.stepA2?.checked) || a3;
  return {
    A1: true,
    A2: a2,
    A3: a3,
    EMB: Boolean(refs.stepEmbed?.checked),
  };
}

function syncPipelineDependencies(origin = "") {
  const a2Checked = Boolean(refs.stepA2?.checked);
  const a3Checked = Boolean(refs.stepA3?.checked);
  if (origin === "A2" && !a2Checked && a3Checked && refs.stepA3) {
    refs.stepA3.checked = false;
  }
  if (a3Checked && !a2Checked && refs.stepA2) {
    refs.stepA2.checked = true;
  }
  state.currentPlan = clonePlan(readPipelineSelection());
}

function pipelinePlanLabel(plan = {}) {
  return ["Extraction", "Validation", plan?.A2 ? "Applicabilité" : null, plan?.A3 ? "Conformité" : null, plan?.EMB ? "Assistant" : null]
    .filter(Boolean)
    .join(" - ");
}

function canRunFollowupsForRow(row) {
  const status = String(row?.status || "").trim().toUpperCase();
  const docId = String(row?.doc_id || "").trim();
  return Boolean(docId) && status === "DONE" && row?._docAvailable === true;
}

async function annotateRunDocAvailability(rows = []) {
  const annotated = [];
  for (const row of rows || []) {
    const next = { ...row, _docAvailable: false };
    const docId = String(row?.doc_id || "").trim();
    const status = String(row?.status || "").trim().toUpperCase();
    if (docId && status !== "FAILED" && status !== "ERROR" && status !== "CANCELLED") {
      try {
        const summary = await api.documentSummary(docId, getAuth().tenant_id);
        next._docAvailable = true;
        next._docSummary = summary;
      } catch {
        next._docAvailable = false;
      }
    }
    annotated.push(next);
  }
  return annotated;
}

function backendStepToVisual(status) {
  const norm = String(status || "").trim().toUpperCase();
  if (norm === "DONE") return "done";
  if (norm === "RUNNING") return "running";
  if (norm === "PAUSED") return "paused";
  if (norm === "SKIPPED") return "skipped";
  if (norm === "ERROR" || norm === "FAILED") return "error";
  return "pending";
}

function runStatusSummary(run) {
  const status = String(run?.status || "").trim().toUpperCase();
  const stageMessage = String(run?.stage_message || "").trim();
  if (stageMessage) {
    return stageMessage;
  }
  if (status === "DONE") {
    return "Traitement terminé";
  }
  if (status === "FAILED" || status === "ERROR") {
    return "Traitement en échec";
  }
  if (status === "PAUSED") {
    return "Traitement en pause";
  }
  return "Traitement en attente";
}

function runHistorySummary(run) {
  const status = String(run?.status || "").trim().toUpperCase();
  const summary = run?.document_summary || {};
  const promoted = Number(summary.promoted_count || summary.promoted_total || 0);
  const draft = Number(summary.draft_count || summary.draft_total || 0);
  const toValidate = Number(summary.to_validate_count || summary.to_validate_total || 0);
  const pendingReview = draft + toValidate;
  const a2Status = stepStatusForRun(run, "A2");
  const a3Status = stepStatusForRun(run, "A3");
  const embStatus = stepStatusForRun(run, "EMB");
  const a2 = getStageResult(run, "A2");
  const a3 = getStageResult(run, "A3");
  const a2Errors = Number(a2?.counts?.ERROR || 0);
  const a3Errors = Number(a3?.counts?.ERROR || 0);
  const currentStage = String(run?.current_stage || normalizeRunType(run) || "").trim().toUpperCase();
  const currentStageLabel = STEP_LABELS[currentStage] || "Traitement";

  if (status === "RUNNING" || status === "QUEUED" || status === "PENDING") {
    return `${currentStageLabel} en cours`;
  }
  if (status === "PAUSED") {
    return `${currentStageLabel} en pause`;
  }
  if (status === "FAILED" || status === "ERROR") {
    return "Traitement en erreur";
  }
  if (a3Errors > 0 && Number(a3?.total || 0) <= 0) {
    return "Conformité en échec";
  }
  if (a2Errors > 0 && Number(a2?.total || 0) <= 0) {
    return "Applicabilité en échec";
  }
  if (a3Status === "DONE") {
    return "Applicabilité et conformité prêtes";
  }
  if (a2Status === "DONE") {
    return "Applicabilité prête";
  }
  if (pendingReview > 0) {
    return "Registre en revue";
  }
  if (promoted > 0) {
    return "Registre final prêt";
  }
  if (embStatus === "DONE") {
    return "Assistant prêt";
  }
  if (status === "DONE") {
    return "Extraction terminée";
  }
  return runStatusSummary(run);
}

function clonePlan(plan = null) {
  const next = { A1: true, A2: false, A3: false, EMB: false };
  if (!plan || typeof plan !== "object") {
    return next;
  }
  next.A1 = true;
  next.A2 = Boolean(plan.A2);
  next.A3 = Boolean(plan.A3);
  next.EMB = Boolean(plan.EMB);
  if (next.A3 && !next.A2) {
    next.A2 = true;
  }
  return next;
}

function stepStatusForRun(run, stage) {
  return String(run?.pipeline_steps?.[stage] || "").trim().toUpperCase();
}

function shortId(value, size = 8) {
  const text = String(value || "").trim();
  if (!text) return "";
  return text.length <= size ? text : text.slice(0, size);
}

function getStageResult(run, stage) {
  const source = run && typeof run === "object" ? run : {};
  const runType = normalizeRunType(source);
  if (stage === "A2") {
    return source.a2_result || (runType === "A2" ? source.result : null) || {};
  }
  if (stage === "A3") {
    return source.a3_result || (runType === "A3" ? source.result : null) || {};
  }
  return {};
}

function getRunDisplayTitle(run) {
  const source = run && typeof run === "object" ? run : {};
  return String(
    source.file_name
    || source.title
    || source?._docSummary?.title
    || (source.doc_id ? `Document ${shortId(source.doc_id)}` : "")
    || (source.job_id ? `Run ${shortId(source.job_id)}` : "")
    || "Run"
  ).trim();
}

function getRunDisplayMeta(run) {
  const source = run && typeof run === "object" ? run : {};
  const parts = [];
  const updated = formatDate(source.updated_at);
  if (updated) parts.push(updated);
  if (source.doc_id) parts.push(`Doc ${shortId(source.doc_id)}`);
  if (source.job_id) parts.push(`Run ${shortId(source.job_id)}`);
  return parts.join(" - ");
}

function stepStatusLabel(status) {
  const norm = String(status || "").trim().toUpperCase();
  if (norm === "DONE") return "Terminée";
  if (norm === "SKIPPED") return "Ignorée";
  if (norm === "PAUSED") return "En pause";
  if (norm === "ERROR" || norm === "FAILED") return "En erreur";
  if (norm === "RUNNING") return "En cours";
  return "En attente";
}

function compactRunSummaryHtml(run, detail = null) {
  const source = detail && typeof detail === "object" ? detail : run || {};
  const summary = source.document_summary || {};
  const latest = summary.latest_extraction || {};
  const a2 = getStageResult(source, "A2");
  const a3 = getStageResult(source, "A3");
  const a2Counts = a2.counts || {};
  const a3Counts = a3.counts || {};
  const a2Errors = Number(a2Counts.ERROR || 0);
  const a3Errors = Number(a3Counts.ERROR || 0);
  const pieces = [];

  if (summary.doc_id) {
    pieces.push(`<div class="page-subtitle"><strong>Exigences A1</strong> - ${numberFmt(summary.requirements_total || 0)} candidat(s) | Registre final ${numberFmt(summary.promoted_count || 0)} | Pré-validées auto ${numberFmt(summary.draft_count || 0)} | À valider ${numberFmt(summary.to_validate_count || 0)} | Rejetées ${numberFmt(summary.reject_count || 0)}</div>`);
    pieces.push(`<div class="page-subtitle">Dernière extraction - ${numberFmt(latest.requirements_inserted || 0)} proposition(s)</div>`);
  } else if (stepStatusForRun(source, "A1")) {
    pieces.push(`<div class="page-subtitle"><strong>Exigences A1</strong> - ${escapeHtml(stepStatusLabel(stepStatusForRun(source, "A1")))}</div>`);
  }

  if (source?.pipeline_plan?.A2) {
    const a2Status = stepStatusForRun(source, "A2");
    if (a2Status === "SKIPPED") {
      pieces.push(`<div class="page-subtitle"><strong>Applicabilité</strong> - Ignorée</div>`);
    } else if (a2 && typeof a2 === "object" && Object.keys(a2Counts).length) {
      pieces.push(`<div class="page-subtitle"><strong>Applicabilité</strong> - ${numberFmt(a2.total || 0)} décision(s) | Applicable ${numberFmt(a2Counts.APPLICABLE || 0)} | Futur ${numberFmt(a2Counts.APPLICABLE_FUTUR || 0)} | Non applicable ${numberFmt(a2Counts.NON_APPLICABLE || 0)} | Sous conditions ${numberFmt(a2Counts.APPLICABLE_SOUS_CONDITIONS || 0)} | Incertain ${numberFmt(a2Counts.INCERTAIN || 0)}${a2Errors > 0 ? ` | Erreurs ${numberFmt(a2Errors)}` : ""}</div>`);
    } else {
      pieces.push(`<div class="page-subtitle"><strong>Applicabilité</strong> - ${escapeHtml(stepStatusLabel(a2Status))}</div>`);
    }
  }

  if (source?.pipeline_plan?.A3) {
    const a3Status = stepStatusForRun(source, "A3");
    if (a3Status === "SKIPPED") {
      pieces.push(`<div class="page-subtitle"><strong>Conformité</strong> - Ignorée</div>`);
    } else if (a3 && typeof a3 === "object" && Object.keys(a3Counts).length) {
      pieces.push(`<div class="page-subtitle"><strong>Conformité</strong> - ${numberFmt(a3.total || 0)} évaluation(s) | Conforme ${numberFmt(a3Counts.CONFORME || 0)} | Partielle ${numberFmt(a3Counts.PARTIELLEMENT_CONFORME || 0)} | Non conforme ${numberFmt(a3Counts.NON_CONFORME || 0)} | Absence preuve ${numberFmt(a3Counts.ABSENCE_DE_PREUVE || 0)}${a3Errors > 0 ? ` | Erreurs ${numberFmt(a3Errors)}` : ""}</div>`);
      if (a3Errors > 0 && Number(a3.total || 0) <= 0) {
        const sample = Array.isArray(a3.error_samples) && a3.error_samples.length ? ` - ${a3.error_samples[0]}` : "";
        pieces.push(`<div class="page-subtitle" style="color:#991b1b"><strong>Moteur A3</strong> - Toutes les évaluations ont échoué${escapeHtml(sample)}</div>`);
      }
    } else {
      pieces.push(`<div class="page-subtitle"><strong>Conformité</strong> - ${escapeHtml(stepStatusLabel(a3Status))}</div>`);
    }
  }

  if (source?.pipeline_plan?.EMB) {
    const embStatus = stepStatusForRun(source, "EMB");
    const indexed = Number(source?.indexed || 0);
    pieces.push(`<div class="page-subtitle"><strong>Assistant expert</strong> - ${escapeHtml(stepStatusLabel(embStatus))}${embStatus !== "SKIPPED" || indexed > 0 ? ` | ${numberFmt(indexed)} contenu(s) indexé(s)` : ""}</div>`);
  }

  pieces.push(`<div class="page-subtitle"><strong>État du traitement</strong> - ${escapeHtml(runStatusSummary(source))}</div>`);

  const errorBits = [];
  if (source?.error_category) errorBits.push(String(source.error_category));
  if (source?.failed_step) errorBits.push(String(source.failed_step));
  if (source?.error && String(source.error).trim()) errorBits.push(String(source.error).trim());
  if (errorBits.length) {
    pieces.push(`<div class="page-subtitle" style="color:#991b1b"><strong>Erreur</strong> - ${escapeHtml(errorBits.join(" | "))}</div>`);
  }

  if (!pieces.length) {
    return `<div class="page-subtitle">Aucun résumé disponible pour ce run.</div>`;
  }
  return pieces.join("");
}

function applyRunPipelineState(run, options = {}) {
  if (!run || typeof run !== "object") {
    return;
  }
  const syncCheckboxes = options.syncCheckboxes === true;
  const plan = run.pipeline_plan && typeof run.pipeline_plan === "object" ? run.pipeline_plan : null;
  const runType = normalizeRunType(run);
  const fallbackPlan = runType === "A3"
    ? { A1: true, A2: true, A3: true, EMB: false }
    : runType === "A2"
      ? { A1: true, A2: true, A3: false, EMB: false }
      : { A1: true, A2: false, A3: false, EMB: false };
  state.currentPlan = clonePlan(plan || fallbackPlan);
  if (syncCheckboxes && plan) {
    refs.stepA2.checked = Boolean(plan.A2);
    refs.stepA3.checked = Boolean(plan.A3);
    refs.stepEmbed.checked = Boolean(plan.EMB);
  }
  const steps = run.pipeline_steps && typeof run.pipeline_steps === "object" ? run.pipeline_steps : null;
  if (steps) {
    const effectiveSteps = { ...steps };
    const a2Result = getStageResult(run, "A2");
    const a3Result = getStageResult(run, "A3");
    const a2Loaded = Number(a2Result?.engine_stats?.requirements_loaded || 0);
    const a2Errors = Number(a2Result?.counts?.ERROR || 0);
    const a3Total = Number(a3Result?.total || 0);
    const a3Errors = Number(a3Result?.counts?.ERROR || 0);
    const indexed = Number(run?.indexed || 0);
    if (String(effectiveSteps.A2 || "").toUpperCase() === "DONE" && run?.pipeline_plan?.A2 && a2Loaded <= 0 && a2Errors <= 0) {
      effectiveSteps.A2 = "SKIPPED";
    }
    if (String(effectiveSteps.A3 || "").toUpperCase() === "DONE" && run?.pipeline_plan?.A3 && a3Total <= 0 && a3Errors <= 0) {
      effectiveSteps.A3 = "SKIPPED";
    }
    if (String(effectiveSteps.EMB || "").toUpperCase() === "DONE" && run?.pipeline_plan?.EMB && indexed <= 0) {
      effectiveSteps.EMB = "SKIPPED";
    }
    for (const stage of PIPELINE_ORDER) {
      state.steps[stage] = backendStepToVisual(effectiveSteps[stage]);
    }
    if (runType === "A2" || runType === "A3" || String(effectiveSteps.A1 || "").toUpperCase() === "DONE") {
      const a2Started = ["RUNNING", "DONE", "SKIPPED", "PAUSED", "ERROR", "FAILED"].includes(String(effectiveSteps.A2 || "").toUpperCase());
      const a3Started = ["RUNNING", "DONE", "SKIPPED", "PAUSED", "ERROR", "FAILED"].includes(String(effectiveSteps.A3 || "").toUpperCase());
      state.steps.REVIEW = (runType === "A2" || runType === "A3" || a2Started || a3Started) ? "done" : "action";
    } else {
      state.steps.REVIEW = "pending";
    }
    renderSteps();
    return;
  }
  const currentStage = String(run.current_stage || "").trim().toUpperCase();
  if (currentStage && PIPELINE_ORDER.includes(currentStage)) {
    resetStepsPending();
    for (const stage of PIPELINE_ORDER) {
      if (stage === currentStage) {
        state.steps[stage] = backendStepToVisual(run.status === "DONE" ? "DONE" : "RUNNING");
        break;
      }
      state.steps[stage] = "done";
    }
    state.steps.REVIEW = currentStage === "A1" ? "pending" : "done";
    renderSteps();
  }
}

function docContextKey(suffix) {
  return `qalitas.${suffix}.${getAuth().tenant_id}`;
}

function rememberRunDocumentContext({ docId = "", fileName = "", source = "" } = {}) {
  const safeDocId = String(docId || "").trim();
  const safeFileName = String(fileName || "").trim();
  const safeSource = String(source || "").trim();
  if (safeDocId) {
    localStorage.setItem(docContextKey("last_doc_id"), safeDocId);
  }
  if (safeFileName) {
    localStorage.setItem(docContextKey("last_doc_name"), safeFileName);
  }
  if (safeSource) {
    localStorage.setItem(docContextKey("last_doc_source"), safeSource);
  }
}

function validateFile(file) {
  if (!file) return "Aucun fichier sélectionné.";
  const isPdf = file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
  if (!isPdf) return `Type de fichier non accepté : "${file.type || file.name}". Seuls les fichiers PDF sont acceptés.`;
  if (file.size > MAX_FILE_SIZE_BYTES) return `Fichier trop volumineux : ${(file.size / 1024 / 1024).toFixed(1)} Mo. Limite : ${MAX_FILE_SIZE_MB} Mo.`;
  return null;
}

function toInt(value, fallback, min = 0) {
  const num = Number.parseInt(String(value ?? ""), 10);
  if (!Number.isFinite(num)) {
    return fallback;
  }
  return Math.max(min, num);
}

function logLine(text) {
  const stamp = new Date().toLocaleTimeString("fr-FR");
  const current = String(refs.liveLog.textContent || "");
  const prefix = current ? `${current}\n` : "";
  const next = `${prefix}[${stamp}] ${text}`;
  const lines = next.split("\n");
  refs.liveLog.textContent = lines.slice(-MAX_LOG_LINES).join("\n");
  refs.liveLog.scrollTop = refs.liveLog.scrollHeight;
}

function resetLog() {
  refs.liveLog.textContent = "Traitement initialisé.";
}

function renderUploadGuidance(message, tone = "info") {
  if (!refs.resetGuidance) {
    return;
  }
  if (!message) {
    refs.resetGuidance.classList.add("hidden");
    return;
  }
  refs.resetGuidance.classList.remove("hidden");
  refs.resetGuidance.textContent = message;
  if (tone === "danger") {
    refs.resetGuidance.style.background = "#fff1f2";
    refs.resetGuidance.style.borderColor = "rgba(220, 38, 38, 0.45)";
    refs.resetGuidance.style.color = "#991b1b";
    return;
  }
  if (tone === "success") {
    refs.resetGuidance.style.background = "#f0fdf4";
    refs.resetGuidance.style.borderColor = "rgba(22, 163, 74, 0.35)";
    refs.resetGuidance.style.color = "#166534";
    return;
  }
  refs.resetGuidance.style.background = "#fffbeb";
  refs.resetGuidance.style.borderColor = "rgba(245, 158, 11, 0.6)";
  refs.resetGuidance.style.color = "#92400e";
}

function setWorkflowButtonVisibility(button, visible) {
  if (!button) {
    return;
  }
  button.classList.toggle("hidden", !visible);
}

function renderWorkflowMeta(items = []) {
  if (!refs.workflowStateMeta) {
    return;
  }
  const clean = (items || []).filter(Boolean);
  refs.workflowStateMeta.innerHTML = clean.length
    ? clean.map((item) => `<span class="workflow-meta-chip">${escapeHtml(item)}</span>`).join("")
    : "";
}

function markWorkflowSteps(activeStep = "A1", completed = []) {
  const ordered = ["A1", "REVIEW", "A2", "A3", "A4"];
  document.querySelectorAll("[data-workflow-step]").forEach((node) => {
    const step = String(node.getAttribute("data-workflow-step") || "");
    node.classList.remove("is-active", "is-complete", "is-waiting");
    if (completed.includes(step)) {
      node.classList.add("is-complete");
      return;
    }
    if (step === activeStep) {
      node.classList.add("is-active");
      return;
    }
    if (ordered.indexOf(step) > ordered.indexOf(activeStep)) {
      node.classList.add("is-waiting");
    }
  });
}

function findLatestAvailableDoc(rows = []) {
  const candidates = (rows || []).filter((row) => String(row?.doc_id || "").trim() && row?._docAvailable === true);
  if (!candidates.length) {
    return null;
  }
  candidates.sort((a, b) => parseIsoDateMs(b?.updated_at || b?.created_at) - parseIsoDateMs(a?.updated_at || a?.created_at));
  return candidates[0];
}

function assistantIndexedFromRuns(rows = []) {
  return (rows || []).some((row) => Number(row?.indexed || 0) > 0 || String(stepStatusForRun(row, "EMB") || "").toUpperCase() === "DONE");
}

function renderWorkflowGuide() {
  const overview = state.workflowData.overview || {};
  const applicability = state.workflowData.applicability || {};
  const compliance = state.workflowData.compliance || {};
  const counts = deriveRegistryCounts(overview);
  const docsTotal = Number(overview?.documents_total || 0);
  const a2Total = Number(applicability?.total || 0);
  const a3Total = Number(compliance?.total_checks || 0);
  const pendingReview = counts.draft + counts.toValidate;
  const latestDoc = state.latestAvailableDoc;
  const assistantReady = assistantIndexedFromRuns(state.recentRuns);
  const activeRun = pickLatestActiveRun(state.recentRuns);
  const pausedRun = pickLatestPausedRun(state.recentRuns);

  let stage = "A1";
  let badge = "Étape 1";
  let title = "Importer un texte juridique";
  let copy = "En attente de document.";
  let completed = [];
  const meta = [];

  setWorkflowButtonVisibility(refs.workflowOpenReview, false);
  setWorkflowButtonVisibility(refs.workflowRunA2, false);
  setWorkflowButtonVisibility(refs.workflowRunA3, false);
  setWorkflowButtonVisibility(refs.workflowRunA2A3, false);
  setWorkflowButtonVisibility(refs.workflowRunA4, false);
  setWorkflowButtonVisibility(refs.workflowOpenAssistant, false);

  if (activeRun) {
    const runningStage = normalizeRunType(activeRun);
    stage = runningStage === "EMB" ? "A4" : runningStage;
    badge = "En cours";
    title = "Traitement en cours";
    copy = runStatusSummary(activeRun);
    completed = runningStage === "A2"
      ? ["A1", "REVIEW"]
      : runningStage === "A3"
        ? ["A1", "REVIEW", "A2"]
        : runningStage === "EMB"
          ? ["A1", "REVIEW", "A2", "A3"]
          : [];
    meta.push(`Run actif: ${activeRun.file_name || activeRun.job_id || "Traitement"}`);
  } else if (pausedRun) {
    const pausedStage = normalizeRunType(pausedRun);
    stage = pausedStage === "EMB" ? "A4" : pausedStage;
    badge = "En pause";
    title = "Traitement en pause";
    copy = "Run en pause.";
    completed = pausedStage === "A2"
      ? ["A1", "REVIEW"]
      : pausedStage === "A3"
        ? ["A1", "REVIEW", "A2"]
        : pausedStage === "EMB"
          ? ["A1", "REVIEW", "A2", "A3"]
          : [];
    meta.push(`Run en pause: ${pausedRun.file_name || pausedRun.job_id || "Traitement"}`);
  } else if (docsTotal === 0 && counts.total === 0) {
    meta.push("Aucun document réglementaire actif");
  } else if (pendingReview > 0 || (counts.total > 0 && counts.promoted === 0)) {
    stage = "REVIEW";
    badge = "Étape 2";
    title = "Finaliser la revue A1";
    copy = "Validation requise.";
    completed = ["A1"];
    meta.push(`${numberFmt(counts.promoted)} validée(s)`);
    meta.push(`${numberFmt(counts.draft)} pré-validée(s) auto`);
    meta.push(`${numberFmt(counts.toValidate)} à valider`);
    setWorkflowButtonVisibility(refs.workflowOpenReview, true);
  } else if (counts.promoted > 0 && a2Total === 0) {
    stage = "A2";
    badge = "Étape 3";
    title = "Lancer l'applicabilité";
    copy = latestDoc
      ? `Registre prêt: ${latestDoc.file_name || latestDoc.doc_id}.`
      : "Registre prêt.";
    completed = ["A1", "REVIEW"];
    meta.push(`${numberFmt(counts.promoted)} exigence(s) dans le registre final`);
    if (latestDoc?.file_name) {
      meta.push(`Document actif: ${latestDoc.file_name}`);
      setWorkflowButtonVisibility(refs.workflowRunA2, true);
      setWorkflowButtonVisibility(refs.workflowRunA2A3, true);
    } else {
      meta.push("Document source introuvable dans l'historique récent");
    }
  } else if (a2Total > 0 && a3Total === 0) {
    stage = "A3";
    badge = "Étape 4";
    title = "Lancer la conformité";
    copy = latestDoc
      ? `Applicabilité disponible: ${latestDoc.file_name || latestDoc.doc_id}.`
      : "Applicabilité disponible.";
    completed = ["A1", "REVIEW", "A2"];
    meta.push(`${numberFmt(a2Total)} décision(s) d'applicabilité disponible(s)`);
    if (latestDoc?.file_name || latestDoc?.doc_id) {
      setWorkflowButtonVisibility(refs.workflowRunA3, true);
      setWorkflowButtonVisibility(refs.workflowRunA2A3, true);
    } else {
      meta.push("Document source introuvable dans l'historique récent");
    }
  } else if (a3Total > 0 && !assistantReady) {
    stage = "A4";
    badge = "Étape 5";
    title = "Préparer l'assistant expert";
    copy = "Contexte prêt pour indexation.";
    completed = ["A1", "REVIEW", "A2", "A3"];
    meta.push(`${numberFmt(a3Total)} analyse(s) de conformité disponible(s)`);
    setWorkflowButtonVisibility(refs.workflowRunA4, true);
  } else if (a3Total > 0 && assistantReady) {
    stage = "A4";
    badge = "Prêt";
    title = "Assistant prêt à être utilisé";
    copy = "Contexte indexé.";
    completed = ["A1", "REVIEW", "A2", "A3", "A4"];
    meta.push(`${numberFmt(a3Total)} analyse(s) de conformité disponible(s)`);
    setWorkflowButtonVisibility(refs.workflowOpenAssistant, true);
    setWorkflowButtonVisibility(refs.workflowRunA4, true);
  } else if (counts.promoted > 0) {
    stage = "A2";
    badge = "Étape 3";
    title = "Registre final disponible";
    copy = "Exigences validées disponibles.";
    completed = ["A1", "REVIEW"];
    meta.push(`${numberFmt(counts.promoted)} exigence(s) validée(s)`);
    setWorkflowButtonVisibility(refs.workflowRunA2, true);
    setWorkflowButtonVisibility(refs.workflowRunA2A3, true);
  }

  if (refs.workflowStageBadge) {
    refs.workflowStageBadge.textContent = badge;
  }
  if (refs.workflowStateTitle) {
    refs.workflowStateTitle.textContent = title;
  }
  if (refs.workflowStateCopy) {
    refs.workflowStateCopy.textContent = copy;
  }
  renderWorkflowMeta(meta);
  markWorkflowSteps(stage, completed);
}

async function loadUploadGuidance() {
  try {
    const tenant = getAuth().tenant_id;
    const [overview, applicability, compliance] = await Promise.all([
      api.dashboardOverview(tenant),
      api.applicabilitySummary(tenant),
      api.complianceSummary(tenant),
    ]);
    const docsTotal = Number(overview?.documents_total || 0);
    const counts = deriveRegistryCounts(overview);
    const requirementsTotal = counts.total;
    const a2Total = Number(applicability?.total || 0);
    const a3Total = Number(compliance?.total_checks || 0);
    state.workflowData = { overview, applicability, compliance };

    if (requirementsTotal === 0 && (a2Total > 0 || a3Total > 0)) {
      renderUploadGuidance(
        "Résultats A2/A3 hérités sans registre A1 actif.",
        "danger",
      );
      renderWorkflowGuide();
      return;
    }
    if (docsTotal === 0 && requirementsTotal === 0) {
      renderUploadGuidance(
        "Base réglementaire vide.",
        "info",
      );
      renderWorkflowGuide();
      return;
    }
    if (counts.promoted === 0 && requirementsTotal > 0 && counts.draft === 0 && counts.toValidate === 0) {
      renderUploadGuidance(
        "Registre final vide.",
        "info",
      );
      renderWorkflowGuide();
      return;
    }
    if (counts.toValidate > 0 || counts.draft > 0) {
      renderUploadGuidance(
        `Revue A1: ${numberFmt(counts.toValidate)} à valider.`,
        "info",
      );
      renderWorkflowGuide();
      return;
    }
    renderUploadGuidance("", "success");
    renderWorkflowGuide();
  } catch {
    state.workflowData = { overview: null, applicability: null, compliance: null };
    renderUploadGuidance(
      "Corpus non vérifié.",
      "info",
    );
    renderWorkflowGuide();
  }
}

function setRunStatus(text) {
  if (refs.runStatusText) {
    refs.runStatusText.textContent = text;
  }
}

function clearRunError() {
  if (!refs.runErrorBox) {
    return;
  }
  refs.runErrorBox.classList.add("hidden");
  refs.runErrorBox.textContent = "";
}

function showRunError(message) {
  if (!refs.runErrorBox) {
    return;
  }
  refs.runErrorBox.textContent = message;
  refs.runErrorBox.classList.remove("hidden");
}

function updateSelectedFileUI(file) {
  const hasFile = Boolean(file);
  if (refs.dropzoneTitle) {
    refs.dropzoneTitle.textContent = hasFile ? "PDF prêt pour extraction A1" : "Glisser-déposer un PDF juridique ici";
  }
  if (refs.dropzoneFile) {
    if (hasFile) {
      refs.dropzoneFile.textContent = `Fichier : ${file.name}`;
      refs.dropzoneFile.classList.remove("hidden");
    } else {
      refs.dropzoneFile.classList.add("hidden");
    }
  }
}

function statusToVisual(status) {
  const norm = String(status || "pending").toLowerCase();
  if (norm === "done") return ["dot-green", "badge-done", "Termine"];
  if (norm === "running") return ["dot-blue", "badge-running", "En cours"];
  if (norm === "action") return ["dot-amber", "badge-amber", "Action requise"];
  if (norm === "paused") return ["dot-amber", "badge-amber", "En pause"];
  if (norm === "skipped") return ["dot-gray", "badge-pending", "Ignoré"];
  if (norm === "error") return ["dot-red", "badge-error", "Erreur"];
  return ["dot-gray", "badge-pending", "En attente"];
}

function updateRunControlButtons() {
  const hasWritableRole = !isReadOnlyRole(session.role);
  const stopTarget = state.currentJobId || state.resumeJobId || state.activeJobId;
  if (refs.stopBtn) {
    refs.stopBtn.disabled = !hasWritableRole || !stopTarget;
  }
  if (refs.resumeBtn) {
    refs.resumeBtn.disabled = !hasWritableRole || !state.pausedJobId || state.isExecuting || Boolean(stopTarget);
  }
}

function renderSteps() {
  const map = [
    ["A1", STEP_LABELS.A1],
    ["REVIEW", STEP_LABELS.REVIEW],
    ["A2", STEP_LABELS.A2],
    ["A3", STEP_LABELS.A3],
    ["EMB", STEP_LABELS.EMB],
  ];
  refs.liveSteps.innerHTML = map.map(([key, label]) => {
    const [dot, badge, text] = statusToVisual(state.steps[key]);
    return `<div class="step-item"><span class="pipeline-left"><span class="dot ${dot}"></span>${label}</span><span class="badge ${badge}">${text}</span></div>`;
  }).join("");

  const plan = state.currentPlan || readPipelineSelection();
  const targets = PIPELINE_ORDER.filter((stage) => Boolean(plan[stage]));
  const done = targets.filter((k) => ["done", "skipped", "error"].includes(state.steps[k])).length;
  const progress = Math.round((done / Math.max(1, targets.length)) * 100);
  refs.progressBar.style.width = `${progress}%`;
  refs.progressText.textContent = `${progress}%`;
}

function setStep(step, status) {
  state.steps[step] = status;
  renderSteps();
}

function parseIsoDateMs(value) {
  const ts = Date.parse(String(value || ""));
  if (Number.isNaN(ts)) {
    return Date.now();
  }
  return ts;
}

function normalizeRunType(run) {
  const type = String(run?.type || "").toLowerCase();
  if (type === "applicability") return "A2";
  if (type === "compliance") return "A3";
  if (type === "embedding_index") return "EMB";
  return "A1";
}

function formatSecondsShort(value) {
  const num = Number(value || 0);
  if (!Number.isFinite(num) || num <= 0) {
    return "0s";
  }
  if (num >= 60) {
    return `${(num / 60).toFixed(1)} min`;
  }
  return `${num.toFixed(num >= 10 ? 1 : 2)}s`;
}

function renderA2RunMetrics(row) {
  const stats = getStageResult(row, "A2")?.engine_stats;
  if (!stats || typeof stats !== "object") {
    return "";
  }
  const retained = Number(stats.pairs_retained || 0);
  const selected = Number(stats.pairs_selected || retained || 0);
  const pruned = Number(stats.pairs_pruned || 0);
  const llmCalls = Number(stats.llm_calls || 0);
  const ppm = Number(stats.pairs_per_minute || 0);
  const prunePct = Number(stats.prune_rate_pct || 0);
  const avgPair = Number(stats.avg_seconds_per_pair || 0);
  const total = Number(stats.runtime_seconds_total || 0);
  if ([retained, selected, pruned, llmCalls, ppm, avgPair, total].every((v) => v <= 0)) {
    return "";
  }
  return `<div class="page-subtitle">A2: paires ${escapeHtml(numberFmt(selected || retained))} | prune ${escapeHtml(prunePct.toFixed(1))}% | LLM ${escapeHtml(numberFmt(llmCalls))} | débit ${escapeHtml(ppm.toFixed(1))}/min | moy ${escapeHtml(formatSecondsShort(avgPair))} | total ${escapeHtml(formatSecondsShort(total))}</div>`;
}

function renderRunOutcomeNotes(row) {
  const notes = [];
  const a2 = getStageResult(row, "A2");
  const a3 = getStageResult(row, "A3");
  const indexed = Number(row?.indexed || 0);
  const a2Total = Number(a2?.total || 0);
  const a3Total = Number(a3?.total || 0);
  const a2Errors = Number(a2?.counts?.ERROR || 0);
  const a3Errors = Number(a3?.counts?.ERROR || 0);
  const a2Status = stepStatusForRun(row, "A2");
  const a3Status = stepStatusForRun(row, "A3");
  const embStatus = stepStatusForRun(row, "EMB");

  if (row?.pipeline_plan?.A2) {
    if (a2Status === "SKIPPED") {
      notes.push("Applicabilité non lancée: le registre final A1 n'était pas encore prêt ou exploitable");
    } else if ((a2Status === "ERROR" || a2Status === "FAILED") || (a2Errors > 0 && a2Total <= 0)) {
      notes.push(`Applicabilité en échec: ${numberFmt(a2Errors)} erreur(s) moteur`);
    } else if (a2Status === "DONE" && a2Total <= 0) {
      notes.push("Applicabilité terminée: aucune décision produite");
    }
  }
  if (row?.pipeline_plan?.A3) {
    if (a3Status === "SKIPPED") {
      notes.push("Conformité non lancée: aucune exigence applicable après A2");
    } else if ((a3Status === "ERROR" || a3Status === "FAILED") || (a3Errors > 0 && a3Total <= 0)) {
      notes.push(`Conformité en échec: ${numberFmt(a3Errors)} évaluation(s) en erreur`);
    } else if (a3Status === "DONE" && a3Total <= 0) {
      notes.push("Conformité terminée: aucune évaluation produite");
    }
  }
  if (row?.pipeline_plan?.EMB && (embStatus === "DONE" || embStatus === "SKIPPED")) {
    notes.push(`Assistant expert: ${numberFmt(indexed)} contenu(s) indexé(s)`);
  }

  if (!notes.length) {
    return "";
  }
  return notes.map((note) => `<div class="page-subtitle">${escapeHtml(note)}</div>`).join("");
}

function renderRunSummaryPanel(row) {
  const jobId = String(row?.job_id || "").trim();
  if (!jobId || state.expandedRunId !== jobId) {
    return "";
  }
  const cached = state.runSummaryCache[jobId] || null;
  if (!cached) {
    return `
      <div class="run-history-detail is-loading">
        <div class="page-subtitle">Chargement du résumé...</div>
      </div>
    `;
  }
  if (cached && cached.__error) {
    return `
      <div class="run-history-detail is-warning">
        <div class="page-subtitle" style="color:#9a3412">Résumé indisponible: ${escapeHtml(String(cached.__error || "Erreur de chargement"))}</div>
      </div>
    `;
  }
  const summarySource = cached && typeof cached === "object" ? cached : row;
  return `
    <div class="run-history-detail">
      ${renderA2RunMetrics(summarySource)}
      ${renderRunOutcomeNotes(summarySource)}
      ${compactRunSummaryHtml(row, cached)}
    </div>
  `;
}

function resetStepsPending() {
  for (const key of PIPELINE_ORDER) {
    state.steps[key] = "pending";
  }
  renderSteps();
}

function setStageAsRunning(stage) {
  resetStepsPending();
  const idx = PIPELINE_ORDER.indexOf(stage);
  if (idx < 0) {
    return;
  }
  for (let i = 0; i < idx; i += 1) {
    state.steps[PIPELINE_ORDER[i]] = "done";
  }
  state.steps[stage] = "running";
  renderSteps();
}

function pickLatestActiveRun(rows) {
  const active = (rows || []).filter((row) => ACTIVE_RUN_STATUSES.has(String(row?.status || "").toUpperCase()));
  if (!active.length) {
    return null;
  }
  active.sort((a, b) => parseIsoDateMs(b?.created_at || b?.updated_at) - parseIsoDateMs(a?.created_at || a?.updated_at));
  return active[0];
}

function pickLatestPausedRun(rows) {
  const paused = (rows || []).filter((row) =>
    PAUSED_RUN_STATUSES.has(String(row?.status || "").toUpperCase()) &&
    !String(row?.resumed_by || "").trim()
  );
  if (!paused.length) {
    return null;
  }
  paused.sort((a, b) => parseIsoDateMs(b?.created_at || b?.updated_at) - parseIsoDateMs(a?.created_at || a?.updated_at));
  return paused[0];
}

function chooseRunFileName(activeRun, rows) {
  const ownName = String(activeRun?.file_name || "").trim();
  if (ownName) {
    return ownName;
  }
  const docTitle = String(activeRun?._docSummary?.title || "").trim();
  if (docTitle) {
    return docTitle;
  }
  const fallback = (rows || []).find((row) => String(row?.file_name || row?._docSummary?.title || "").trim() && normalizeRunType(row) === "A1");
  return String(fallback?.file_name || fallback?._docSummary?.title || "").trim();
}

async function preloadRunSnapshot(run, label) {
  try {
    const detail = await api.getRunDetails(run.job_id, getAuth().tenant_id);
    applyRunPipelineState(detail, { syncCheckboxes: true });
    const fileName = String(detail?.file_name || detail?.title || "").trim();
    const docId = String(detail?.doc_id || run?.doc_id || "").trim();
    if (fileName) {
      updateSelectedFileUI({ name: fileName });
      if (!refs.docName.value.trim()) {
        refs.docName.value = String(fileName).replace(/\.pdf$/i, "");
      }
      logLine(`${label}: document ${fileName}`);
    }
    rememberRunDocumentContext({
      docId,
      fileName,
      source: label,
    });
    const outTail = String(detail?.stdout_tail || "").trim();
    if (outTail) {
      const last = outTail.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).slice(-1)[0];
      if (last) {
        logLine(`${label}: ${last}`);
      }
    }
    state.runSummaryCache[String(run?.job_id || detail?.job_id || "").trim()] = detail;
  } catch {
  }
}

async function resumeActiveRunFromRecent(rows) {
  if (state.isExecuting) {
    return;
  }
  const activeRun = pickLatestActiveRun(rows);
  state.activeJobId = String(activeRun?.job_id || "");
  updateRunControlButtons();
  if (!activeRun) {
    if (!state.isExecuting && !state.resumeJobId) {
      refs.submit.disabled = false;
      refs.submit.textContent = "Préparer le registre A1";
    }
    return;
  }
  if (state.resumeJobId && state.resumeJobId === activeRun.job_id) {
    return;
  }

  const stage = String(activeRun?.current_stage || normalizeRunType(activeRun) || "A1").toUpperCase();
  const label = STEP_LABELS[stage] || stage;
  const fileName = chooseRunFileName(activeRun, rows);

  if (fileName) {
    updateSelectedFileUI({ name: fileName });
    if (!refs.docName.value.trim()) {
      refs.docName.value = String(fileName).replace(/\.pdf$/i, "");
    }
  }
  rememberRunDocumentContext({
    docId: String(activeRun?.doc_id || "").trim(),
    fileName,
    source: label,
  });
  applyRunPipelineState(activeRun, { syncCheckboxes: true });

  clearRunError();
  if (!activeRun?.pipeline_steps) {
    setStageAsRunning(stage);
  }
  setRunStatus(`${label} en cours (reprise automatique)`);
  refs.submit.disabled = true;
  refs.submit.textContent = "Run actif...";

  const currentLog = String(refs.liveLog.textContent || "").trim();
  if (!currentLog || currentLog.toLowerCase().includes("aucun run actif")) {
    refs.liveLog.textContent = "";
  }
  logLine(`Reprise ${label}: ${activeRun.job_id}`);
  await preloadRunSnapshot(activeRun, label);

  state.resumeJobId = String(activeRun.job_id || "");
  updateRunControlButtons();
  try {
    const resumedRun = await pollJob(activeRun.job_id, label, {
      maxWaitMs: RESUME_MAX_WAIT_MS,
      startedAtMs: parseIsoDateMs(activeRun.created_at || activeRun.updated_at),
    });
    if (resumedRun?.paused) {
      setStep(stage, "paused");
      logLine(`${label}: en pause.`);
      setRunStatus(`${label} en pause`);
    } else {
      setStep(stage, "done");
      logLine(`${label}: terminé.`);
      setRunStatus(`${label} terminé`);
    }
  } catch (error) {
    setStep(stage, "error");
    logLine(`Erreur reprise ${label}: ${error.message || "run arrêté"}`);
    showRunError(error.message || `Erreur ${label}`);
    setRunStatus(`${label} en échec`);
  } finally {
    state.resumeJobId = "";
    state.activeJobId = "";
    refs.submit.disabled = false;
    refs.submit.textContent = "Préparer le registre A1";
    updateRunControlButtons();
    await loadRecentRuns({ autoResume: false });
  }
}

async function pollJob(jobId, label, options = {}) {
  const maxWaitMs = toInt(options.maxWaitMs, 10 * 60 * 1000, 10000);
  const startedAtCandidate = Number(options.startedAtMs);
  const startedAt = Number.isFinite(startedAtCandidate) && startedAtCandidate > 0 ? startedAtCandidate : Date.now();
  let lastStatusLogged = "";
  let lastHeartbeatAt = 0;
  let lastStageMessage = "";

  while (Date.now() - startedAt < maxWaitMs) {
    const run = await api.getRun(jobId, getAuth().tenant_id);
    const status = String(run.status || "").toUpperCase();
    const liveStage = String(run.current_stage || "").trim().toUpperCase();
    const liveLabel = STEP_LABELS[liveStage] || label;
    applyRunPipelineState(run, { syncCheckboxes: true });
    const stageMessage = String(run.stage_message || "").trim();
    if (stageMessage && stageMessage !== lastStageMessage) {
      logLine(stageMessage);
      setRunStatus(stageMessage);
      lastStageMessage = stageMessage;
    }
    const now = Date.now();
    if (status !== lastStatusLogged) {
      logLine(`${liveLabel}: ${status}`);
      lastStatusLogged = status;
      lastHeartbeatAt = now;
    } else if ((status === "RUNNING" || status === "QUEUED" || status === "PENDING") && now - lastHeartbeatAt >= RUNNING_HEARTBEAT_MS) {
      const mins = Math.max(1, Math.floor((now - startedAt) / 60000));
      logLine(`${liveLabel}: toujours en cours (${mins} min)`);
      lastHeartbeatAt = now;
    }

    if (status === "DONE") {
      return run;
    }
    if (status === "PAUSED") {
      return { ...run, paused: true };
    }
    if (status === "FAILED" || status === "ERROR") {
      let detail = null;
      try {
        detail = await api.getRunDetails(jobId, getAuth().tenant_id);
        state.runSummaryCache[String(jobId)] = detail;
      } catch {
        detail = null;
      }

      const category = detail?.error_category || run?.error_category || "unknown_error";
      const step = detail?.failed_step || run?.failed_step || "UNKNOWN_STEP";
      const stdout = String(detail?.stdout_tail || "");
      const stderr = String(detail?.stderr_tail || "");
      const backendError = String(detail?.error || run?.error || "");
      const combined = `${stdout}\n${stderr}`.toLowerCase();
      if (combined.includes("connection timeout") || combined.includes("psycopg.errors.connectiontimeout")) {
        throw new Error(`${liveLabel} échoué: DB indisponible (timeout PostgreSQL). Vérifier PG_DSN / serveur Postgres.`);
      }
      throw new Error(`${liveLabel} échoué: ${category} (${step})${backendError ? ` - ${backendError}` : ""}.`);
    }
    await new Promise((resolve) => setTimeout(resolve, POLL_INTERVAL_MS));
  }

  const minutes = Math.max(1, Math.floor(maxWaitMs / 60000));
  throw new Error(`${label} dépasse ${minutes} min côté interface. Le run continue probablement côté serveur; relancer la page puis vérifier "Runs récents".`);
}

async function executePipeline(file) {
  resetLog();
  clearRunError();
  setRunStatus("Extraction A1 en cours");
  state.currentJobId = "";
  updateRunControlButtons();
  syncPipelineDependencies();
  const plan = readPipelineSelection();
  state.steps = {
    A1: "running",
    REVIEW: "pending",
    A2: plan.A2 ? "pending" : "pending",
    A3: plan.A3 ? "pending" : "pending",
    EMB: plan.EMB ? "pending" : "pending",
  };
  renderSteps();

  const form = new FormData();
  form.append("pdf", file);
  form.append("tenant", getAuth().tenant_id);
  form.append("title", refs.docName.value.trim() || file.name.replace(/\.pdf$/i, ""));
  form.append("source", refs.docReference.value.trim() || "upload_ui");
  form.append("jurisdiction", "TN");
  form.append("document_family", refs.docType.value.trim() || "REGLEMENTAIRE");
  form.append("on_duplicate", refs.duplicateMode?.value || "reinject");
  const extractLimit = toInt(refs.extractLimit?.value, 0, 0);
  const timeoutSegmentSec = toInt(refs.timeoutSegmentSec?.value, DEFAULT_TIMEOUTS.segment, 300);
  const timeoutExtractSec = toInt(refs.timeoutExtractSec?.value, DEFAULT_TIMEOUTS.extract, 600);
  form.append("extract_limit", String(extractLimit));
  form.append("timeout_ingest_sec", String(DEFAULT_TIMEOUTS.ingest));
  form.append("timeout_segment_sec", String(timeoutSegmentSec));
  form.append("timeout_extract_sec", String(timeoutExtractSec));
  form.append("timeout_backfill_sec", String(DEFAULT_TIMEOUTS.backfill));
  form.append("timeout_export_sec", String(DEFAULT_TIMEOUTS.export));
  form.append("run_applicability", String(plan.A2));
  form.append("run_compliance", String(plan.A3));
  form.append("run_embedding", String(plan.EMB));

  state.currentPlan = clonePlan(plan);
  const run = await api.createRun(form);
  state.currentJobId = String(run.job_id || "");
  updateRunControlButtons();
  const a1BudgetSec = DEFAULT_TIMEOUTS.ingest
    + timeoutSegmentSec
    + timeoutExtractSec
    + DEFAULT_TIMEOUTS.backfill
    + DEFAULT_TIMEOUTS.export
    + 300;
  const totalBudgetMs = (a1BudgetSec * 1000)
    + (plan.A2 ? WAIT_BUDGETS_MS.A2 : 0)
    + (plan.A3 ? WAIT_BUDGETS_MS.A3 : 0)
    + (plan.EMB ? WAIT_BUDGETS_MS.Embeddings : 0);
  const totalBudgetMin = Math.ceil(totalBudgetMs / 60000);
  logLine(`Traitement lancé: ${run.job_id} (${pipelinePlanLabel(plan)})`);
  setRunStatus(`Extraction A1 en cours (max ${totalBudgetMin} min)`);
  const pipelineRun = await pollJob(run.job_id, "Traitement", { maxWaitMs: totalBudgetMs });
  if (pipelineRun?.paused) {
    applyRunPipelineState(pipelineRun, { syncCheckboxes: true });
    setRunStatus("Traitement en pause");
    logLine("Traitement mis en pause.");
    state.currentJobId = "";
    updateRunControlButtons();
    return;
  }
  applyRunPipelineState(pipelineRun, { syncCheckboxes: true });
  const a1DocId = pipelineRun?.doc_id || run?.doc_id || "";
  if (a1DocId) {
    logLine(`Document d'extraction: ${a1DocId}`);
    rememberRunDocumentContext({
      docId: String(a1DocId),
      fileName: String(file.name || ""),
      source: "A1",
    });
  } else {
    logLine("Document cible indisponible (l'applicabilite utilisera le scope tenant).");
  }
  if (!plan.A2 && !plan.A3) {
    state.steps.REVIEW = "action";
    renderSteps();
    logLine("Extraction A1 terminée. Ouvrir Exigences pour valider le registre final avant A2/A3.");
    setRunStatus("Extraction terminée - validation A1 requise");
  } else {
    logLine("Traitement terminé.");
    setRunStatus("Traitement terminé");
  }
  state.currentJobId = "";
  updateRunControlButtons();
}

async function executeExistingDocPipeline({ docId = "", fileName = "", runA2 = true, runA3 = true } = {}) {
  const safeDocId = String(docId || "").trim();
  if (!safeDocId) {
    throw new Error("doc_id manquant pour relancer A2/A3.");
  }
  const plan = {
    A1: true,
    A2: Boolean(runA2),
    A3: Boolean(runA3),
    EMB: false,
  };
  resetLog();
  clearRunError();
  state.currentJobId = "";
  state.currentPlan = clonePlan(plan);
  state.steps = {
    A1: "done",
    REVIEW: "done",
    A2: plan.A2 ? "pending" : "done",
    A3: plan.A3 ? "pending" : "pending",
    EMB: "pending",
  };
  renderSteps();
  setRunStatus(plan.A2 ? "Relance des analyses aval" : "Relance conformité sur données existantes");
  rememberRunDocumentContext({
    docId: safeDocId,
    fileName: String(fileName || "").trim(),
    source: "A1_EXISTING",
  });
  logLine(`Document existant ciblé: ${fileName ? `${fileName} (${safeDocId})` : safeDocId}`);

  if (plan.A2) {
    setStep("A2", "running");
    setRunStatus("Applicabilité en cours");
    const a2 = await api.runApplicability({
      tenant_id: getAuth().tenant_id,
      doc_id: safeDocId,
      mode: "full",
      force: false,
    });
    state.currentJobId = String(a2.job_id || "");
    updateRunControlButtons();
    logLine(`Applicabilité lancée: ${a2.job_id}`);
    const a2Run = await pollJob(a2.job_id, "A2", { maxWaitMs: WAIT_BUDGETS_MS.A2 });
    if (a2Run?.paused) {
      setStep("A2", "paused");
      setRunStatus("Applicabilité en pause");
      state.currentJobId = "";
      updateRunControlButtons();
      return;
    }
    setStep("A2", "done");
    setRunStatus("Applicabilité terminée");
    state.currentJobId = "";
    updateRunControlButtons();
  }

  if (plan.A3) {
    setStep("A3", "running");
    setRunStatus("Conformité en cours");
    const a3 = await api.runCompliance({
      tenant_id: getAuth().tenant_id,
      doc_id: safeDocId,
      mode: "full",
      force: false,
    });
    state.currentJobId = String(a3.job_id || "");
    updateRunControlButtons();
    logLine(`Conformité lancée: ${a3.job_id}`);
    const a3Run = await pollJob(a3.job_id, "A3", { maxWaitMs: WAIT_BUDGETS_MS.A3 });
    if (a3Run?.paused) {
      setStep("A3", "paused");
      setRunStatus("Conformité en pause");
      state.currentJobId = "";
      updateRunControlButtons();
      return;
    }
    setStep("A3", "done");
    setRunStatus("Conformité terminée");
    state.currentJobId = "";
    updateRunControlButtons();
  }

  logLine("Relance des analyses terminée.");
  setRunStatus("Analyses aval terminées");
}

async function executeAssistantIndex({ force = false } = {}) {
  resetLog();
  clearRunError();
  state.currentPlan = { A1: true, A2: false, A3: false, EMB: true };
  state.steps = {
    A1: "done",
    REVIEW: "done",
    A2: "done",
    A3: "done",
    EMB: "running",
  };
  renderSteps();
  setRunStatus("Indexation assistant en cours");
  const run = await api.reindexEmbeddings(getAuth().tenant_id, force);
  state.currentJobId = String(run.job_id || "");
  updateRunControlButtons();
  logLine(`Indexation assistant lancée: ${run.job_id}`);
  const embRun = await pollJob(run.job_id, "Assistant", { maxWaitMs: WAIT_BUDGETS_MS.Embeddings });
  if (embRun?.paused) {
    setStep("EMB", "paused");
    setRunStatus("Indexation assistant en pause");
    state.currentJobId = "";
    updateRunControlButtons();
    return;
  }
  setStep("EMB", "done");
  setRunStatus("Assistant prêt");
  state.currentJobId = "";
  updateRunControlButtons();
  logLine("Indexation assistant terminée.");
}

function setupDropzone() {
  ["dragenter", "dragover"].forEach((evtName) => {
    refs.dropzone.addEventListener(evtName, (event) => {
      event.preventDefault();
      refs.dropzone.classList.add("drag-over");
    });
  });

  ["dragleave", "drop"].forEach((evtName) => {
    refs.dropzone.addEventListener(evtName, (event) => {
      event.preventDefault();
      refs.dropzone.classList.remove("drag-over");
    });
  });

  refs.dropzone.addEventListener("drop", (event) => {
    const file = event.dataTransfer?.files?.[0];
    if (file) {
      const err = validateFile(file);
      if (err) { showRunError(err); return; }
      const dt = new DataTransfer();
      dt.items.add(file);
      refs.file.files = dt.files;
      updateSelectedFileUI(file);
      if (!refs.docName.value.trim()) {
        refs.docName.value = file.name.replace(/\.pdf$/i, "");
      }
    }
  });

  refs.file.addEventListener("change", () => {
    const file = refs.file.files?.[0];
    if (file) {
      const err = validateFile(file);
      if (err) { showRunError(err); refs.file.value = ""; updateSelectedFileUI(null); return; }
      clearRunError();
    }
    updateSelectedFileUI(file || null);
    if (file && !refs.docName.value.trim()) {
      refs.docName.value = file.name.replace(/\.pdf$/i, "");
    }
  });
}

async function loadRecentRuns(options = {}) {
  const autoResume = options.autoResume !== false;
  if (refs.clearRunsHistory) {
    refs.clearRunsHistory.disabled = true;
  }
  refs.recentRuns.innerHTML = `<div class="skeleton skeleton-line"></div><div class="skeleton skeleton-line"></div>`;
  try {
    const data = await api.listRuns(getAuth().tenant_id, 8);
    const rows = await annotateRunDocAvailability(data.items || []);
    state.recentRuns = rows;
    state.latestAvailableDoc = findLatestAvailableDoc(rows);
    const activeRun = pickLatestActiveRun(rows);
    const pausedRun = pickLatestPausedRun(rows);
    const contextualRun = activeRun || pausedRun;
    state.activeJobId = String(activeRun?.job_id || "");
    state.pausedJobId = String(pausedRun?.job_id || "");
    if (contextualRun) {
      rememberRunDocumentContext({
        docId: String(contextualRun?.doc_id || "").trim(),
        fileName: String(contextualRun?.file_name || "").trim(),
        source: normalizeRunType(contextualRun),
      });
    }
    updateRunControlButtons();
    if (!rows.length) {
      state.activeJobId = "";
      state.pausedJobId = "";
      state.currentPlan = clonePlan();
      resetStepsPending();
      setRunStatus("Prêt à lancer");
      resetLog();
      updateRunControlButtons();
      if (refs.clearRunsHistory) {
        refs.clearRunsHistory.disabled = true;
      }
      await loadUploadGuidance();
      renderEmpty(refs.recentRuns, "Aucun run trouvé.", "Actualiser", loadRecentRuns);
      return rows;
    }

    if (refs.clearRunsHistory) {
      const hasTerminalRuns = rows.some((row) => {
        const status = String(row?.status || "").trim().toUpperCase();
        return !ACTIVE_RUN_STATUSES.has(status) && !PAUSED_RUN_STATUSES.has(status);
      });
      refs.clearRunsHistory.disabled = !hasTerminalRuns;
    }

    refs.recentRuns.innerHTML = rows.map((row) => `
      <div class="pipeline-item run-history-item">
        <div class="run-history-main">
          <div class="run-history-title">${escapeHtml(getRunDisplayTitle(row))}</div>
          <div class="run-history-meta">${escapeHtml(getRunDisplayMeta(row))}</div>
          <div class="run-history-summary">${escapeHtml(runHistorySummary(row))}</div>
          ${renderRunSummaryPanel(row)}
        </div>
        <div class="run-history-actions">
          <div class="run-history-status">${statusBadge(row.status)}</div>
          ${canRunFollowupsForRow(row) ? `
            <div class="run-history-actions-row">
              <button
                type="button"
                class="btn btn-secondary btn-sm"
                data-run-existing="a2"
                data-doc-id="${escapeHtml(String(row.doc_id || ""))}"
                data-file-name="${escapeHtml(String(getRunDisplayTitle(row) || ""))}"
              >Applicabilité</button>
              <button
                type="button"
                class="btn btn-secondary btn-sm"
                data-run-existing="a3"
                data-doc-id="${escapeHtml(String(row.doc_id || ""))}"
                data-file-name="${escapeHtml(String(getRunDisplayTitle(row) || ""))}"
              >Conformité</button>
              <button
                type="button"
                class="btn btn-primary btn-sm"
                data-run-existing="a2a3"
                data-doc-id="${escapeHtml(String(row.doc_id || ""))}"
                data-file-name="${escapeHtml(String(getRunDisplayTitle(row) || ""))}"
              >Suite complète</button>
            </div>
          ` : ""}
          <button
            type="button"
            class="btn btn-secondary btn-sm run-history-more"
            data-run-summary-toggle="${escapeHtml(String(row.job_id || ""))}"
          >${state.expandedRunId === String(row.job_id || "") ? "Masquer" : "Détails"}</button>
        </div>
      </div>
    `).join("");
    const latestVisualRun = contextualRun || rows[0];
    if (latestVisualRun) {
      applyRunPipelineState(latestVisualRun, { syncCheckboxes: false });
      setRunStatus(runStatusSummary(latestVisualRun));
      refs.liveLog.textContent = activeRun ? "Run actif détecté." : `Dernier run: ${runStatusSummary(latestVisualRun)}`;
    }
    await loadUploadGuidance();
    if (autoResume) {
      await resumeActiveRunFromRecent(rows);
    }
    renderWorkflowGuide();
    return rows;
  } catch (error) {
    state.activeJobId = "";
    state.pausedJobId = "";
    state.recentRuns = [];
    state.latestAvailableDoc = null;
    updateRunControlButtons();
    if (refs.clearRunsHistory && !isReadOnlyRole(session.role)) {
      refs.clearRunsHistory.disabled = false;
    }
    await loadUploadGuidance();
    renderError(refs.recentRuns, error, "Réessayer", loadRecentRuns);
    return [];
  }
}

refs.form?.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = refs.file.files?.[0];
  if (!file) {
    showRunError("Sélectionner un fichier PDF avant de lancer.");
    return;
  }
  const fileErr = validateFile(file);
  if (fileErr) {
    showRunError(fileErr);
    return;
  }

  refs.submit.disabled = true;
  const text = refs.submit.textContent;
  refs.submit.textContent = "Lancement...";
  clearRunError();
  setRunStatus("Vérification du document...");
  state.isExecuting = true;
  updateRunControlButtons();

  try {
    await executePipeline(file);
    await loadRecentRuns({ autoResume: false });
  } catch (error) {
    const message = error.message || "Erreur de traitement inconnue";
    logLine(`Erreur: ${message}`);
    showRunError(message);
    if (message.includes("Finalisez la validation A1")) {
      state.steps.REVIEW = "action";
      renderSteps();
      setRunStatus("Validation A1 requise avant A2");
    } else {
      setRunStatus("Échec du traitement");
    }
    const lastRunning = Object.entries(state.steps).find(([, val]) => val === "running")?.[0];
    if (lastRunning) {
      setStep(lastRunning, "error");
    }
  } finally {
    state.isExecuting = false;
    refs.submit.disabled = false;
    refs.submit.textContent = text;
    updateRunControlButtons();
  }
});

refs.clearRunsHistory?.addEventListener("click", async () => {
  const ok = window.confirm("Supprimer l'historique des runs récents de ce tenant ? Les runs actifs ou en pause seront conservés.");
  if (!ok) {
    return;
  }
  refs.clearRunsHistory.disabled = true;
  const previousLabel = refs.clearRunsHistory.textContent;
  refs.clearRunsHistory.textContent = "…";
  clearRunError();
  try {
    const result = await api.clearRunsHistory(getAuth().tenant_id);
    const deleted = Number(result?.deleted_runs || 0);
    const keptActive = Number(result?.kept_active_runs || 0);
    const keptPaused = Number(result?.kept_paused_runs || 0);
    logLine(`Historique nettoyé: ${deleted} run(s) supprimé(s).`);
    if (deleted === 0) {
      setRunStatus("Aucun run historique à supprimer");
    } else {
      setRunStatus(`Historique nettoyé (${deleted} run(s))`);
    }
    if (keptActive > 0 || keptPaused > 0) {
      const parts = [];
      if (keptActive > 0) parts.push(`${keptActive} actif(s) conservé(s)`);
      if (keptPaused > 0) parts.push(`${keptPaused} en pause conservé(s)`);
      logLine(`Runs non supprimés: ${parts.join(", ")}.`);
    }
    await loadRecentRuns({ autoResume: false });
  } catch (error) {
    showRunError(error.message || "Impossible de supprimer l'historique des runs.");
  } finally {
    refs.clearRunsHistory.textContent = previousLabel;
    if (!refs.clearRunsHistory.disabled) {
      refs.clearRunsHistory.blur();
    }
  }
});

refs.stopBtn?.addEventListener("click", async () => {
  const targetJobId = state.currentJobId || state.resumeJobId || state.activeJobId;
  if (!targetJobId) {
    alert("Aucun run actif à arrêter.");
    return;
  }
  refs.stopBtn.disabled = true;
  const label = refs.stopBtn.textContent;
  refs.stopBtn.textContent = "Arrêt...";
  try {
    const result = await api.stopRun(targetJobId, getAuth().tenant_id);
    logLine(`Arrêt demandé: ${targetJobId} (${result.status || "RUNNING"})`);
    setRunStatus("Arrêt demandé...");
    await loadRecentRuns({ autoResume: false });
  } catch (error) {
    alert(error.message || "Impossible d'arrêter ce run");
  } finally {
    refs.stopBtn.textContent = label;
    updateRunControlButtons();
  }
});

refs.resumeBtn?.addEventListener("click", async () => {
  const targetJobId = state.pausedJobId;
  if (!targetJobId) {
    alert("Aucun run en pause à reprendre.");
    return;
  }
  refs.resumeBtn.disabled = true;
  const label = refs.resumeBtn.textContent;
  refs.resumeBtn.textContent = "Reprise...";
  try {
    const resumed = await api.resumeRun(targetJobId, getAuth().tenant_id);
    logLine(`Reprise demandée: ${targetJobId} -> ${resumed.job_id}`);
    setRunStatus("Reprise en cours...");
    await loadRecentRuns({ autoResume: true });
  } catch (error) {
    alert(error.message || "Impossible de reprendre ce run");
  } finally {
    refs.resumeBtn.textContent = label;
    updateRunControlButtons();
  }
});

refs.recentRuns?.addEventListener("click", async (event) => {
  const target = event.target instanceof HTMLElement ? event.target.closest("button") : null;
  if (!target) {
    return;
  }

  const summaryToggle = target.getAttribute("data-run-summary-toggle");
  if (summaryToggle) {
    const jobId = String(summaryToggle || "").trim();
    if (!jobId) {
      return;
    }
    if (state.expandedRunId === jobId) {
      state.expandedRunId = "";
      await loadRecentRuns({ autoResume: false });
      return;
    }
    state.expandedRunId = jobId;
    if (!state.runSummaryCache[jobId]) {
      try {
        state.runSummaryCache[jobId] = await api.getRunDetails(jobId, getAuth().tenant_id);
      } catch (error) {
        state.runSummaryCache[jobId] = { __error: error.message || "Impossible de charger le résumé du run." };
        showRunError(error.message || "Impossible de charger le résumé du run.");
      }
    }
    await loadRecentRuns({ autoResume: false });
    return;
  }

  const btn = target.closest("[data-run-existing]");
  if (!btn) {
    return;
  }
  const action = String(btn.getAttribute("data-run-existing") || "").trim().toLowerCase();
  const docId = String(btn.getAttribute("data-doc-id") || "").trim();
  const fileName = String(btn.getAttribute("data-file-name") || "").trim();
  if (!docId || !action) {
    return;
  }

  const previousText = btn.textContent;
  btn.setAttribute("disabled", "disabled");
  btn.textContent = "Lancement...";
  refs.submit.disabled = true;
  state.isExecuting = true;
  updateRunControlButtons();
  try {
    await executeExistingDocPipeline({
      docId,
      fileName,
      runA2: action === "a2" || action === "a2a3",
      runA3: action === "a3" || action === "a2a3",
    });
    await loadRecentRuns({ autoResume: false });
  } catch (error) {
    const message = error.message || "Erreur relance A2/A3";
    logLine(`Erreur: ${message}`);
    showRunError(message);
    if (message.includes("Finalisez la validation A1")) {
      state.steps.REVIEW = "action";
      renderSteps();
      setRunStatus("Validation A1 requise avant A2");
    } else {
      setRunStatus("Échec du traitement aval");
    }
  } finally {
    state.isExecuting = false;
    refs.submit.disabled = false;
    btn.removeAttribute("disabled");
    btn.textContent = previousText || "Relancer";
    updateRunControlButtons();
  }
});

renderSteps();
setupDropzone();
updateSelectedFileUI(null);
clearRunError();
setRunStatus("Prêt à lancer");
updateRunControlButtons();
renderWorkflowGuide();
refs.stepA2?.addEventListener("change", () => { syncPipelineDependencies("A2"); renderSteps(); });
refs.stepA3?.addEventListener("change", () => { syncPipelineDependencies("A3"); renderSteps(); });
refs.stepEmbed?.addEventListener("change", () => { renderSteps(); });
await loadUploadGuidance();
await loadRecentRuns({ autoResume: true });

refs.workflowOpenReview?.addEventListener("click", () => {
  window.location.href = "/ui/requirements.html";
});

refs.workflowOpenAssistant?.addEventListener("click", () => {
  window.location.href = "/ui/assistant.html";
});

refs.workflowRunA2?.addEventListener("click", async () => {
  const doc = state.latestAvailableDoc;
  if (!doc?.doc_id) {
    showRunError("Aucun document A1 exploitable n'a été trouvé pour lancer l'applicabilité.");
    return;
  }
  refs.workflowRunA2.disabled = true;
  state.isExecuting = true;
  updateRunControlButtons();
  try {
    await executeExistingDocPipeline({
      docId: String(doc.doc_id),
      fileName: String(doc.file_name || ""),
      runA2: true,
      runA3: false,
    });
    await loadRecentRuns({ autoResume: false });
  } catch (error) {
    showRunError(error.message || "Impossible de lancer l'applicabilité.");
  } finally {
    state.isExecuting = false;
    refs.workflowRunA2.disabled = false;
    updateRunControlButtons();
  }
});

refs.workflowRunA3?.addEventListener("click", async () => {
  const doc = state.latestAvailableDoc;
  if (!doc?.doc_id) {
    showRunError("Aucun document exploitable n'a été trouvé pour lancer la conformité.");
    return;
  }
  refs.workflowRunA3.disabled = true;
  state.isExecuting = true;
  updateRunControlButtons();
  try {
    await executeExistingDocPipeline({
      docId: String(doc.doc_id),
      fileName: String(doc.file_name || ""),
      runA2: false,
      runA3: true,
    });
    await loadRecentRuns({ autoResume: false });
  } catch (error) {
    showRunError(error.message || "Impossible de lancer la conformité.");
  } finally {
    state.isExecuting = false;
    refs.workflowRunA3.disabled = false;
    updateRunControlButtons();
  }
});

refs.workflowRunA2A3?.addEventListener("click", async () => {
  const doc = state.latestAvailableDoc;
  if (!doc?.doc_id) {
    showRunError("Aucun document A1 exploitable n'a été trouvé pour lancer A2 et A3.");
    return;
  }
  refs.workflowRunA2A3.disabled = true;
  state.isExecuting = true;
  updateRunControlButtons();
  try {
    await executeExistingDocPipeline({
      docId: String(doc.doc_id),
      fileName: String(doc.file_name || ""),
      runA2: true,
      runA3: true,
    });
    await loadRecentRuns({ autoResume: false });
  } catch (error) {
    showRunError(error.message || "Impossible de lancer A2 et A3.");
  } finally {
    state.isExecuting = false;
    refs.workflowRunA2A3.disabled = false;
    updateRunControlButtons();
  }
});

refs.workflowRunA4?.addEventListener("click", async () => {
  refs.workflowRunA4.disabled = true;
  state.isExecuting = true;
  updateRunControlButtons();
  try {
    await executeAssistantIndex({ force: false });
    await loadRecentRuns({ autoResume: false });
  } catch (error) {
    showRunError(error.message || "Impossible de préparer l'assistant.");
  } finally {
    state.isExecuting = false;
    refs.workflowRunA4.disabled = false;
    updateRunControlButtons();
  }
});

if (isReadOnlyRole(session.role)) {
  refs.form?.classList.add("hidden");
}
