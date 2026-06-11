import { clearAuth, getAuth } from "/ui/js/auth.js";

const API_V1 = "/api/v1";
const API_V2 = "/api/v2";

function buildQuery(params = {}) {
  const query = new URLSearchParams();
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") {
      return;
    }
    query.set(key, String(value));
  });
  const str = query.toString();
  return str ? `?${str}` : "";
}

function forceLogout() {
  clearAuth();
  if (!window.location.pathname.endsWith("/login.html")) {
    window.location.href = "/ui/login.html";
  }
}

export function getHeaders({ json = true, extra = {} } = {}) {
  const session = getAuth();
  const headers = {
    Accept: "application/json",
    ...extra,
  };
  if (json) {
    headers["Content-Type"] = "application/json";
  }
  if (session.token && !(Object.prototype.hasOwnProperty.call(headers, "Authorization"))) {
    headers.Authorization = `Bearer ${session.token}`;
  }
  return headers;
}

async function request(method, path, { params, data, formData, responseType = "json", headers = {} } = {}) {
  const url = `${path}${buildQuery(params)}`;
  const init = {
    method,
    headers: formData ? getHeaders({ json: false, extra: headers }) : getHeaders({ json: true, extra: headers }),
  };

  if (formData) {
    init.body = formData;
  } else if (data !== undefined) {
    init.body = JSON.stringify(data);
  }

  const res = await fetch(url, init);
  if (res.status === 401) {
    forceLogout();
    throw new Error("Session expirée");
  }

  const parseJsonSafe = (text) => {
    if (!text) {
      return null;
    }
    try {
      return JSON.parse(text);
    } catch {
      return null;
    }
  };

  const formatApiErrorDetail = (detail, fallback = "") => {
    if (!detail) {
      return fallback;
    }
    if (typeof detail === "string") {
      return detail;
    }
    if (typeof detail === "object") {
      const message = detail.message || detail.error_description || detail.error;
      const hint = detail.hint ? ` ${detail.hint}` : "";
      if (message) {
        return `${message}${hint}`.trim();
      }
      try {
        return JSON.stringify(detail);
      } catch {
        return fallback || "Erreur API";
      }
    }
    return String(detail);
  };

  if (responseType === "blob") {
    if (!res.ok) {
      const rawError = await res.text();
      const payload = parseJsonSafe(rawError);
      const detail = formatApiErrorDetail(payload?.detail || payload?.message, rawError || `Erreur ${res.status}`);
      throw new Error(detail || `Erreur ${res.status}`);
    }
    return res.blob();
  }

  const rawText = await res.text();

  if (!res.ok) {
    const payload = parseJsonSafe(rawText);
    const detail = formatApiErrorDetail(payload?.detail || payload?.message, rawText || `Erreur ${res.status}`);
    throw new Error(detail || `Erreur ${res.status}`);
  }
  if (responseType === "text") {
    return rawText;
  }
  if (res.status === 204) {
    return null;
  }
  const payload = parseJsonSafe(rawText);
  if (payload === null && rawText) {
    throw new Error("Réponse JSON invalide du serveur");
  }
  return payload;
}

export function apiGet(path, params = {}, options = {}) {
  return request("GET", path, { ...options, params });
}

export function apiPost(path, data = {}, options = {}) {
  return request("POST", path, { ...options, data });
}

export function apiDelete(path, params = {}, options = {}) {
  return request("DELETE", path, { ...options, params });
}

export function apiUpload(path, formData, options = {}) {
  return request("POST", path, { ...options, formData });
}

function tenantParam(tenant_id) {
  const session = getAuth();
  return tenant_id || session.active_tenant_id || session.tenant_id || "";
}

