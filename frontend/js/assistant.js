import { getAuth, initShell, isReadOnlyRole, requireRole } from "/ui/js/auth.js";
import { api } from "/ui/js/api.js";
import { escapeHtml, formatDate, renderEmpty, renderError, truncate } from "/ui/js/utils.js";

const session = initShell("assistant");
if (!session) {
  throw new Error("Session absente");
}

const refs = {
  sessions: document.getElementById("session-list"),
  thread: document.getElementById("chat-thread"),
  form: document.getElementById("chat-form"),
  input: document.getElementById("chat-input"),
  send: document.getElementById("chat-send"),
  chips: document.getElementById("suggested-chips"),
  contextCards: document.getElementById("context-cards"),
  newSession: document.getElementById("btn-new-session"),
  profile: document.getElementById("chat-profile"),
  format: document.getElementById("chat-format"),
};

const state = {
  tenant: getAuth().tenant_id,
  activeSession: null,
  sessions: [],
};

const SUGGESTED_DEFAULT = [
  "Comment peux-tu m'aider ?",
  "Quelle différence entre applicabilité et conformité ?",
  "Qu'est-ce qu'une exigence réglementaire ?",
  "Quels écarts critiques ont l'impact légal le plus élevé ?",
];

async function loadDynamicSuggestions(tenant) {
  try {
    const data = await api.complianceSummary(tenant);
    const gaps = Array.isArray(data?.top_gaps) ? data.top_gaps : [];
    if (!gaps.length) return SUGGESTED_DEFAULT;
    const fromGaps = gaps.slice(0, 2).map((g) => {
      const domain = g.domain || g.requirement_domain || "";
      const severity = g.severity || "";
      return `Quelles preuves manquent pour l'exigence ${domain ? domain + " " : ""}de gravité ${severity || "CRITIQUE"} ?`;
    });
    return [...fromGaps, ...SUGGESTED_DEFAULT].slice(0, 4);
  } catch {
    return SUGGESTED_DEFAULT;
  }
}

function sessionsKey() {
  return `qalitas.chat.sessions.${state.tenant}`;
}

function loadStoredSessions() {
  try {
    const payload = JSON.parse(localStorage.getItem(sessionsKey()) || "[]");
    if (Array.isArray(payload)) {
      state.sessions = payload;
    }
  } catch {
    state.sessions = [];
  }
}

function saveSessions() {
  localStorage.setItem(sessionsKey(), JSON.stringify(state.sessions.slice(0, 30)));
}

function upsertSession(sessionId, title = "Session") {
  const idx = state.sessions.findIndex((it) => it.id === sessionId);
  const item = {
    id: sessionId,
    title: truncate(title, 50),
    updated_at: new Date().toISOString(),
  };
  if (idx >= 0) {
    state.sessions[idx] = { ...state.sessions[idx], ...item };
  } else {
    state.sessions.unshift(item);
  }
  state.sessions.sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")));
  saveSessions();
}

function renderSessions() {
  if (!state.sessions.length) {
    renderEmpty(refs.sessions, "Aucune session enregistrée.", "Nouvelle session", () => startNewSession());
    return;
  }
  refs.sessions.innerHTML = state.sessions.map((item) => `
    <button class="session-item ${state.activeSession === item.id ? "is-active" : ""}" data-session="${item.id}">
      <div style="font-weight:600">${escapeHtml(item.title || "Session")}</div>
      <div class="page-subtitle">${escapeHtml(formatDate(item.updated_at))}</div>
    </button>
  `).join("");

  refs.sessions.querySelectorAll("[data-session]").forEach((btn) => {
    btn.addEventListener("click", () => openSession(btn.dataset.session));
  });
}

function appendUserMessage(text) {
  refs.thread.insertAdjacentHTML("beforeend", `
    <div class="bubble bubble-user">${escapeHtml(text)}</div>
  `);
  refs.thread.scrollTop = refs.thread.scrollHeight;
}

function formatAssistantInline(text = "") {
  return escapeHtml(String(text || ""))
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
}

function stripAssistantMarkdown(text = "") {
  return String(text || "")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/^[-*]\s+/gm, "")
    .replace(/^\d+[\.)]\s+/gm, "")
    .trim();
}

function formatAssistantAnswer(text = "") {
  const lines = String(text || "-")
    .replace(/\r\n/g, "\n")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

  if (!lines.length) {
    return `<div class="assistant-answer"><p>-</p></div>`;
  }

  let html = "";
  let listType = "";
  let listItems = [];

  const flushList = () => {
    if (!listType || !listItems.length) return;
    html += `<${listType}>${listItems.map((item) => `<li>${item}</li>`).join("")}</${listType}>`;
    listType = "";
    listItems = [];
  };

  const parseTableRow = (line) => {
    const cleaned = String(line || "")
      .replace(/^[-*]\s+/, "")
      .replace(/^\d+[\.)]\s+/, "")
      .trim();
    if (!cleaned.includes("|")) return null;
    const cells = cleaned.split("|").map((cell) => cell.trim()).filter(Boolean);
    return cells.length >= 3 ? cells.slice(0, 3) : null;
  };

  for (let i = 0; i < lines.length; i += 1) {
    const tableRow = parseTableRow(lines[i]);
    if (tableRow) {
      flushList();
      const rows = [];
      while (i < lines.length) {
        const row = parseTableRow(lines[i]);
        if (!row) break;
        rows.push(row);
        i += 1;
      }
      i -= 1;
      if (rows.length >= 2) {
        const [header, ...body] = rows;
        html += `
          <div class="assistant-table-wrap">
            <table class="assistant-mini-table">
              <thead><tr>${header.map((cell) => `<th>${formatAssistantInline(cell)}</th>`).join("")}</tr></thead>
              <tbody>${body.map((row) => `<tr>${row.map((cell) => `<td>${formatAssistantInline(cell)}</td>`).join("")}</tr>`).join("")}</tbody>
            </table>
          </div>`;
      } else {
        html += `<p>${formatAssistantInline(rows[0].join(" | "))}</p>`;
      }
      continue;
    }

    const line = lines[i];
    const ordered = line.match(/^\d+[\.)]\s+(.+)$/);
    const unordered = line.match(/^[-*]\s+(.+)$/);

    if (ordered || unordered) {
      const nextType = ordered ? "ol" : "ul";
      if (listType && listType !== nextType) {
        flushList();
      }
      listType = nextType;
      listItems.push(formatAssistantInline((ordered || unordered)[1]));
      continue;
    }

    flushList();
    html += `<p>${formatAssistantInline(line)}</p>`;
  }

  flushList();
  return `<div class="assistant-answer">${html}</div>`;
}

function responseTypeBadge(type = "") {
  const normalized = String(type || "").toUpperCase();
  const map = {
    OBLIGATOIRE: ["badge-nc", "Obligatoire"],
    RECOMMANDE: ["badge-info", "Recommandé"],
    MIXTE: ["badge-amber", "Mixte"],
  };
  const [klass, label] = map[normalized] || ["badge-absence", "Interne"];
  return `<span class="badge ${klass}">${label}</span>`;
}

function showAssistantToast(message, ok = true) {
  const el = document.createElement("div");
  el.className = `assistant-toast ${ok ? "is-ok" : "is-error"}`;
  el.textContent = message;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2600);
}

async function createActionFromRecommendation(actionText, button) {
  const text = String(actionText || "").trim();
  if (!text || !button) return;
  button.disabled = true;
  const oldText = button.textContent;
  button.textContent = "Création...";
  try {
    await api.createChatAction({
      tenant_id: state.tenant,
      action_title: truncate(text, 160),
      action_description: `Action recommandée par l'Agent 4: ${text}`,
      expected_proof: "",
    });
    button.textContent = "Action créée";
    showAssistantToast("Action corrective créée.");
  } catch (error) {
    button.disabled = false;
    button.textContent = oldText;
    showAssistantToast(error.message || "Création impossible", false);
  }
}