export const api = {
  login: (payload) => apiPost(`${API_V1}/auth/login`, payload, { headers: { Authorization: "" } }),
  logout: () => apiPost(`${API_V1}/auth/logout`, {}),
  authMe: () => apiGet(`${API_V1}/auth/me`),
  switchTenant: (tenant_id) => apiPost(`${API_V1}/auth/switch-tenant`, { tenant_id }),

  dashboardOverview: (tenant_id = "") => apiGet(`${API_V1}/dashboard/overview`, { tenant_id: tenantParam(tenant_id) }),
  systemStatus: () => apiGet(`${API_V1}/system/status`),
  listRuns: (tenant_id = "", limit = 20) => apiGet(`${API_V1}/runs`, { tenant_id: tenantParam(tenant_id), limit }),
  getRun: (job_id, tenant_id = "") => apiGet(`${API_V1}/runs/${job_id}`, { tenant_id: tenantParam(tenant_id) }),
  getRunDetails: (job_id, tenant_id = "") => apiGet(`${API_V1}/runs/${job_id}/details`, { tenant_id: tenantParam(tenant_id) }),
  clearRunsHistory: (tenant_id = "") => apiDelete(`${API_V1}/runs/history`, { tenant_id: tenantParam(tenant_id) }),
  stopRun: (job_id, tenant_id = "") => apiPost(`${API_V1}/runs/${job_id}/stop`, {}, { params: { tenant_id: tenantParam(tenant_id) } }),
  resumeRun: (job_id, tenant_id = "") => apiPost(`${API_V1}/runs/${job_id}/resume`, {}, { params: { tenant_id: tenantParam(tenant_id) } }),
  createRun: (formData) => apiUpload(`${API_V1}/runs`, formData),

  listRequirements: (filters = {}) => {
    const params = { ...filters };
    params.tenant_id = tenantParam(filters.tenant_id);
    return apiGet(`${API_V1}/requirements`, params);
  },
  validationQueue: (tenant_id = "", limit = 100, offset = 0) =>
    apiGet(`${API_V1}/validation/queue`, { tenant_id: tenantParam(tenant_id), limit, offset }),
  validationContext: (req_id, tenant_id = "") =>
    apiGet(`${API_V1}/requirements/${req_id}/validation-context`, { tenant_id: tenantParam(tenant_id) }),
  validateRequirement: (req_id, decisionOrPayload, comment = "", tenant_id = "") => {
    let payload;
    let tenant = tenantParam(tenant_id);
    if (decisionOrPayload && typeof decisionOrPayload === "object" && !Array.isArray(decisionOrPayload)) {
      payload = { ...decisionOrPayload };
      tenant = tenantParam(payload.tenant_id || tenant_id);
      delete payload.tenant_id;
    } else {
      payload = { decision: decisionOrPayload, comment };
    }
    return apiPost(`${API_V1}/requirements/${req_id}/validate`, payload, { params: { tenant_id: tenant } });
  },
  requirementValidations: (req_id, tenant_id = "") =>
    apiGet(`${API_V1}/requirements/${req_id}/validations`, { tenant_id: tenantParam(tenant_id) }),
  validations: (tenant_id = "", limit = 200) => apiGet(`${API_V1}/validations`, { tenant_id: tenantParam(tenant_id), limit }),

  analyticsOverview: (tenant_id = "") => apiGet(`${API_V1}/analytics/overview`, { tenant_id: tenantParam(tenant_id) }),
  analyticsFamilies: (tenant_id = "") => apiGet(`${API_V1}/analytics/families`, { tenant_id: tenantParam(tenant_id) }),
  analyticsGate: (tenant_id = "") => apiGet(`${API_V1}/analytics/gate`, { tenant_id: tenantParam(tenant_id) }),
  reportsList: (category = "", limit = 200) => apiGet(`${API_V1}/reports`, { category, limit }),
  documentSummary: (doc_id, tenant_id = "") => apiGet(`${API_V1}/documents/${doc_id}/summary`, { tenant_id: tenantParam(tenant_id) }),

  applicabilitySummary: (tenant_id = "") => apiGet(`${API_V2}/applicability/summary`, { tenant_id: tenantParam(tenant_id) }),
  reviewApplicabilityDecision: (requirement_id, payload = {}) =>
    apiPost(`${API_V2}/applicability/decisions/${requirement_id}/review`, {
      tenant_id: tenantParam(payload.tenant_id),
      status: payload.status,
      comment: payload.comment || "",
      scope_key: payload.scope_key || null,
    }),
  runApplicability: (payload = {}) => apiPost(`${API_V2}/applicability/run`, { tenant_id: tenantParam(payload.tenant_id), ...payload }),
  downloadApplicabilityPdf: (tenant_id = "") => request("GET", `${API_V2}/applicability/report.pdf`, { params: { tenant_id: tenantParam(tenant_id) }, responseType: "blob" }),

  complianceSummary: (tenant_id = "") => apiGet(`${API_V2}/compliance/summary`, { tenant_id: tenantParam(tenant_id) }),
  runCompliance: (payload = {}) => apiPost(`${API_V2}/compliance/run`, { tenant_id: tenantParam(payload.tenant_id), ...payload }),
  downloadCompliancePdf: (tenant_id = "") => request("GET", `${API_V2}/compliance/report.pdf`, { params: { tenant_id: tenantParam(tenant_id) }, responseType: "blob" }),
  downloadExecutivePdf: (tenant_id = "") => request("GET", `${API_V2}/reports/executive.pdf`, { params: { tenant_id: tenantParam(tenant_id) }, responseType: "blob" }),
  downloadActionPlanPdf: (tenant_id = "") => request("GET", `${API_V2}/reports/action-plan.pdf`, { params: { tenant_id: tenantParam(tenant_id) }, responseType: "blob" }),

  chat: (payload = {}) => apiPost(`${API_V2}/chat`, { tenant_id: tenantParam(payload.tenant_id), ...payload }),
  createChatAction: (payload = {}) => apiPost(`${API_V2}/chat/actions`, { tenant_id: tenantParam(payload.tenant_id), ...payload }),
  chatHistory: (session_id, tenant_id = "") => apiGet(`${API_V2}/chat/sessions/${session_id}/history`, { tenant_id: tenantParam(tenant_id) }),
  reindexEmbeddings: (tenant_id = "", force = false) => apiPost(`${API_V2}/chat/index`, {}, { params: { tenant_id: tenantParam(tenant_id), force } }),

  listTenants: () => apiGet(`${API_V2}/tenants`),
  onboardCompany: (payload = {}) => apiPost(`${API_V2}/admin/onboarding/company`, payload),
  companyProfile: (tenant_id = "") => apiGet(`${API_V2}/company/profile`, { tenant_id: tenantParam(tenant_id) }),
  upsertCompanyProfile: (payload = {}) => apiPost(`${API_V2}/company/profile`, { tenant_id: tenantParam(payload.tenant_id), ...payload }),
  companySites: (tenant_id = "") => apiGet(`${API_V2}/company/sites`, { tenant_id: tenantParam(tenant_id) }),
  upsertCompanySite: (payload = {}, tenant_id = "") => apiPost(`${API_V2}/company/sites`, payload, { params: { tenant_id: tenantParam(tenant_id || payload.tenant_id) } }),
  deleteCompanySite: (site_id, tenant_id = "") => apiDelete(`${API_V2}/company/sites/${site_id}`, { tenant_id: tenantParam(tenant_id) }),
  companyProcesses: (tenant_id = "") => apiGet(`${API_V2}/company/processes`, { tenant_id: tenantParam(tenant_id) }),
  upsertCompanyProcess: (payload = {}, tenant_id = "") => apiPost(`${API_V2}/company/processes`, payload, { params: { tenant_id: tenantParam(tenant_id || payload.tenant_id) } }),
  deleteCompanyProcess: (process_id, tenant_id = "") => apiDelete(`${API_V2}/company/processes/${process_id}`, { tenant_id: tenantParam(tenant_id) }),
  companyActivities: (tenant_id = "") => apiGet(`${API_V2}/company/activities`, { tenant_id: tenantParam(tenant_id) }),
  upsertCompanyActivity: (payload = {}, tenant_id = "") => apiPost(`${API_V2}/company/activities`, payload, { params: { tenant_id: tenantParam(tenant_id || payload.tenant_id) } }),
  deleteCompanyActivity: (activity_id, tenant_id = "") => apiDelete(`${API_V2}/company/activities/${activity_id}`, { tenant_id: tenantParam(tenant_id) }),
  companyProducts: (tenant_id = "") => apiGet(`${API_V2}/company/products`, { tenant_id: tenantParam(tenant_id) }),
  upsertCompanyProduct: (payload = {}, tenant_id = "") => apiPost(`${API_V2}/company/products`, payload, { params: { tenant_id: tenantParam(tenant_id || payload.tenant_id) } }),
  deleteCompanyProduct: (product_id, tenant_id = "") => apiDelete(`${API_V2}/company/products/${product_id}`, { tenant_id: tenantParam(tenant_id) }),
  companyChemicals: (tenant_id = "") => apiGet(`${API_V2}/company/chemicals`, { tenant_id: tenantParam(tenant_id) }),
  upsertCompanyChemicals: (payload = {}) => apiPost(`${API_V2}/company/chemicals`, { tenant_id: tenantParam(payload.tenant_id), ...payload }),
  companyImportTypes: () => apiGet(`${API_V2}/company/import/types`),
  importCompanyDataset: (formData) => apiUpload(`${API_V2}/company/import/bulk`, formData),
  companyProofs: (tenant_id = "", limit = 100) => apiGet(`${API_V2}/company/proofs`, { tenant_id: tenantParam(tenant_id), limit }),
  uploadCompanyProof: (formData) => apiUpload(`${API_V2}/company/proofs/upload`, formData),
};