function appendAssistantMessage(payload, pending = false) {
  const citations = Array.isArray(payload.source_citations) ? payload.source_citations : [];
  const visibleCitations = citations.filter((c) => {
    const kind = String(c?.source_kind || "").toUpperCase();
    return !["GLOSSARY", "SYSTEM", "COMPANY_CONTEXT"].includes(kind);
  });
  const actions = Array.isArray(payload.recommended_actions) ? payload.recommended_actions.slice(0, 4) : [];
  const citationsHtml = visibleCitations.length
    ? `<div class="citation-list">${visibleCitations.map((c, idx) => `
      <div class="citation-card">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
          <div style="font-weight:600">${escapeHtml(c.article_ref || c.doc_title || "Source")}</div>
          <span class="badge ${c.verified ? "badge-conforme" : "badge-amber"}">${c.verified ? "Citation vérifiée" : "À contrôler"}</span>
        </div>
        <div style="margin-top:4px">${escapeHtml(truncate(stripAssistantMarkdown(c.excerpt || ""), 160))}</div>
        ${c.warning ? `<div class="page-subtitle" style="margin-top:4px">${escapeHtml(c.warning)}</div>` : ""}
      </div>
    `).join("")}</div>`
    : "";

  const actionsHtml = actions.length
    ? `<div class="action-list">
        <div class="assistant-section-label">Actions recommandees</div>
        ${actions.map((a, idx) => `
          <div class="action-item">
            <span>${idx + 1}</span>
            <p>${escapeHtml(truncate(String(a), 120))}</p>
            ${isReadOnlyRole(session.role) ? "" : `<button type="button" class="action-create-btn" data-action-index="${idx}">Créer action</button>`}
          </div>
        `).join("")}
      </div>`
    : "";

  const pendingText = pending ? `<div class="page-subtitle">Traitement en cours...</div>` : "";
  const answerHtml = pendingText || formatAssistantAnswer(payload.answer || payload.content || "-");
  const metaHtml = pending || !payload.obligation_type
    ? ""
    : `<div class="assistant-response-meta">${responseTypeBadge(payload.obligation_type)}</div>`;

  refs.thread.insertAdjacentHTML("beforeend", `
    <div class="bubble bubble-ai" data-bubble="ai">
      <div class="assistant-bubble-head">
        <span class="badge badge-a4">IA</span>
        ${metaHtml}
      </div>
      <div class="assistant-answer-wrap">${answerHtml}</div>
      ${citationsHtml}
      ${actionsHtml}
    </div>
  `);
  const bubble = refs.thread.querySelector(".bubble-ai:last-child");
  bubble?.querySelectorAll("[data-action-index]").forEach((button) => {
    const idx = Number(button.getAttribute("data-action-index"));
    button.addEventListener("click", () => createActionFromRecommendation(actions[idx], button));
  });
  refs.thread.scrollTop = refs.thread.scrollHeight;
}

function replacePendingAssistant(payload) {
  const pending = refs.thread.querySelector(".bubble-ai:last-child");
  if (!pending) {
    appendAssistantMessage(payload);
    return;
  }
  pending.remove();
  appendAssistantMessage(payload);
}

function summarizeCounts(counts = {}) {
  const entries = Object.entries(counts || {}).filter(([, value]) => Number(value || 0) > 0);
  if (!entries.length) {
    return "Aucune donnée";
  }
  return entries
    .sort((a, b) => Number(b[1] || 0) - Number(a[1] || 0))
    .slice(0, 4)
    .map(([label, value]) => `${label}: ${value}`)
    .join(" | ");
}

function renderContextCards(citations = [], meta = {}) {
  const snapshot = meta.context_snapshot && typeof meta.context_snapshot === "object"
    ? meta.context_snapshot
    : {};

  const snapshotCards = Object.keys(snapshot).length
    ? [
        {
          article_ref: "Synthese applicabilite / conformite",
          excerpt: summarizeCounts(snapshot.compliance_counts || snapshot.applicability_counts || {}),
        },
        {
          article_ref: "Gaps / Actions",
          excerpt: `${summarizeCounts(snapshot.gap_counts || {})} | ${summarizeCounts(snapshot.action_counts || {})}`,
        },
        {
          article_ref: "Preuves",
          excerpt: `Preuves disponibles: ${snapshot.evidence_total ?? 0}`,
        },
      ]
    : [];

  const cards = [...snapshotCards, ...(citations.length
    ? citations
    : [{ article_ref: "Aucune source", excerpt: "Les citations s'affichent après la réponse IA." }])];

  refs.contextCards.innerHTML = cards.slice(0, 4).map((c, idx) => `
    <article class="citation-card" style="background:#fff">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
        <div style="font-weight:600">${escapeHtml(c.article_ref || c.doc_title || `Source ${idx + 1}`)}</div>
        ${typeof c.verified === "boolean" ? `<span class="badge ${c.verified ? "badge-conforme" : "badge-amber"}">${c.verified ? "Vérifiée" : "À contrôler"}</span>` : ""}
      </div>
      <div style="margin-top:4px">${escapeHtml(truncate(stripAssistantMarkdown(c.excerpt || ""), 120))}</div>
      ${c.warning ? `<div class="page-subtitle" style="margin-top:4px">${escapeHtml(c.warning)}</div>` : ""}
      ${meta.search_mode ? `<div class="page-subtitle" style="margin-top:4px">mode: ${escapeHtml(meta.search_mode)}</div>` : ""}
    </article>
  `).join("");
}

function renderSuggested(questions = SUGGESTED_DEFAULT) {
  refs.chips.innerHTML = questions.map((q) => `<button class="chip" data-suggest="${escapeHtml(q)}">${escapeHtml(truncate(q, 34))}</button>`).join("");
  refs.chips.querySelectorAll("[data-suggest]").forEach((chip) => {
    chip.addEventListener("click", () => {
      refs.input.value = chip.dataset.suggest || "";
      if (!isReadOnlyRole(session.role)) {
        refs.input.focus();
      }
    });
  });
}

async function openSession(sessionId) {
  state.activeSession = sessionId;
  renderSessions();
  refs.thread.innerHTML = `<div class="skeleton skeleton-line"></div><div class="skeleton skeleton-line"></div>`;

  try {
    const history = await api.chatHistory(sessionId, state.tenant);
    const messages = Array.isArray(history.messages) ? history.messages : [];
    if (!messages.length) {
      renderEmpty(refs.thread, "Session vide.", "Nouvelle question", null);
      return;
    }
    refs.thread.innerHTML = "";
    messages.forEach((msg) => {
      if (msg.role === "user") {
        appendUserMessage(msg.content || "");
      } else {
        appendAssistantMessage({ answer: msg.content || "", source_citations: [] });
      }
    });
  } catch (error) {
    renderError(refs.thread, error, "Réessayer", () => openSession(sessionId));
  }
}

function startNewSession() {
  state.activeSession = null;
  refs.thread.innerHTML = `
    <div class="bubble bubble-ai">
      <span class="badge badge-a4">IA</span>
      <div class="assistant-answer-wrap">
        <div class="assistant-answer">
          <p>Bonjour, comment puis-je vous aider ?</p>
          <p>Vous pouvez me demander une définition, une obligation, un écart, une preuve manquante ou une action corrective.</p>
        </div>
      </div>
    </div>
  `;
  renderSessions();
}

async function sendQuestion(question) {
  appendUserMessage(question);
  appendAssistantMessage({ answer: "" }, true);

  refs.send.disabled = true;
  refs.send.textContent = "...";

  try {
    const result = await api.chat({
      question,
      tenant_id: state.tenant,
      session_id: state.activeSession,
      user_role: refs.profile?.value || "expert",
      response_format: refs.format?.value || "synthesis",
    });

    if (result.session_id) {
      state.activeSession = result.session_id;
      upsertSession(result.session_id, question);
      renderSessions();
    }

    replacePendingAssistant(result);
    renderContextCards(result.source_citations || [], {
      search_mode: result.search_mode,
      context_snapshot: result.context_snapshot || {},
    });
  } catch (error) {
    replacePendingAssistant({ answer: `Erreur: ${error.message || "chat indisponible"}` });
  } finally {
    refs.send.disabled = false;
    refs.send.textContent = "Envoyer";
  }
}

refs.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = refs.input.value.trim();
  if (!question) {
    return;
  }
  refs.input.value = "";
  await sendQuestion(question);
});

refs.newSession?.addEventListener("click", startNewSession);

loadStoredSessions();
renderSessions();
renderSuggested();
renderContextCards();
requireRole(session.role);

loadDynamicSuggestions(state.tenant).then((questions) => renderSuggested(questions));

if (isReadOnlyRole(session.role)) {
  refs.form.classList.add("hidden");
} else if (state.sessions.length) {
  openSession(state.sessions[0].id);
} else {
  startNewSession();
}
