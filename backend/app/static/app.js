// ===========================================================================
// CodeJury frontend.
// One file, page modules dispatched from body[data-page]. No frameworks.
// ===========================================================================

// --- Helpers ---------------------------------------------------------------
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (s) => (s ?? "").toString().replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
// Minimal markdown for agent-written text (escapes first, so it's XSS-safe):
// fenced code, inline code, **bold**, #-headings, and -/* bullets.
function md(s) {
  let h = esc(s);
  h = h.replace(/```\w*\n?([\s\S]*?)```/g, (_, code) => `<pre class="md-pre">${code.replace(/\n+$/, "")}</pre>`);
  h = h.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  h = h.replace(/^\s*[-*]\s+/gm, "• ");
  h = h.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
  h = h.replace(/^#{1,4}\s+(.+)$/gm, "<b>$1</b>");
  return h;
}
const node = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; };
const setText = (id, txt) => { const e = document.getElementById(id); if (e) e.textContent = txt; };
const fmtCost = (c) => "$" + (c || 0).toFixed(2);
// A backend that reports tokens but not a dollar cost (Codex, Cursor) flags
// cost_unknown: show "n/a" (or a starred figure for a mixed total), never a
// fake $0.00 that reads as free.
const fmtCostU = (c, unknown) => (unknown ? (c ? fmtCost(c) + "*" : "n/a") : fmtCost(c));
const fmtTok = (t) => (t || 0).toLocaleString();
const fmtWhen = (iso) => iso ? new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";

function toast(msg, isError = false) {
  const t = node(`<div class="toast ${isError ? "toast-error" : ""}">${icon(isError ? "alert" : "checkCircle", 14)}<span>${esc(msg)}</span></div>`);
  $("#toasts")?.appendChild(t);
  setTimeout(() => t.remove(), 3400);
}

// `bodyNode` turns this into a small form dialog: the caller builds the fields,
// reads them itself on confirm, and gets the same true/false back.
function confirmDialog({ title, text = "", confirmLabel = "Confirm", danger = false, bodyNode = null }) {
  return new Promise((resolve) => {
    const root = $("#modal-root");
    const el = node(`
      <div class="modal-backdrop">
        <div class="modal">
          <h3>${esc(title)}</h3>
          ${text ? `<div class="modal-text">${esc(text)}</div>` : ""}
          <div class="modal-body"></div>
          <div class="modal-actions">
            <button class="btn btn-ghost" data-act="no">Cancel</button>
            <button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-act="yes">${esc(confirmLabel)}</button>
          </div>
        </div>
      </div>`);
    if (bodyNode) el.querySelector(".modal-body").appendChild(bodyNode);
    const done = (v) => { el.remove(); resolve(v); };
    el.addEventListener("click", (e) => { if (e.target === el) done(false); });
    el.querySelector('[data-act="no"]').addEventListener("click", () => done(false));
    el.querySelector('[data-act="yes"]').addEventListener("click", () => done(true));
    root.appendChild(el);
    if (bodyNode) el.querySelector("input, select, textarea")?.focus();
  });
}

// --- Session state -----------------------------------------------------------
const ROLE = document.body.dataset.role || "viewer";
const CAN_WRITE = ROLE === "member" || ROLE === "admin";
const IS_ADMIN = ROLE === "admin";
const PAGE = document.body.dataset.page;

const STATE = {
  repos: [],
  repo: null,        // selected repo object, or null = all repositories
  sessionId: null,   // scope page: selected scope session
};

const REPO_KEY = "codejury.repo";
// Pages that can aggregate across every repo ("All repositories").
const ALLOWS_ALL = new Set(["board", "agents", "costs"]);

const AGENT_META = {
  pm: { label: "PM", cls: "ag-pm", icon: "user" },
  dev: { label: "Dev", cls: "ag-dev", icon: "terminal" },
  qa: { label: "QA", cls: "ag-qa", icon: "checkCircle" },
  review: { label: "Review", cls: "ag-review", icon: "eye" },
  pr: { label: "PR", cls: "ag-pr", icon: "pr" },
};
const STATUS_LABEL = { backlog: "Backlog", scoped: "Scoped", in_dev: "In Dev", qa: "QA", review: "Review", pr: "PR", done: "Done" };
const LANE_DOT = { backlog: "", scoped: "dot-purple", approved: "dot-accent", in_dev: "dot-accent", qa: "dot-warn", review: "dot-cyan", pr: "dot-ok", done: "dot-ok" };
const RUN_BADGE = { running: ["badge-accent", "running"], completed: ["badge-ok", "done"], failed: ["badge-danger", "failed"], queued: ["badge", "queued"] };

// --- Repo switcher -------------------------------------------------------------
async function loadRepos() {
  STATE.repos = await API.repos();
  const stored = localStorage.getItem(REPO_KEY);
  if (stored === "" && ALLOWS_ALL.has(PAGE)) {
    STATE.repo = null;
  } else {
    let repo = STATE.repos.find((r) => String(r.id) === stored);
    if (!repo) repo = STATE.repos.find((r) => r.kb_status === "ready") || STATE.repos[0] || null;
    STATE.repo = repo;
  }
}

function renderRepoSwitch() {
  const nameEl = $("#repo-switch-name"), metaEl = $("#repo-switch-meta");
  $("#repo-switch-icon").innerHTML = icon("branch", 15);
  $("#repo-switch-chev").innerHTML = icon("chevronDown", 14);
  if (STATE.repo) {
    nameEl.textContent = `${STATE.repo.org}/${STATE.repo.name}`;
    const k = STATE.repo.kb_status;
    metaEl.textContent = k === "ready" ? `${STATE.repo.kb_doc_count} docs indexed`
      : k === "indexing" ? `indexing ${STATE.repo.kb_progress || 0}%` : `knowledge base ${k}`;
  } else if (STATE.repos.length) {
    nameEl.textContent = "All repositories";
    metaEl.textContent = `${STATE.repos.length} ingested`;
  } else {
    nameEl.textContent = "No repository";
    metaEl.textContent = "Add one under Knowledge";
  }
}

function initRepoSwitcher() {
  const btn = $("#repo-switch-btn"), menu = $("#repo-switch-menu");
  if (!btn) return;
  renderRepoSwitch();

  const pick = (value) => {
    localStorage.setItem(REPO_KEY, value);
    location.reload();
  };
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (!menu.hidden) { menu.hidden = true; return; }
    menu.innerHTML = "";
    if (ALLOWS_ALL.has(PAGE)) {
      menu.appendChild(node(`
        <button class="menu-item ${STATE.repo ? "" : "selected"}">${icon("layers", 14)}
          <span class="grow">All repositories</span></button>`));
      menu.lastElementChild.addEventListener("click", () => pick(""));
      if (STATE.repos.length) menu.appendChild(node(`<div class="menu-sep"></div>`));
    }
    for (const r of STATE.repos) {
      const sel = STATE.repo && STATE.repo.id === r.id;
      const dot = r.kb_status === "ready" ? "dot-ok" : r.kb_status === "indexing" ? "dot-warn pulse" : r.kb_status === "failed" ? "dot-danger" : "";
      const item = node(`
        <button class="menu-item ${sel ? "selected" : ""}">
          <span class="dot ${dot}"></span>
          <span class="grow">
            <span class="truncate" style="display:block">${esc(r.org)}/${esc(r.name)}</span>
            <span class="mi-sub">${esc(r.kb_status)}${r.kb_status === "ready" ? ` · ${r.kb_doc_count} docs` : ""}</span>
          </span>
          ${sel ? icon("check", 14) : ""}
        </button>`);
      item.addEventListener("click", () => pick(String(r.id)));
      menu.appendChild(item);
    }
    if (!STATE.repos.length) {
      menu.appendChild(node(`<div class="menu-item faint" style="cursor:default">No repositories yet</div>`));
    }
    menu.appendChild(node(`<div class="menu-sep"></div>`));
    const manage = node(`<button class="menu-item">${icon("book", 14)}<span class="grow">Manage repositories</span>${icon("arrowRight", 13)}</button>`);
    manage.addEventListener("click", () => { location.href = "/knowledge"; });
    menu.appendChild(manage);
    menu.hidden = false;
  });
  document.addEventListener("click", (e) => { if (!e.target.closest("#repo-switch")) menu.hidden = true; });
}

// --- Shell: theme, logout, nav icons, metrics ----------------------------------
function initShell() {
  $("#brand-mark") && ($("#brand-mark").innerHTML = icon("codejury", 16));
  $$(".nav-item[data-icon]").forEach((a) => a.insertAdjacentHTML("afterbegin", icon(a.dataset.icon, 16)));

  const themeBtn = $("#theme-toggle");
  const paintTheme = () => { themeBtn.innerHTML = icon(document.documentElement.dataset.theme === "light" ? "moon" : "sun", 15); };
  if (themeBtn) {
    paintTheme();
    themeBtn.addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
      document.documentElement.dataset.theme = next;
      localStorage.setItem("codejury.theme", next);
      paintTheme();
    });
  }
  const logout = $("#logout-btn");
  if (logout) {
    logout.innerHTML = icon("logout", 15);
    logout.addEventListener("click", async () => { await API.logout(); location.href = "/login"; });
  }
  // Viewers: mutation controls never render.
  if (!CAN_WRITE) $$("[data-writer]").forEach((el) => el.remove());
}

async function hydrateMetrics() {
  let ov;
  try {
    ov = await API.overview(STATE.repo ? STATE.repo.id : undefined, PAGE === "scope" ? STATE.sessionId : undefined);
  } catch (e) { return; }
  const m = ov.metrics;
  setText("m-runs", m.runs);
  setText("m-active", m.active);
  setText("m-tokens", `${m.tokens_in_label} / ${m.tokens_out_label}`);
  setText("m-cost", `$${m.cost_usd}`);
}

// --- Ticket drawer -----------------------------------------------------------
let drawerOnChange = null;

function closeDrawer() { document.body.classList.remove("drawer-open"); }

async function openTicket(id, onChange) {
  drawerOnChange = onChange || null;
  const body = $("#drawer-body");
  body.innerHTML = `<div class="row faint small" style="padding:20px"><div class="spinner"></div> Loading ticket…</div>`;
  document.body.classList.add("drawer-open");
  try { renderDrawer(await API.taskDetail(id), "details"); }
  catch (e) { body.innerHTML = `<div class="small" style="color:var(--danger); padding:20px">${esc(e.message)}</div>`; }
}

function renderDrawer(d, tab) {
  const t = d.task, body = $("#drawer-body");
  const ag = AGENT_META[t.current_agent];
  const draft = t.status === "scoped" || t.status === "backlog";
  const running = ["in_dev", "qa", "review"].includes(t.status);

  const dparts = (t.description || "").split(/\n{2,}\[Change request\]\s*/);
  const baseDesc = (dparts[0] || "").trim() || "—";
  const changeReqs = dparts.slice(1).map((s) => s.trim()).filter(Boolean);

  body.innerHTML = `
    <div class="panel-head" style="padding:14px 18px">
      <div class="grow">
        <div class="mono small" style="color:var(--accent-strong)">${esc(t.key)}</div>
        <div style="font-size:14.5px; font-weight:650; line-height:1.35; margin-top:2px">${esc(t.title)}</div>
        <div class="row" style="margin-top:8px; flex-wrap:wrap">
          <span class="badge">${esc(STATUS_LABEL[t.status] || t.status)}</span>
          <span class="badge">${esc(t.priority)}</span>
          ${ag ? `<span class="badge ${ag.cls}" style="border-color:currentColor">${ag.label} agent</span>` : ""}
          ${t.approved ? `<span class="badge badge-ok">approved</span>` : ""}
        </div>
      </div>
      <button class="icon-btn" id="drawer-close">${icon("x", 16)}</button>
    </div>
    <div class="tabs" style="padding:0 12px; flex-shrink:0">
      <button class="tab ${tab === "details" ? "active" : ""}" data-tab="details">Details</button>
      <button class="tab ${tab === "review" ? "active" : ""}" data-tab="review">Review &amp; diff</button>
    </div>
    <div class="panel-body" id="drawer-tab-body" style="padding:16px 18px"></div>`;

  $("#drawer-close").addEventListener("click", closeDrawer);
  $$(".tab", body).forEach((b) => b.addEventListener("click", () => renderDrawer(d, b.dataset.tab)));

  const tabBody = $("#drawer-tab-body");
  if (tab === "review") { renderDrawerReview(d, tabBody); return; }

  // --- Details tab ---
  const crit = (t.acceptance_criteria || []).map((c) =>
    `<li class="small muted" style="margin-bottom:4px">${esc(c)}</li>`).join("") || `<li class="small faint">—</li>`;
  const runsRows = (d.runs || []).map((r) => {
    const a = AGENT_META[r.agent_type] || { label: r.agent_type, cls: "" };
    const [bcls, blabel] = RUN_BADGE[r.status] || RUN_BADGE.queued;
    const dur = r.duration_ms ? `${(r.duration_ms / 1000).toFixed(1)}s` : (r.status === "running" ? "…" : "—");
    return `<div class="row small" style="padding:5px 0; border-bottom:1px solid var(--border)">
      <span class="${a.cls}" style="width:52px; font-weight:600">${a.label}</span>
      <span class="badge ${bcls}">${blabel}</span>
      <span class="grow"></span>
      <span class="faint mono">${fmtTok(r.tokens)} tok</span>
      <span class="faint mono" style="width:52px; text-align:right">${dur}</span>
    </div>`;
  }).join("") || `<div class="small faint">No runs yet.</div>`;

  // A scope is the unit of work: running delivers ALL approved subtasks together.
  const scopeN = d.scope && d.scope.sibling_count > 1 ? d.scope.sibling_count : 0;
  let actions = "";
  if (CAN_WRITE && draft && !t.approved) actions += `<button class="btn btn-primary grow" data-act="approve">Approve</button>`;
  if (CAN_WRITE && draft && t.approved && t.session_id) actions += `<button class="btn btn-primary grow" data-act="run">${icon("play", 13)} Run scope${scopeN ? ` (${scopeN} tickets)` : ""}</button>`;
  if (running) actions += `<a class="btn btn-soft grow" href="/agents">${icon("activity", 13)} Watch live</a>`;
  if (t.pr_url) actions += `<a class="btn btn-ghost grow" href="${esc(t.pr_url)}" target="_blank" rel="noopener">${icon("external", 13)} Open PR</a>`;

  const canRequest = CAN_WRITE && (draft || t.status === "pr");
  const requestBlock = canRequest ? `
    <div style="border-top:1px solid var(--border); padding-top:14px; margin-top:16px">
      <div class="field">
        <label>Request a change</label>
        <textarea class="input" id="change-note" rows="3" placeholder="e.g. Add input validation and a unit test. The whole scope returns to Scoped (approved) so it can be re-run together."></textarea>
      </div>
      <button class="btn btn-soft btn-block" id="change-submit" style="margin-top:8px">Record change &amp; make re-runnable</button>
    </div>` : (running ? `<div class="small faint" style="border-top:1px solid var(--border); padding-top:12px; margin-top:16px">Pipeline is running — wait for it to finish to request changes.</div>` : "");

  tabBody.innerHTML = `
    ${d.scope ? `<div class="card card-pad" style="margin-bottom:14px">
      <div class="small faint" style="margin-bottom:3px">Scope${d.scope.title ? " · " + esc(d.scope.title) : ""}</div>
      <div class="small muted">${esc(d.scope.summary || "")}</div></div>` : ""}
    <div style="margin-bottom:14px">
      <div class="stat-label" style="margin-bottom:5px">Description</div>
      <div class="small muted" style="white-space:pre-wrap">${md(baseDesc)}</div>
    </div>
    ${changeReqs.length ? `<div style="margin-bottom:14px">
      <div class="stat-label" style="color:var(--warn); margin-bottom:5px">Change requests (${changeReqs.length})</div>
      ${changeReqs.map((c) => `<div class="small muted" style="border-left:2px solid var(--warn); padding-left:9px; margin-bottom:6px">${esc(c)}</div>`).join("")}</div>` : ""}
    <div style="margin-bottom:14px">
      <div class="stat-label" style="margin-bottom:5px">Acceptance criteria</div>
      <ul style="margin:0; padding-left:18px">${crit}</ul>
    </div>
    <div style="margin-bottom:16px">
      <div class="stat-label" style="margin-bottom:5px">Agent runs</div>
      ${runsRows}
    </div>
    <div class="row">${actions || `<span class="small faint">No actions available</span>`}</div>
    ${requestBlock}`;

  const refresh = async () => {
    try { renderDrawer(await API.taskDetail(t.id), "details"); } catch (e) {}
    if (drawerOnChange) drawerOnChange();
  };
  tabBody.querySelector('[data-act="approve"]')?.addEventListener("click", async () => {
    try { await API.approve(t.id); toast(`${t.key} approved`); } catch (e) { toast(e.message, true); }
    refresh();
  });
  tabBody.querySelector('[data-act="run"]')?.addEventListener("click", async () => {
    toast(scopeN ? `Launching scope (${scopeN} tickets)…` : `Launching ${t.key}…`);
    try { await API.runScope(t.session_id); } catch (e) { toast(e.message, true); }
    setTimeout(refresh, 700);
  });
  $("#change-submit")?.addEventListener("click", async () => {
    const note = ($("#change-note")?.value || "").trim();
    if (!note) { toast("Type what to change first", true); return; }
    try { await API.requestChanges(t.id, note); toast(scopeN ? "Change recorded — scope is re-runnable" : `Change recorded for ${t.key}`); } catch (e) { toast(e.message, true); }
    refresh();
  });
}

// --- Jury verdict panel -----------------------------------------------------
// Blocking findings first, then observations, then what the foreperson threw
// out — the dismissals matter: they're the evidence the panel is filtering
// noise rather than just generating more of it.

function juryFindingHtml(f, cls) {
  const who = (f.raised_by || []).join(", ");
  const meta = [
    f.severity,
    f.confidence != null ? `${Math.round(f.confidence * 100)}% confidence` : "",
    who ? `raised by ${who}` : "",
    f.agreement && f.agreement !== "single" ? f.agreement : "",
  ].filter(Boolean).join(" · ");
  const body = [
    f.why_it_matters, f.suggestion ? `Fix: ${f.suggestion}` : "",
    f.reason ? `Dismissed: ${f.reason}` : "",
  ].filter(Boolean).join("\n");
  return `<div class="jv-finding ${cls}">
    <div class="jv-title">${esc(f.title)}${f.location ? ` <span class="mono faint">${esc(f.location)}</span>` : ""}</div>
    <div class="jv-meta">${esc(meta)}</div>
    ${body ? `<div class="jv-body" style="white-space:pre-wrap">${esc(body)}</div>` : ""}
  </div>`;
}

// One juror's own review, verbatim: how it voted, what it said, and every
// finding it filed — including the ones the foreperson merged or threw out.
// The synthesis is the decision; this is the evidence behind it, and without it
// there's no way to tell a panel that agreed from a panel that never spoke.
const JV_VERDICT = {
  APPROVE: { label: "approved", cls: "jv-v-ok" },
  REQUEST_CHANGES: { label: "changes requested", cls: "jv-v-bad" },
  ABSTAIN: { label: "abstained", cls: "jv-v-abstain" },
};

function jurorCardHtml(o) {
  const v = JV_VERDICT[o.verdict] || JV_VERDICT.ABSTAIN;
  const findings = o.findings || [];
  const count = findings.length;
  return `<details class="jv-juror-card ${o.error ? "jv-abstain" : ""}" ${count ? "open" : ""}>
    <summary>
      <span class="jv-j-name">${esc(o.name)}</span>
      <span class="badge ${v.cls}">${v.label}</span>
      <span class="grow"></span>
      <span class="small faint">${count ? `${count} finding${count === 1 ? "" : "s"}` : "no findings"}</span>
      <span class="small faint mono jv-j-model">${esc(o.model_label || o.model || "")}</span>
    </summary>
    <div class="jv-j-body">
      ${o.error
        ? `<div class="jv-j-error">Did not review — ${esc(o.error)}
             <div class="small faint" style="margin-top:4px">This perspective was NOT covered on this delivery.</div></div>`
        : ""}
      ${o.summary ? `<div class="jv-j-summary">${esc(o.summary)}</div>` : ""}
      ${findings.map((f) => {
        const meta = [f.severity,
          f.confidence != null ? `${Math.round(f.confidence * 100)}% confidence` : ""]
          .filter(Boolean).join(" · ");
        return `<div class="jv-finding jv-own">
          <div class="jv-title">${esc(f.title)}${f.location ? ` <span class="mono faint">${esc(f.location)}</span>` : ""}</div>
          <div class="jv-meta">${esc(meta)}</div>
          ${f.evidence ? `<pre class="jv-evidence">${esc(f.evidence)}</pre>` : ""}
          ${f.why_it_matters ? `<div class="jv-body">${esc(f.why_it_matters)}</div>` : ""}
          ${f.suggestion ? `<div class="jv-body"><b>Suggested fix:</b> ${esc(f.suggestion)}</div>` : ""}
        </div>`;
      }).join("")}
    </div>
  </details>`;
}

// The Planner's plan for this delivery. It is shown ABOVE the diff on purpose:
// it is what lets a reviewer read the change as "did it do the agreed thing"
// rather than "does this look plausible", and the pins in it were verified
// against the code graph before any code was written.
function planHtml(p) {
  const steps = (p && p.steps) || [];
  if (!steps.length) return "";
  const kindColor = { create: "var(--ok)", delete: "var(--danger)", wire: "var(--accent)" };
  const stepRows = steps.map((s) => {
    const bits = [];
    if (s.files && s.files.length) bits.push(`<div class="small mono faint">${esc(s.files.join(", "))}</div>`);
    if (s.symbols && s.symbols.length) bits.push(`<div class="small mono">${esc(s.symbols.join("  ·  "))}</div>`);
    if (s.blast_radius && s.blast_radius.length) {
      bits.push(`<div class="small muted">${icon("alert", 11)} callers affected: ${esc(s.blast_radius.join("; "))}</div>`);
    }
    if (s.existing_tests && s.existing_tests.length) {
      bits.push(`<div class="small muted">covered by ${esc(s.existing_tests.join(", "))}</div>`);
    }
    if (s.verify && s.verify.length) bits.push(`<div class="small muted">done when: ${esc(s.verify.join("; "))}</div>`);
    return `
      <div style="padding:8px 0; border-top:1px solid var(--border)">
        <div class="row small" style="font-weight:650; gap:6px">
          <span class="badge" style="color:${kindColor[s.edit_kind] || "var(--text-faint)"}">${esc(s.edit_kind || "modify")}</span>
          <span>${esc(s.intent || "")}</span>
        </div>
        ${s.why ? `<div class="small muted" style="margin:2px 0 4px">${esc(s.why)}</div>` : ""}
        ${bits.join("")}
      </div>`;
  }).join("");
  const notes = [];
  if (p.risks && p.risks.length) {
    notes.push(`<div class="small" style="color:var(--warn); margin-top:8px">${icon("alert", 12)} Risks: ${esc(p.risks.join("; "))}</div>`);
  }
  if (p.open_questions && p.open_questions.length) {
    // Unconfirmed by the Planner — a reader must not mistake these for decisions.
    notes.push(`<div class="small" style="color:var(--warn); margin-top:4px">${icon("alert", 12)} Could not confirm: ${esc(p.open_questions.join("; "))}</div>`);
  }
  if (p.unresolved_symbols) {
    notes.push(`<div class="small muted" style="margin-top:4px">${p.unresolved_symbols} symbol(s) did not exist yet and were to be created.</div>`);
  }
  return `
    <div class="card card-pad" style="margin-bottom:14px">
      <div class="row" style="margin-bottom:4px">
        <span class="stat-label">Implementation plan</span>
        <span class="small faint">${steps.length} step${steps.length === 1 ? "" : "s"} · pins verified against the code graph</span>
      </div>
      ${p.summary ? `<div class="small" style="margin-bottom:4px">${esc(p.summary)}</div>` : ""}
      ${stepRows}
      ${notes.join("")}
    </div>`;
}

function juryVerdictHtml(j) {
  if (!j || !j.verdict) return "";
  const ok = j.verdict === "APPROVED";
  const color = ok ? "var(--ok)" : j.verdict === "INCONCLUSIVE" ? "var(--warn)" : "var(--danger)";
  const jurors = j.jurors || [];
  const spoke = jurors.filter((o) => !o.error).length;
  const sec = (title, items, cls, hint) => items && items.length
    ? `<div class="jv-sec-head">${title} <span class="faint">(${items.length})</span>
         ${hint ? `<span class="small faint jv-sec-hint">${hint}</span>` : ""}</div>
       ${items.map((f) => juryFindingHtml(f, cls)).join("")}` : "";

  return `<div class="card card-pad jv-card" style="border-color:${color}; margin-bottom:14px">
    <div class="jv-head">
      <span style="color:${color}">${icon(ok ? "checkCircle" : "alert", 16)}</span>
      <span style="font-weight:650; color:${color}">Jury verdict: ${esc(j.verdict)}</span>
      <div class="grow"></div>
      <span class="small faint">${spoke} of ${jurors.length} judges reviewed</span>
      ${j.synthesis === "deterministic"
        ? `<span class="badge" title="The foreperson model could not be reached — findings were merged mechanically and only critical/high were allowed to block">mechanical synthesis</span>` : ""}
    </div>

    ${j.rationale ? `<div class="jv-rationale"><b>Foreperson.</b> ${esc(j.rationale)}
      ${j.foreperson ? `<span class="small faint mono"> — ${esc(j.foreperson)}</span>` : ""}</div>` : ""}

    ${sec("Blocking", j.blocking, "jv-block", "must be fixed before merge")}
    ${sec("Observations", j.observations, "jv-obs", "noted, not blocking")}
    ${sec("Dismissed", j.dismissed, "jv-dropped", "raised by a judge, rejected by the foreperson")}

    ${jurors.length ? `<div class="jv-sec-head">Each judge's own review
        <span class="small faint jv-sec-hint">what every perspective actually reported</span></div>
      ${jurors.map(jurorCardHtml).join("")}` : ""}
  </div>`;
}

async function renderDrawerReview(d, tabBody) {
  const t = d.task;
  const scopeN = d.scope && d.scope.sibling_count > 1 ? d.scope.sibling_count : 0;
  tabBody.innerHTML = `<div class="row faint small"><div class="spinner"></div> Loading review…</div>`;
  let r;
  try { r = await API.review(t.id); } catch (e) {
    tabBody.innerHTML = `<div class="small" style="color:var(--danger)">${esc(e.message)}</div>`;
    return;
  }
  const checks = r.checks.map((c) => `
    <div class="row small" style="padding:4px 0">
      <span style="color:${c.ok ? "var(--ok)" : "var(--text-faint)"}">${icon(c.ok ? "checkCircle" : "clock", 15)}</span>
      <span class="${c.ok ? "" : "faint"}">${esc(c.label)}</span>
    </div>`).join("");
  const diffLines = (r.pr.diff || []).map((l) => {
    if (l.type === "file") {
      return `<div class="dl dl-file"><span class="no">${icon("fileCode", 12)}</span><span class="tx">${esc(l.text)}</span></div>`;
    }
    const cls = l.type === "add" ? "dl-add" : l.type === "del" ? "dl-del" : l.type === "hunk" ? "dl-hunk" : "";
    const sign = l.type === "add" ? "+" : l.type === "del" ? "-" : " ";
    return `<div class="dl ${cls}"><span class="no">${l.n ?? ""}</span><span class="tx">${esc((l.type === "hunk" ? "" : sign) + l.text)}</span></div>`;
  }).join("");
  const observations = (r.observations || []).map((o) => `
    <div class="card card-pad" style="border-color:${o.level === "warn" ? "var(--warn)" : "var(--border)"}; margin-bottom:10px">
      <div class="row small" style="font-weight:650; margin-bottom:4px; color:${o.level === "warn" ? "var(--warn)" : "var(--text)"}">
        ${icon(o.level === "warn" ? "alert" : "info", 14)} ${esc(o.title)}</div>
      <div class="small muted" style="white-space:pre-wrap">${md(o.text)}</div>
    </div>`).join("");
  const qaNotes = (r.qa_notes || []).map((n) => `<div class="small muted" style="white-space:pre-wrap; margin-bottom:10px">${md(n)}</div>`).join("");

  const canMerge = CAN_WRITE && t.status === "pr";
  const canCreatePr = CAN_WRITE && !t.pr_url && (t.status === "pr" || t.status === "done");
  const mergeLabel = scopeN ? `Merge scope — mark ${scopeN} tickets done` : "Merge — mark done";
  const actionRow = (canMerge || canCreatePr) ? `
    ${scopeN ? `<div class="small faint" style="margin-bottom:8px">${icon("info", 12)} This scope delivers as one unit — the actions below apply to all ${scopeN} tickets.</div>` : ""}
    <div class="row" style="margin-bottom:16px">
      ${canCreatePr ? `<button class="btn btn-soft grow" id="rv-create-pr">${icon("pr", 13)} Create PR on GitHub</button>` : ""}
      ${canMerge ? `<button class="btn btn-primary grow" id="rv-merge">${icon("pr", 13)} ${mergeLabel}</button>
      <button class="btn btn-ghost grow" id="rv-request">Request changes</button>` : ""}
    </div>` : "";

  tabBody.innerHTML = `
    <div class="card card-pad" style="margin-bottom:14px">
      <div class="row">
        <span class="mono small muted">PR ${esc(String(r.pr.number))}</span>
        <span class="badge ${r.pr.status === "MERGED" ? "badge-ok" : r.pr.status === "OPEN" ? "badge-accent" : ""}">${esc(r.pr.status)}</span>
        ${t.pr_url ? `<a class="small" href="${esc(t.pr_url)}" target="_blank" rel="noopener">${icon("external", 12)} on GitHub</a>` : ""}
        <div class="grow"></div>
        <span class="small mono" style="color:var(--ok)">+${r.pr.insertions}</span>
        <span class="small mono" style="color:var(--danger)">−${r.pr.deletions}</span>
      </div>
    </div>
    ${actionRow}
    <div style="margin-bottom:16px">${checks}</div>
    ${planHtml(r.plan)}
    ${juryVerdictHtml(r.jury)}
    ${observations}
    ${qaNotes ? `<div style="margin-bottom:14px"><div class="stat-label" style="margin-bottom:6px">QA notes</div>${qaNotes}</div>` : ""}
    <div class="stat-label" style="margin-bottom:6px">Diff</div>
    <div class="card diff" style="padding:8px 0; overflow-x:auto">${diffLines || `<div class="small faint" style="padding:0 14px">No diff yet.</div>`}</div>`;

  $("#rv-create-pr")?.addEventListener("click", async () => {
    if (!(await confirmDialog({ title: "Create the pull request?", text: `The delivery branch is pushed and the PR is opened on GitHub. If you've connected your GitHub account (Settings → Access), it's opened as you — forking the repo first if you don't have push access; otherwise it's opened as the agent.`, confirmLabel: "Create PR" }))) return;
    const btn = $("#rv-create-pr"); btn.disabled = true; btn.textContent = "Creating…";
    try {
      const r = await API.createPr(t.id);
      const via = r.forked ? ` (forked to ${r.fork_repo})` : "";
      toast(r.created ? `PR opened by ${r.author || "the agent"}${via} ✓` : "PR already exists");
      window.open(r.url, "_blank", "noopener");
    } catch (e) { toast(e.message, true); }
    renderDrawer(await API.taskDetail(t.id), "review");
    if (drawerOnChange) drawerOnChange();
  });
  $("#rv-merge")?.addEventListener("click", async () => {
    if (!(await confirmDialog({ title: "Merge this delivery?", text: scopeN ? `All ${scopeN} tickets in this scope will be marked done.` : `${t.key} will be marked done.`, confirmLabel: "Merge" }))) return;
    try { await API.merge(t.id); toast(scopeN ? `Scope merged — ${scopeN} tickets done ✓` : "Merged ✓"); } catch (e) { toast(e.message, true); }
    renderDrawer(await API.taskDetail(t.id), "review");
    if (drawerOnChange) drawerOnChange();
  });
  $("#rv-request")?.addEventListener("click", async () => {
    try { await API.requestChanges(t.id); toast(scopeN ? "Scope returned to Scoped (approved) for a re-run" : "Returned to Scoped (approved) for a re-run"); } catch (e) { toast(e.message, true); }
    renderDrawer(await API.taskDetail(t.id), "details");
    if (drawerOnChange) drawerOnChange();
  });
}

// --- Page: Scope Chat -----------------------------------------------------------
function chatMsgNode(msg) {
  if (msg.role === "user") {
    return node(`<div class="msg msg-user"><div class="msg-body"><div class="msg-bubble">${esc(msg.content)}</div></div></div>`);
  }
  const sources = (msg.sources || []).map((s) => `<span class="src">${esc(s)}</span>`).join("");
  return node(`
    <div class="msg msg-agent">
      <div class="msg-avatar">${icon("bot", 15)}</div>
      <div class="msg-body">
        <div class="msg-bubble">${md(msg.content)}</div>
        ${sources ? `<div class="msg-sources">${sources}</div>` : ""}
      </div>
    </div>`);
}

function renderChat(messages) {
  const box = $("#chat-messages");
  box.innerHTML = "";
  for (const m of messages) box.appendChild(chatMsgNode(m));
  const scroll = $("#chat-scroll");
  scroll.scrollTop = scroll.scrollHeight;
}

function scopeIntro() {
  $("#chat-messages").appendChild(node(`
    <div class="msg msg-agent">
      <div class="msg-avatar">${icon("bot", 15)}</div>
      <div class="msg-body"><div class="msg-bubble">Describe the feature you want to build. I'll ask clarifying questions grounded in this repository's actual code, lock the scope, and then you can draft and approve tickets on the right.</div></div>
    </div>`));
}

function updateScopeState(session) {
  const ready = session && session.status === "scoped";
  const draftBtn = $("#draft-btn");
  if (draftBtn) draftBtn.hidden = !ready;
  const sum = $("#scope-summary");
  if (!sum) return;
  if (ready && (session.requirement_summary || (session.acceptance_criteria || []).length)) {
    const crit = (session.acceptance_criteria || []).map((c) => `<li class="small muted" style="margin-bottom:3px">${esc(c)}</li>`).join("");
    sum.innerHTML = `
      <div class="card card-pad" style="border-color:var(--accent)">
        <div class="row small" style="color:var(--accent-strong); font-weight:650; margin-bottom:6px">${icon("checkCircle", 14)} Locked scope</div>
        <div class="small" style="margin-bottom:8px">${esc(session.requirement_summary || "")}</div>
        <ul style="margin:0; padding-left:16px">${crit}</ul>
      </div>`;
    sum.hidden = false;
  } else {
    sum.hidden = true;
  }
}

async function loadStories() {
  const box = $("#stories");
  if (!box || !STATE.repo) return;
  const tasks = (await API.tasks(STATE.repo.id))
    .filter((t) => (t.status === "scoped" || t.status === "backlog") && (!STATE.sessionId || t.session_id === STATE.sessionId))
    .sort((a, b) => a.key.localeCompare(b.key));
  const approved = tasks.filter((t) => t.approved).length;
  const rb = $("#run-scope-btn");
  if (rb) {
    rb.hidden = !approved;
    rb.innerHTML = `${icon("zap", 14)} Run scope — ${approved} ticket${approved !== 1 ? "s" : ""} → one PR`;
  }
  box.innerHTML = "";
  if (!tasks.length) {
    box.appendChild(node(`<div class="small faint" style="line-height:1.55">No tickets in this scope yet. Lock the scope in chat, then click <b>Draft tickets</b>.</div>`));
    return;
  }
  for (const t of tasks) {
    const crit = (t.acceptance_criteria || []).slice(0, 3).map((c) => `<li class="small faint" style="margin-bottom:2px">${esc(c)}</li>`).join("");
    const action = !CAN_WRITE ? "" : (t.approved
      ? `<span class="row small" style="color:var(--ok)">${icon("checkCircle", 13)} Approved</span>`
      : `<button class="btn btn-primary btn-sm approve-btn">Approve</button>`);
    const card = node(`
      <div class="ticket">
        <div class="row"><span class="t-key">${esc(t.key)}</span><div class="grow"></div><span class="badge">${esc(t.priority)}</span></div>
        <div class="t-title">${esc(t.title)}</div>
        <ul style="margin:0 0 8px; padding-left:16px">${crit}</ul>
        <div class="t-foot">${action}
          ${t.jira_key ? `<a class="row small" href="${esc(t.jira_url)}" target="_blank" rel="noopener">${icon("external", 12)} ${esc(t.jira_key)}</a>` : ""}
        </div>
      </div>`);
    card.querySelector(".approve-btn")?.addEventListener("click", async (e) => {
      e.stopPropagation();
      e.target.disabled = true;
      try { await API.approve(t.id); toast(`${t.key} approved`); } catch (er) { toast(er.message, true); }
      loadStories();
    });
    card.addEventListener("click", (e) => { if (e.target.closest("button,a")) return; openTicket(t.id, loadStories); });
    box.appendChild(card);
  }
}

async function switchScope(session) {
  STATE.sessionId = session.id;
  const detail = await API.session(session.id);
  $("#chat-messages").innerHTML = "";
  if (detail.messages.length) renderChat(detail.messages); else scopeIntro();
  updateScopeState(detail.session);
  setText("scope-switch-title", detail.session.title && detail.session.title !== "Feature scoping" ? detail.session.title.slice(0, 34) : "Scopes");
  await loadStories();
  hydrateMetrics();
}

async function initScope() {
  $("#chat-send").innerHTML = icon("send", 15);
  $("#board-arrow") && ($("#board-arrow").innerHTML = icon("arrowRight", 13));

  if (!STATE.repo) {
    $("#chat-messages").appendChild(node(`
      <div class="empty" style="padding-top:80px">
        ${icon("book", 26)}
        <div class="e-title">No repository yet</div>
        <div class="e-sub">The PM agent scopes features against a repository's knowledge base. Add one first.</div>
        <a class="btn btn-primary" href="/knowledge" style="margin-top:4px">Add a repository</a>
      </div>`));
    return;
  }
  if (STATE.repo.kb_status !== "ready") {
    $("#chat-messages").appendChild(node(`
      <div class="empty" style="padding-top:80px">
        ${icon("clock", 26)}
        <div class="e-title">Knowledge base is ${esc(STATE.repo.kb_status)}</div>
        <div class="e-sub">Scoping works best once ${esc(STATE.repo.org)}/${esc(STATE.repo.name)} finishes indexing. Check progress under Knowledge.</div>
        <a class="btn btn-ghost" href="/knowledge" style="margin-top:4px">View progress</a>
      </div>`));
    if (STATE.repo.kb_status !== "indexing") return;
  }

  const sessions = await API.sessions(STATE.repo.id, "pm");
  const current = sessions[0] || (CAN_WRITE ? await API.createSession(STATE.repo.id, "pm", "Feature scoping") : null);
  if (current) await switchScope(current);

  // Composer
  const input = $("#chat-input"), send = $("#chat-send");
  let busy = false;
  const autogrow = () => {
    if (!input.value) { input.style.height = ""; return; }
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 160) + "px";
  };
  const submit = async () => {
    const content = input.value.trim();
    if (!content || busy || !STATE.sessionId) return;
    if (!CAN_WRITE) { toast("Viewers can't drive the PM agent", true); return; }
    busy = true; send.disabled = true; input.value = ""; autogrow();
    const box = $("#chat-messages");
    box.appendChild(chatMsgNode({ role: "user", content }));
    const pending = node(`
      <div class="msg msg-agent" style="opacity:.75">
        <div class="msg-avatar">${icon("bot", 15)}</div>
        <div class="msg-body"><div class="msg-bubble row"><div class="spinner"></div><span class="faint">PM agent is thinking…</span></div></div>
      </div>`);
    box.appendChild(pending);
    $("#chat-scroll").scrollTop = $("#chat-scroll").scrollHeight;
    try {
      const detail = await API.scopeTurn(STATE.sessionId, content);
      renderChat(detail.messages);
      updateScopeState(detail.session);
      setText("scope-switch-title", (detail.session.title || "Scopes").slice(0, 34));
      loadStories();
      hydrateMetrics();
    } catch (e) {
      pending.querySelector(".msg-bubble").innerHTML = `<span style="color:var(--danger)">${esc(e.message)}</span>`;
    } finally { busy = false; send.disabled = false; }
  };
  send.addEventListener("click", submit);
  input.addEventListener("input", autogrow);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } });

  // Draft tickets / run scope
  $("#draft-btn")?.addEventListener("click", async () => {
    const btn = $("#draft-btn"); btn.disabled = true;
    toast("PM agent drafting tickets…");
    try {
      const made = await API.createTasks(STATE.sessionId);
      toast(`Drafted ${made.created.length} ticket(s) — review & approve`);
      loadStories();
    } catch (e) { toast(e.message, true); }
    btn.disabled = false;
  });
  $("#run-scope-btn")?.addEventListener("click", async () => {
    const btn = $("#run-scope-btn"); btn.disabled = true;
    try {
      await API.runScope(STATE.sessionId);
      toast("Scope launched → one PR. Redirecting to the board…");
      setTimeout(() => { location.href = "/board"; }, 900);
    } catch (e) { toast(e.message, true); btn.disabled = false; }
  });

  // Scope switcher + new scope
  const sBtn = $("#scope-switch-btn"), sMenu = $("#scope-switch-menu");
  $("#scope-switch-chev").innerHTML = icon("chevronDown", 13);
  sBtn?.addEventListener("click", async (e) => {
    e.stopPropagation();
    if (!sMenu.hidden) { sMenu.hidden = true; return; }
    const list = await API.sessions(STATE.repo.id, "pm");
    sMenu.innerHTML = "";
    if (!list.length) sMenu.appendChild(node(`<div class="menu-item faint" style="cursor:default">No scopes yet</div>`));
    for (const s of list) {
      const active = s.id === STATE.sessionId;
      const item = node(`
        <button class="menu-item ${active ? "selected" : ""}">
          <span style="color:${s.status === "scoped" ? "var(--ok)" : "var(--text-faint)"}">${icon(s.status === "scoped" ? "checkCircle" : "chat", 14)}</span>
          <span class="grow">
            <span class="truncate" style="display:block">${esc(s.title || "Untitled scope")}</span>
            <span class="mi-sub">#${s.id} · ${esc(s.status)} · ${fmtWhen(s.created_at)}</span>
          </span>
          ${active ? icon("check", 14) : ""}
        </button>`);
      item.addEventListener("click", () => { sMenu.hidden = true; switchScope(s); });
      sMenu.appendChild(item);
    }
    sMenu.hidden = false;
  });
  document.addEventListener("click", (e) => { if (!e.target.closest("#scope-switch")) sMenu.hidden = true; });

  $("#new-scope-btn")?.addEventListener("click", async () => {
    const s = await API.createSession(STATE.repo.id, "pm", "Feature scoping");
    await switchScope(s);
    toast("New scope — describe the feature");
  });
}

// --- Page: Board -----------------------------------------------------------------
async function initBoard() {
  const reload = async () => {
    let data;
    try { data = await API.board(STATE.repo ? STATE.repo.id : undefined); } catch (e) { return; }
    renderBoard(data, reload);
  };
  await reload();
  setInterval(reload, 3000);
}

function renderBoard(data, reload) {
  const box = $("#board");
  box.innerHTML = "";
  let total = 0;
  for (const col of data.columns) {
    total += col.tasks.length;
    const lane = node(`
      <div class="lane">
        <div class="lane-head">
          <span class="dot ${LANE_DOT[col.status] || ""}"></span>
          <span class="lane-title">${esc(col.title)}</span>
          <span class="lane-count">${col.tasks.length}</span>
        </div>
        <div class="lane-cards"></div>
      </div>`);
    const cards = $(".lane-cards", lane);
    for (const t of col.tasks) cards.appendChild(boardCard(t, reload));
    box.appendChild(lane);
  }
  setText("board-count", `${total} ticket${total !== 1 ? "s" : ""} across the pipeline`);
}

function boardCard(t, reload) {
  const ag = AGENT_META[t.current_agent];
  const draft = t.status === "scoped" || t.status === "backlog";
  let action = "";
  if (CAN_WRITE && draft && !t.approved) action = `<button class="btn btn-primary btn-sm approve-btn">Approve</button>`;
  else if (CAN_WRITE && draft && t.approved && t.session_id) action = `<button class="btn btn-soft btn-sm run-btn" title="Runs the whole scope">${icon("play", 11)} Run scope</button>`;
  else if (["in_dev", "qa", "review"].includes(t.status)) action = `<a class="row small" style="color:var(--text-faint)" href="/agents">${icon("activity", 12)} live</a>`;
  else if (t.status === "done") action = `<span style="color:var(--ok)">${icon("checkCircle", 14)}</span>`;
  const card = node(`
    <div class="ticket" title="Open ticket">
      <div class="row"><span class="t-key">${esc(t.key)}</span><div class="grow"></div><span class="small faint">${esc(t.priority)}</span></div>
      <div class="t-title">${esc(t.title)}</div>
      <div class="t-foot">
        ${ag ? `<span class="small ${ag.cls}" style="font-weight:600">${ag.label}</span>` : "<span></span>"}
        ${action}
      </div>
    </div>`);
  card.querySelector(".approve-btn")?.addEventListener("click", async (e) => {
    e.stopPropagation(); e.target.disabled = true;
    try { await API.approve(t.id); toast(`${t.key} approved`); } catch (er) { toast(er.message, true); }
    reload();
  });
  card.querySelector(".run-btn")?.addEventListener("click", async (e) => {
    e.stopPropagation();
    toast("Launching scope…");
    try { await API.runScope(t.session_id); } catch (er) { toast(er.message, true); }
    setTimeout(reload, 800);
  });
  card.addEventListener("click", (e) => { if (e.target.closest("button,a")) return; openTicket(t.id, reload); });
  return card;
}

// --- Page: Agents ----------------------------------------------------------------
let selectedRun = null;

async function initAgents() {
  const load = async () => {
    let stats, runs, tasks, sessions;
    try {
      [stats, runs, tasks, sessions] = await Promise.all([
        API.stats(STATE.repo ? STATE.repo.id : undefined),
        API.runs(STATE.repo ? { repo_id: STATE.repo.id } : {}),
        STATE.repo ? API.tasks(STATE.repo.id) : API.get("/tasks"),
        STATE.repo ? API.sessions(STATE.repo.id, "pm").catch(() => []) : Promise.resolve([]),
      ]);
    } catch (e) { return; }
    renderAgentStats(stats);
    const keyById = Object.fromEntries(tasks.map((t) => [t.id, t.key]));
    const titleById = Object.fromEntries(tasks.map((t) => [t.id, t.title]));
    const sessionById = Object.fromEntries(tasks.map((t) => [t.id, t.session_id]));
    const scopeTitleById = Object.fromEntries(sessions.map((s) => [s.id, s.title]));
    renderRunGroups(runs, keyById, titleById, sessionById, scopeTitleById);
    if (!selectedRun && runs.length) selectRun((runs.find((r) => r.status === "running") || runs[0]).id, keyById);
    else if (selectedRun) refreshLog(selectedRun, keyById);
  };
  await load();
  setInterval(load, 3000);
}

function renderAgentStats(s) {
  const box = $("#agent-stats");
  box.innerHTML = "";
  for (const [label, val, sub] of [
    ["Active agents", s.active_agents, "running now"],
    ["Total tokens", fmtTok(s.total_tokens), "in + out"],
    ["Total cost", `$${s.total_cost_usd}`, "usd"],
    ["Done today", s.tasks_completed_today, "tickets"],
  ]) {
    box.appendChild(node(`
      <div class="card stat-card">
        <div class="stat-label">${esc(label)}</div>
        <div class="stat-value">${esc(String(val))}<span class="stat-sub">${esc(sub)}</span></div>
      </div>`));
  }
}

function stageChip(r, agentKey, label) {
  const a = AGENT_META[agentKey];
  if (!r) return `<div class="grow" style="text-align:center; padding:6px 2px; border:1px dashed var(--border); border-radius:6px; font-size:10.5px; color:var(--text-faint)">${esc(label || a.label)}</div>`;
  const [bcls, blabel] = RUN_BADGE[r.status] || RUN_BADGE.queued;
  const dur = r.duration_ms ? `${(r.duration_ms / 1000).toFixed(1)}s` : (r.status === "running" ? "…" : "");
  const sel = selectedRun === r.id;
  return `<button data-run="${r.id}" class="stage-chip grow" style="padding:5px 2px; border-radius:6px; cursor:pointer; background:${r.status === "running" ? "var(--accent-soft)" : "transparent"}; border:1px solid ${sel ? "var(--accent)" : "var(--border)"}">
    <div class="${a.cls}" style="font-size:10.5px; font-weight:650">${r.status === "running" ? `<span class="dot dot-accent pulse" style="display:inline-block; width:5px; height:5px; margin-right:3px"></span>` : ""}${esc(label || a.label)}</div>
    <div style="font-size:9.5px; color:var(--text-faint)">${blabel}${dur ? " · " + dur : ""}</div>
  </button>`;
}

function renderRunGroups(runs, keyById, titleById, sessionById, scopeTitleById) {
  const box = $("#agent-tickets");
  setText("active-count", `${runs.filter((r) => r.status === "running").length} active`);

  const byTask = {};
  for (const r of runs) {
    const g = (byTask[r.task_id] ||= { stages: {}, latest: 0 });
    if (!g.stages[r.agent_type]) g.stages[r.agent_type] = r;
    g.latest = Math.max(g.latest, new Date(r.created_at || 0).getTime());
  }
  const groups = {};
  for (const tid of Object.keys(byTask)) {
    const sid = sessionById[tid] ?? null;
    const gk = sid != null ? `s${sid}` : `t${tid}`;
    const g = (groups[gk] ||= { sid, tids: [], latest: 0 });
    g.tids.push(tid);
    g.latest = Math.max(g.latest, byTask[tid].latest);
  }
  const order = Object.keys(groups).sort((a, b) => groups[b].latest - groups[a].latest);

  box.innerHTML = "";
  if (!order.length) {
    box.appendChild(node(`
      <div class="empty">${icon("bot", 24)}
        <div class="e-title">No agent runs yet</div>
        <div class="e-sub">Approve a ticket on the <a href="/board">Board</a> and hit Run — the live pipeline shows up here.</div>
      </div>`));
    return;
  }

  for (const gk of order) {
    const g = groups[gk];
    const isScope = g.sid != null;
    const tids = g.tids.sort((a, b) => (keyById[a] || "").localeCompare(keyById[b] || ""));
    const scopeStage = {};
    for (const tid of tids) for (const st of ["qa", "review", "pr"]) {
      if (!scopeStage[st] && byTask[tid].stages[st]) scopeStage[st] = byTask[tid].stages[st];
    }
    const anyRunning = tids.some((tid) => Object.values(byTask[tid].stages).some((r) => r.status === "running"));
    const heading = isScope ? (scopeTitleById[g.sid] || `Scope #${g.sid}`)
                            : (titleById[tids[0]] || keyById[tids[0]] || "task " + tids[0]);
    const devChips = tids.map((tid) => stageChip(byTask[tid].stages.dev, "dev", keyById[tid] || "T" + tid)).join("");
    const band = ["qa", "review", "pr"].map((st) => stageChip(scopeStage[st], st)).join(`<span class="faint" style="align-self:center">${icon("chevronRight", 12)}</span>`);

    const card = node(`
      <div class="card card-pad" style="${anyRunning ? "border-color:var(--accent)" : ""}">
        <div class="row" style="margin-bottom:9px">
          <span class="${isScope ? "" : "faint"}" style="color:${isScope ? "var(--accent-strong)" : ""}">${icon(isScope ? "workflow" : "fileCode", 14)}</span>
          <span class="truncate" style="font-size:12.5px; font-weight:600">${esc(heading)}</span>
          <span class="small faint">${tids.length} ticket${tids.length !== 1 ? "s" : ""}</span>
          <div class="grow"></div>
          ${isScope ? `<span class="badge badge-accent">one PR</span>`
                    : `<button class="icon-btn open-drawer" title="Ticket details">${icon("external", 13)}</button>`}
        </div>
        <div class="stat-label" style="margin-bottom:4px">Dev · per ticket</div>
        <div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(110px, 1fr)); gap:5px; margin-bottom:9px">${devChips}</div>
        <div class="stat-label" style="margin-bottom:4px">QA › Review › PR${isScope ? " · scope-level" : ""}</div>
        <div class="row" style="gap:4px; align-items:stretch">${band}</div>
      </div>`);
    card.querySelectorAll(".stage-chip").forEach((ch) => ch.addEventListener("click", () => selectRun(parseInt(ch.dataset.run), keyById)));
    card.querySelector(".open-drawer")?.addEventListener("click", () => openTicket(parseInt(tids[0]), null));
    box.appendChild(card);
  }
}

async function selectRun(runId, keyById) {
  selectedRun = runId;
  $$(".stage-chip").forEach((c) => { c.style.borderColor = parseInt(c.dataset.run) === runId ? "var(--accent)" : "var(--border)"; });
  await refreshLog(runId, keyById);
}

async function refreshLog(runId, keyById) {
  let d;
  try { d = await API.run(runId); } catch (e) { return; }
  const run = d.run;
  const a = AGENT_META[run.agent_type] || { label: run.agent_type, icon: "bot", cls: "" };
  $("#log-agent-icon").innerHTML = icon(a.icon, 15);
  setText("log-title", `${a.label} · ${(keyById && keyById[run.task_id]) || "task " + run.task_id}`);
  setText("log-model", run.model || "");
  const logBox = $("#agent-log");
  const stick = logBox.parentElement.scrollTop + logBox.parentElement.clientHeight >= logBox.parentElement.scrollHeight - 40;
  logBox.innerHTML = (d.logs || []).map((l) =>
    `<div class="ln-${esc(l.severity || "info")}">${md(l.message)}</div>`).join("") || "Waiting for output…";
  if (stick) logBox.parentElement.scrollTop = logBox.parentElement.scrollHeight;
  const [bcls, blabel] = RUN_BADGE[run.status] || RUN_BADGE.queued;
  $("#log-status").innerHTML = `<span class="badge ${bcls}">${blabel}</span>`;
  // Backends that don't report usage are flagged usage_unknown — show that
  // honestly instead of a fake $0.00 / 0 tok.
  const costStr = run.usage_unknown && !run.cost_usd ? "cost unknown" : fmtCost(run.cost_usd);
  const tokStr = run.usage_unknown && !(run.tokens_input + run.tokens_output)
    ? "tok unknown" : `${fmtTok(run.tokens_input + run.tokens_output)} tok`;
  setText("log-cost", `${costStr} · ${tokStr}`);
}

// --- Page: Knowledge -----------------------------------------------------------
async function initKnowledge() {
  $("#ingest-icon") && ($("#ingest-icon").innerHTML = icon("database", 16));
  $("#ingest-btn")?.addEventListener("click", async () => {
    const url = ($("#ingest-url").value || "").trim();
    if (!url.includes("/")) { toast("Enter a valid git URL", true); return; }
    $("#ingest-btn").disabled = true;
    try {
      await API.ingest(url);
      $("#ingest-url").value = "";
      toast("Ingesting repository — indexing runs in the background");
    } catch (e) { toast(e.message, true); }
    $("#ingest-btn").disabled = false;
    refreshRepoCards();
  });
  await refreshRepoCards();
  setInterval(refreshRepoCards, 3000);
  renderKnowledgeViews();
}

async function refreshRepoCards() {
  const box = $("#repo-list");
  let repos;
  try { repos = await API.repos(); } catch (e) { return; }
  STATE.repos = repos;
  box.innerHTML = "";
  if (!repos.length) {
    box.appendChild(node(`<div class="small faint">Nothing ingested yet — add a public git URL above.</div>`));
    return;
  }
  for (const r of repos) {
    const selected = STATE.repo && STATE.repo.id === r.id;
    const badge = r.kb_status === "ready" ? `<span class="badge badge-ok">ready</span>`
      : r.kb_status === "indexing" ? `<span class="badge badge-warn">indexing</span>`
      : r.kb_status === "failed" ? `<span class="badge badge-danger">failed</span>`
      : `<span class="badge">pending</span>`;
    let bodyRow = "";
    if (r.kb_status === "indexing") {
      bodyRow = `<div style="margin-top:9px"><div class="progress"><div style="width:${r.kb_progress || 0}%"></div></div>
        <div class="small faint truncate" style="margin-top:4px">${esc(r.kb_step || "")}</div></div>`;
    } else if (r.kb_status === "ready") {
      bodyRow = `<div class="small faint" style="margin-top:6px">${r.kb_doc_count} files · ${r.kb_knowledge_count || 0} graph nodes${r.last_indexed_at ? ` · indexed ${fmtWhen(r.last_indexed_at)}` : ""}</div>`;
    } else if (r.kb_status === "failed") {
      bodyRow = `<div class="small truncate" style="margin-top:6px; color:var(--danger)">${esc(r.kb_error || "Indexing failed")}</div>`;
    }
    const card = node(`
      <div class="card card-pad" style="${selected ? "border-color:var(--accent)" : ""}">
        <div class="row">
          <span class="truncate" style="font-weight:650; font-size:13px">${esc(r.org)}/${esc(r.name)}</span>
          <div class="grow"></div>
          ${badge}
        </div>
        <div class="small faint truncate mono" style="margin-top:2px">${esc(r.git_url)}</div>
        ${bodyRow}
        <div class="row" style="margin-top:11px">
          ${selected ? `<span class="row small" style="color:var(--accent-strong); font-weight:600">${icon("check", 13)} Active</span>`
                     : `<button class="btn btn-soft btn-sm" data-act="select">Set active</button>`}
          <div class="grow"></div>
          ${CAN_WRITE ? `<button class="btn btn-ghost btn-sm" data-act="reindex">${icon("refresh", 12)} Reindex</button>
          <button class="icon-btn" data-act="delete" title="Delete repository" style="color:var(--danger)">${icon("trash", 14)}</button>` : ""}
        </div>
      </div>`);
    card.querySelector('[data-act="select"]')?.addEventListener("click", () => {
      localStorage.setItem(REPO_KEY, String(r.id));
      location.reload();
    });
    card.querySelector('[data-act="reindex"]')?.addEventListener("click", async (e) => {
      e.target.closest("button").disabled = true;
      try { await API.reindex(r.id); toast(`Reindexing ${r.org}/${r.name}`); } catch (er) { toast(er.message, true); }
      refreshRepoCards();
    });
    card.querySelector('[data-act="delete"]')?.addEventListener("click", async () => {
      const ok = await confirmDialog({
        title: `Delete ${r.org}/${r.name}?`,
        text: "Removes the repository plus all of its scopes, tickets, runs, logs, and the local workspace clone. This cannot be undone.",
        confirmLabel: "Delete", danger: true,
      });
      if (!ok) return;
      try {
        await API.deleteRepo(r.id);
        toast(`Deleted ${r.org}/${r.name}`);
        if (String(r.id) === localStorage.getItem(REPO_KEY)) localStorage.removeItem(REPO_KEY);
      } catch (er) { toast(er.message, true); }
      refreshRepoCards();
    });
    box.appendChild(card);
  }
}

async function renderKnowledgeViews() {
  const host = $("#kn-views");
  if (!STATE.repo) {
    host.innerHTML = `<div class="empty">${icon("book", 24)}<div class="e-title">No repository selected</div>
      <div class="e-sub">Ingest a repository and its code graph — structure, entry points, routes, functional clusters — plus what past runs delivered, appears here.</div></div>`;
    return;
  }
  setText("kn-title", `Structured knowledge · ${STATE.repo.org}/${STATE.repo.name}`);
  let data;
  try { data = await API.knowledge(STATE.repo.id); }
  catch (e) { host.innerHTML = `<div class="small" style="color:var(--danger)">Could not load knowledge: ${esc(e.message)}</div>`; return; }

  const total = data.total || 0;
  setText("kn-count", total ? `${total} items` : "");
  if (!total) {
    host.innerHTML = `<div class="empty">${icon("layers", 24)}<div class="e-title">No knowledge yet</div>
      <div class="e-sub">Reindex this repository (Settings → Knowledge base) to build its code graph.</div></div>`;
    return;
  }
  const parts = [];
  for (const dom of data.order) {
    const docs = data.domains[dom] || [];
    if (!docs.length) continue;
    const label = (data.labels && data.labels[dom]) || dom;
    const items = docs.slice(0, 8).map((doc) => {
      const files = (doc.content && doc.content.files) ? doc.content.files.slice(0, 4).join(", ") : "";
      return `<div class="card card-pad">
        <div style="font-size:12.5px; font-weight:600">${esc(doc.name)}</div>
        ${doc.summary ? `<div class="small muted" style="margin-top:3px; line-height:1.5">${esc(doc.summary)}</div>` : ""}
        ${files ? `<div class="small faint mono truncate" style="margin-top:6px">${esc(files)}</div>` : ""}
      </div>`;
    }).join("");
    parts.push(`<div style="margin-bottom:20px">
      <div class="row" style="margin-bottom:8px">
        <span class="stat-label">${esc(label)}</span>
        <span class="small faint">${docs.length}</span>
      </div>
      <div style="display:grid; grid-template-columns:repeat(auto-fill, minmax(290px, 1fr)); gap:10px">${items}</div>
    </div>`);
  }
  host.innerHTML = parts.join("");
}

// --- Page: Costs -----------------------------------------------------------------
async function initCosts() {
  const reload = async () => {
    let data;
    try { data = await API.costs(STATE.repo ? STATE.repo.id : undefined); } catch (e) { return; }
    renderCosts(data, reload);
  };
  await reload();
  setInterval(reload, 5000);
}

function renderCosts(data, reload) {
  const totals = $("#cost-totals");
  totals.innerHTML = "";
  for (const [l, v, s] of [
    ["Total cost", fmtCostU(data.totals.cost, data.totals.cost_unknown), "usd"],
    ["Tokens in", fmtTok(data.totals.tokens_in), "input"],
    ["Tokens out", fmtTok(data.totals.tokens_out), "output"],
    ["Tickets", data.totals.tickets, "tracked"],
  ]) {
    totals.appendChild(node(`
      <div class="card stat-card">
        <div class="stat-label">${esc(l)}</div>
        <div class="stat-value">${esc(String(v))}<span class="stat-sub">${esc(s)}</span></div>
      </div>`));
  }
  const box = $("#cost-scopes");
  box.innerHTML = "";
  if (data.totals.cost_unknown) {
    box.appendChild(node(`<div class="small faint" style="margin-bottom:8px">
      * some backends (Codex, Cursor) report tokens but not a dollar cost, so
      "n/a" or a starred figure means the cost wasn't reported — not that it was free.</div>`));
  }
  if (!data.scopes.length) {
    box.appendChild(node(`<div class="empty">${icon("coins", 24)}<div class="e-title">No spend yet</div>
      <div class="e-sub">Costs appear here as the PM scopes work and the pipeline runs — every token is accounted per ticket, per agent.</div></div>`));
    return;
  }
  const agents = data.agents;
  for (const s of data.scopes) {
    const rows = s.tickets.map((t) => {
      const cells = agents.map((a) => {
        const c = t.by_agent[a];
        return `<td class="num small ${c ? "" : "faint"}">${c ? fmtCostU(c.cost, c.cost_unknown) : "·"}</td>`;
      }).join("");
      return `<tr class="clickable" data-ticket="${t.id}">
        <td class="mono small" style="color:var(--accent-strong)">${esc(t.key)}</td>
        <td class="truncate" style="max-width:260px">${esc(t.title)}</td>
        ${cells}
        <td class="num" style="font-weight:600">${fmtCostU(t.cost, t.cost_unknown)}</td>
        <td class="num small faint">${fmtTok(t.tokens)}</td>
      </tr>`;
    }).join("");
    const head = `<tr><th>Ticket</th><th></th>
      ${agents.map((a) => `<th class="num ${AGENT_META[a] ? AGENT_META[a].cls : ""}">${AGENT_META[a] ? AGENT_META[a].label : a}</th>`).join("")}
      <th class="num">Total</th><th class="num">Tokens</th></tr>`;
    const card = node(`
      <div class="card">
        <div class="panel-head">
          <span class="panel-title truncate">${esc(s.title)}</span>
          <span class="small faint">${s.tickets.length} tickets</span>
          <div class="grow"></div>
          <span class="mono" style="font-weight:650; color:var(--accent-strong)">${fmtCost(s.cost)}</span>
          <span class="small faint mono">${fmtTok(s.tokens_in)} in · ${fmtTok(s.tokens_out)} out</span>
        </div>
        <div style="overflow-x:auto"><table class="table"><thead>${head}</thead><tbody>${rows}</tbody></table></div>
      </div>`);
    card.querySelectorAll("tr[data-ticket]").forEach((tr) =>
      tr.addEventListener("click", () => openTicket(parseInt(tr.dataset.ticket), reload)));
    box.appendChild(card);
  }
}

// --- Page: Settings -----------------------------------------------------------
const SETTINGS_STATE = { view: null, dirty: {}, tab: null };

async function initSettings() {
  try { SETTINGS_STATE.view = await API.settings(); }
  catch (e) {
    $("#settings-content").innerHTML = `<div class="small" style="color:var(--danger)">${esc(e.message)}</div>`;
    return;
  }
  const tabs = $("#settings-tabs");
  const groupTabs = SETTINGS_STATE.view.groups.map((g) => [g.id, g.label]);
  groupTabs.push(["access", "Access & users"]);
  tabs.innerHTML = "";
  for (const [id, label] of groupTabs) {
    const b = node(`<button class="tab" data-tab="${id}">${esc(label)}</button>`);
    b.addEventListener("click", () => showSettingsTab(id));
    tabs.appendChild(b);
  }
  $("#settings-save")?.addEventListener("click", saveSettings);
  showSettingsTab(location.hash === "#access" ? "access" : groupTabs[0][0]);

  // Nag about the bootstrap credentials.
  try {
    const me = await API.me();
    if (me.default_password) {
      toast("You're still on the generated admin password — change it under Access & users", true);
    }
  } catch (e) {}
}

function markDirty(name, value) {
  SETTINGS_STATE.dirty[name] = value;
  const save = $("#settings-save");
  save.hidden = Object.keys(SETTINGS_STATE.dirty).length === 0;
  refreshGates(); // dependent fields show/hide as their driver changes
}

// --- Conditional visibility ("show_if": "field=value|value2") -----------------
// Fields that only make sense in one mode are hidden in the others, live.
let GATED_ROWS = [];

function _fieldByName(name) {
  for (const g of (SETTINGS_STATE.view?.groups || [])) {
    for (const f of g.fields) if (f.name === name) return f;
  }
  return null;
}

function gateSatisfied(showIf) {
  if (!showIf) return true;
  const [drv, vals] = showIf.split("=");
  const f = _fieldByName(drv);
  const cur = _settingsPending(drv, f ? f.value : undefined);
  return vals.split("|").includes(String(cur));
}

function registerGate(el, f) {
  if (!f.show_if) return;
  GATED_ROWS.push({ el, show_if: f.show_if });
  el.hidden = !gateSatisfied(f.show_if);
}

function refreshGates() {
  for (const g of GATED_ROWS) g.el.hidden = !gateSatisfied(g.show_if);
}

async function saveSettings() {
  const btn = $("#settings-save");
  btn.disabled = true;
  try {
    SETTINGS_STATE.view = await API.saveSettings(SETTINGS_STATE.dirty);
    SETTINGS_STATE.dirty = {};
    btn.hidden = true;
    toast("Settings saved — applied live");
    showSettingsTab(SETTINGS_STATE.tab);
  } catch (e) {
    toast(e.message, true);
  }
  btn.disabled = false;
}

function _settingsPending(name, fallback) {
  return name in SETTINGS_STATE.dirty ? SETTINGS_STATE.dirty[name] : fallback;
}

// Option label/state for a provider id: agent-CLI backends that aren't installed
// (or have no headless mode) are shown but not selectable.
function providerOption(o, cur, provsById) {
  const p = provsById[o];
  const unavailable = p && p.backend && p.available === false;
  const label = unavailable ? `${o} — unavailable` : o;
  const title = unavailable ? p.unavailable_reason || "" : (p && p.version) || "";
  return `<option value="${esc(o)}" ${o === cur ? "selected" : ""}
    ${unavailable && o !== cur ? "disabled" : ""} title="${esc(title)}">${esc(label)}</option>`;
}

// Model control for a stage: a real dropdown of the provider's catalog (visible
// options — not a type-to-see datalist), with "Custom model…" swapping to free
// text. Providers with an empty catalog (custom endpoint) get the text input.
function buildModelControl(mf, prov, curModel) {
  const models = (prov && prov.models) || [];
  const textInput = () => {
    const inp = node(`<input class="input mono" type="text" value="${esc(String(curModel ?? ""))}"
        placeholder="model id" ${IS_ADMIN ? "" : "disabled"}/>`);
    inp.addEventListener("input", (e) => markDirty(mf.name, e.target.value));
    return inp;
  };
  if (!models.length) return textInput();
  const opts = [...models];
  if (curModel && !opts.includes(curModel)) opts.unshift(curModel);
  const placeholder = curModel ? "" : `<option value="" selected disabled>choose a model…</option>`;
  const sel = node(`<select class="select mono" ${IS_ADMIN ? "" : "disabled"}>${placeholder}${
    opts.map((m) => `<option value="${esc(m)}" ${m === curModel ? "selected" : ""}>${esc(m)}</option>`).join("")
  }<option value="__custom__">Custom model…</option></select>`);
  sel.addEventListener("change", (e) => {
    if (e.target.value === "__custom__") {
      const inp = textInput();
      sel.replaceWith(inp);
      inp.focus();
    } else {
      markDirty(mf.name, e.target.value);
    }
  });
  return sel;
}

// A stage row: provider <select> + model dropdown that follows the provider.
function buildStageRow(pf, mf, provsById) {
  const curProvider = _settingsPending(pf.name, pf.value);
  const curModel = _settingsPending(mf.name, mf.value);
  const row = node(`
    <div class="setting-row">
      <div class="s-info">
        <div class="s-label">${esc(pf.label)}</div>
        ${pf.help ? `<div class="s-help">${esc(pf.help)}</div>` : ""}
      </div>
      <div class="s-control s-control-stage"></div>
    </div>`);
  const ctl = row.querySelector(".s-control");
  let modelCtl = buildModelControl(mf, provsById[curProvider], curModel);
  const sel = node(`<select class="select" ${IS_ADMIN ? "" : "disabled"}>${pf.options.map((o) =>
      providerOption(o, curProvider, provsById)).join("")}</select>`);
  sel.addEventListener("change", (e) => {
    const val = e.target.value;
    markDirty(pf.name, val);
    const p = provsById[val];
    const next = (p && p.default_model) || "";
    if (next) markDirty(mf.name, next);
    const fresh = buildModelControl(mf, p, next);
    modelCtl.replaceWith(fresh);
    modelCtl = fresh;
  });
  ctl.appendChild(sel);
  ctl.appendChild(modelCtl);
  return row;
}

function buildSettingControl(f) {
  const pending = _settingsPending(f.name, f.value);
  let control;
  if (f.type === "bool") {
    control = node(`<label class="switch"><input type="checkbox" ${pending ? "checked" : ""} ${IS_ADMIN ? "" : "disabled"}/><span class="track"></span></label>`);
    control.querySelector("input").addEventListener("change", (e) => markDirty(f.name, e.target.checked));
  } else if (f.type === "enum" || f.type === "provider") {
    control = node(`<select class="select" ${IS_ADMIN ? "" : "disabled"}>${f.options.map((o) =>
      `<option value="${esc(o)}" ${o === pending ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`);
    control.addEventListener("change", (e) => markDirty(f.name, e.target.value));
  } else {
    const inputType = f.secret ? "password" : (f.type === "int" || f.type === "float") ? "number" : "text";
    const step = f.type === "float" ? `step="0.1"` : "";
    control = node(`<input class="input ${f.secret ? "mono" : ""}" type="${inputType}" ${step}
      value="${esc(String(pending ?? ""))}" ${f.secret && f.set ? `placeholder="configured"` : ""} ${IS_ADMIN ? "" : "disabled"}/>`);
    control.addEventListener("input", (e) => {
      let v = e.target.value;
      if (f.type === "int") v = parseInt(v || "0", 10);
      if (f.type === "float") v = parseFloat(v || "0");
      markDirty(f.name, v);
    });
  }
  return control;
}

function buildSettingRow(f) {
  const row = node(`
    <div class="setting-row">
      <div class="s-info">
        <div class="s-label">${esc(f.label)}${f.secret && f.set ? ` <span class="badge badge-ok" style="margin-left:6px">set</span>` : ""}</div>
        ${f.help ? `<div class="s-help">${esc(f.help)}</div>` : ""}
      </div>
      <div class="s-control"></div>
    </div>`);
  row.querySelector(".s-control").appendChild(buildSettingControl(f));
  registerGate(row, f);
  return row;
}

// Compact field for the provider cards: label above control, full width.
function buildCardField(f) {
  const wrap = node(`<div class="field prov-field">
    <label>${esc(f.label)}${f.secret && f.set ? ` <span class="badge badge-ok">set</span>` : ""}</label>
  </div>`);
  wrap.appendChild(buildSettingControl(f));
  if (f.help) wrap.appendChild(node(`<div class="small faint" style="margin-top:3px">${esc(f.help)}</div>`));
  registerGate(wrap, f);
  return wrap;
}

// Pull each keyed provider's current model ids live, so the dropdowns never
// offer a model that no longer exists (the alternative — a hardcoded catalog —
// goes stale). Updates the in-memory view's model lists and re-renders.
function buildModelRefreshRow(view) {
  const row = node(`<div class="row" style="margin:0 2px 14px; gap:10px; align-items:center">
    <button class="btn btn-sm" id="models-refresh">↻ Refresh model lists</button>
    <span class="small faint">Pulls current model ids live from every provider with a key set —
    so a dropdown never shows a model that has been retired.</span></div>`);
  row.querySelector("#models-refresh").addEventListener("click", async (e) => {
    const btn = e.currentTarget; btn.disabled = true; btn.textContent = "Refreshing…";
    const keyed = (view.providers || []).filter(
      (p) => (p.kind === "openai" || p.kind === "anthropic") && p.key_set);
    let updated = 0;
    for (const p of keyed) {
      try {
        const r = await API.providerModels(p.id);
        const tgt = (SETTINGS_STATE.view.providers || []).find((x) => x.id === p.id);
        if (tgt && r && r.models && r.models.length) { tgt.models = r.models; updated++; }
      } catch (err) { /* one provider failing shouldn't abort the rest */ }
    }
    toast(updated ? `Refreshed model lists for ${updated} provider(s)`
                  : "No providers with a key set to refresh", !updated);
    showSettingsTab("models");
  });
  return row;
}

function buildPresetRow(view) {
  const opts = (view.preset_providers || []).map((p) =>
    `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
  const card = node(`
    <div class="card preset-card">
      <span class="preset-label">Use one provider everywhere</span>
      <select class="select" id="preset-provider" ${IS_ADMIN ? "" : "disabled"} style="width:190px">${opts}</select>
      <button class="btn btn-sm" id="preset-apply" ${IS_ADMIN ? "" : "disabled"}>Apply to all stages</button>
      <span class="grow"></span>
      <span class="small faint">Sets every stage's provider + a recommended model.</span>
    </div>`);
  card.querySelector("#preset-apply").addEventListener("click", (e) =>
    applyPreset($("#preset-provider").value, e.currentTarget));
  return card;
}

async function applyPreset(providerId, btn) {
  if (!providerId) return;
  btn.disabled = true;
  try {
    SETTINGS_STATE.view = await API.applyProviderPreset(providerId);
    SETTINGS_STATE.dirty = {};
    $("#settings-save").hidden = true;
    toast(`Applied ${providerId} to all stages — live`);
    showSettingsTab("models");
  } catch (e) { toast(e.message, true); }
  btn.disabled = false;
}

// --- Connections tab: one card per provider ------------------------------------
// API providers show their key/URL; agentic CLIs show live install status with
// one-click Install / Re-check. A field appears on exactly ONE card (shared keys
// like the Anthropic key render once, other users of it point there).

function buildApiProviderCard(p, byName, claimed) {
  const badge = !p.key_field ? ""
    : p.key_set ? `<span class="badge badge-ok">key set</span>`
    : `<span class="badge">no key</span>`;
  const card = node(`<div class="card prov-card">
    <div class="row prov-head">
      <span class="prov-name">${esc(p.name)}</span><span class="grow"></span>${badge}
    </div>
    ${p.note ? `<div class="small faint prov-note">${esc(p.note)}</div>` : ""}</div>`);
  for (const fname of [p.base_url_field, p.key_field]) {
    const f = fname && byName[fname];
    if (!f || claimed.has(fname)) continue;
    claimed.add(fname);
    card.appendChild(buildCardField(f));
  }
  return card;
}

function buildCliProviderCard(p, byName, claimed) {
  const badge = p.available
    ? `<span class="badge badge-ok">installed</span>`
    : `<span class="badge">not installed</span>`;
  const detail = p.available ? (p.version || "") : (p.unavailable_reason || "");
  const installBtn = !p.available && p.installable && IS_ADMIN
    ? `<button class="btn btn-sm" data-install="${esc(p.backend)}">Install</button>` : "";
  const card = node(`<div class="card prov-card">
    <div class="row prov-head">
      <span class="prov-name">${esc(p.name)}</span><span class="grow"></span>${badge}${installBtn}
    </div>
    <div class="small faint mono prov-note">${esc(detail)}</div>
    ${!p.available && p.connect_hint
      ? `<div class="small faint prov-note">→ ${esc(p.connect_hint)}</div>` : ""}</div>`);
  // The CLI's own key (e.g. Cursor); shared keys just point at their card.
  const kf = p.key_field && byName[p.key_field];
  if (kf && !claimed.has(p.key_field)) {
    claimed.add(p.key_field);
    card.appendChild(buildCardField(kf));
  } else if (p.key_field) {
    card.appendChild(node(`<div class="small faint prov-note">Uses the
      ${esc((byName[p.key_field] || { label: p.key_field }).label)} from “API providers” above,
      or the tool's own login.</div>`));
  }
  // Binary path is an edge-case tweak — folded away so cards stay clean.
  const pf = p.path_field && byName[p.path_field];
  if (pf && !claimed.has(p.path_field)) {
    claimed.add(p.path_field);
    const det = node(`<details class="prov-adv"><summary class="small faint">Advanced</summary></details>`);
    det.appendChild(buildCardField(pf));
    card.appendChild(det);
  }
  return card;
}

function renderConnectionsTab(host, view, group) {
  const byName = Object.fromEntries(group.fields.map((f) => [f.name, f]));
  const claimed = new Set();
  const apis = (view.providers || []).filter((p) => !p.backend);
  const clis = (view.providers || []).filter((p) => p.backend);

  host.appendChild(node(`<div class="settings-section-head">Agentic coding CLIs</div>`));
  const cliHead = node(`<div class="row" style="margin:2px 2px 8px">
    <span class="small faint grow">Auto-detected on this host — any pipeline stage can use any
    installed tool. Install runs the tool's official installer.</span>
    <button class="btn btn-sm" id="backends-recheck">Re-check</button></div>`);
  host.appendChild(cliHead);
  const cliGrid = node(`<div class="prov-grid"></div>`);
  for (const p of clis) cliGrid.appendChild(buildCliProviderCard(p, byName, claimed));
  host.appendChild(cliGrid);
  host.appendChild(node(`<div class="small faint mono" id="backend-install-out" hidden
      style="white-space:pre-wrap; max-height:140px; overflow:auto; margin-top:8px"></div>`));

  host.appendChild(node(`<div class="settings-section-head">API providers</div>`));
  const apiGrid = node(`<div class="prov-grid"></div>`);
  for (const p of apis) apiGrid.appendChild(buildApiProviderCard(p, byName, claimed));
  host.appendChild(apiGrid);

  // Safety net: any provider-tab field no card claimed still gets rendered.
  const leftovers = group.fields.filter((f) => !claimed.has(f.name));
  if (leftovers.length) {
    const card = node(`<div class="card" style="margin-top:12px"></div>`);
    for (const f of leftovers) card.appendChild(buildSettingRow(f));
    host.appendChild(card);
  }

  host.querySelector("#backends-recheck").addEventListener("click", async (e) => {
    e.currentTarget.disabled = true;
    try {
      SETTINGS_STATE.view = await API.refreshBackends();
      showSettingsTab(SETTINGS_STATE.tab);
      toast("Backends re-checked");
    } catch (err) { toast(err.message, true); e.currentTarget.disabled = false; }
  });
  host.querySelectorAll("[data-install]").forEach((btn) => btn.addEventListener("click", async (e) => {
    const id = e.currentTarget.dataset.install;
    e.currentTarget.disabled = true;
    e.currentTarget.textContent = "Installing…";
    toast(`Installing ${id} — this can take a minute`);
    try {
      const v = await API.installBackend(id);
      SETTINGS_STATE.view = v;
      const r = v.install || {};
      showSettingsTab(SETTINGS_STATE.tab);
      toast(r.ok ? `${id} installed` : `${id} install failed — see output below`, !r.ok);
      if (!r.ok && r.output) {
        const out = $("#backend-install-out");
        if (out) { out.hidden = false; out.textContent = r.output; }
      }
    } catch (err) { toast(err.message, true); showSettingsTab(SETTINGS_STATE.tab); }
  }));
}

function showSettingsTab(id) {
  SETTINGS_STATE.tab = id;
  $$("#settings-tabs .tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === id));
  const host = $("#settings-content");
  if (id === "access") { renderAccessTab(host); return; }

  const view = SETTINGS_STATE.view;
  const group = view.groups.find((g) => g.id === id);
  const provsById = Object.fromEntries((view.providers || []).map((p) => [p.id, p]));
  GATED_ROWS = []; // visibility gates re-register on every render
  host.innerHTML = "";
  host.appendChild(node(`<p class="small faint" style="margin:0 2px 2px; line-height:1.5">${esc(group.help)}${IS_ADMIN ? "" : " — read-only (admin role required to edit)"}</p>`));

  if (id === "providers") { renderConnectionsTab(host, view, group); return; }
  if (id === "models") {
    host.appendChild(buildPresetRow(view));
    if (IS_ADMIN) host.appendChild(buildModelRefreshRow(view));
  }
  // The panel roster is its own resource (/api/jury), not a settings field —
  // it renders above the panel-wide knobs and loads itself.
  if (id === "jury") {
    const slot = node(`<div id="jury-roster"><div class="row faint small" style="margin:8px 2px">
      <div class="spinner"></div> Loading the panel…</div></div>`);
    host.appendChild(slot);
    loadJuryRoster(slot);
  }

  const byName = Object.fromEntries(group.fields.map((f) => [f.name, f]));
  const consumed = new Set();

  // Group fields by their section (in declared order) so each subsection gets its
  // own heading + card — turns a long flat list into scannable groups.
  const sections = [];
  for (const f of group.fields) {
    const name = f.section || "";
    let sec = sections.find((s) => s.name === name);
    if (!sec) { sec = { name, fields: [] }; sections.push(sec); }
    sec.fields.push(f);
  }

  for (const sec of sections) {
    if (sec.name) host.appendChild(node(`<div class="settings-section-head">${esc(sec.name)}</div>`));
    const card = node(`<div class="card"></div>`);
    for (const f of sec.fields) {
      if (consumed.has(f.name)) continue;
      if (f.type === "provider" && f.model_field && byName[f.model_field]) {
        consumed.add(f.model_field);
        card.appendChild(buildStageRow(f, byName[f.model_field], provsById));
      } else {
        card.appendChild(buildSettingRow(f));
      }
    }
    // Both knowledge engines get a live "does it actually run here?" probe
    // (checks the SAVED settings — save first, then test).
    if (id === "knowledge" && sec.name === "Code graph") card.appendChild(buildGraphTestRow());
    if (id === "knowledge" && sec.name === "Code search") card.appendChild(buildSearchTestRow());
    host.appendChild(card);
  }
}

// --- Settings → Jury: the panel roster ------------------------------------------
// Judges are rows in their own table, not settings fields, so this editor talks
// to /api/jury directly and saves each edit on change (no batched Save button —
// a half-applied panel is worse than an immediately-applied one).

async function loadJuryRoster(slot) {
  try { SETTINGS_STATE.jury = await API.jury(); }
  catch (e) {
    slot.innerHTML = `<div class="small" style="color:var(--danger)">${esc(e.message)}</div>`;
    return;
  }
  renderJuryRoster(slot);
}

async function juryAction(slot, fn) {
  slot.style.opacity = "0.55";
  try {
    SETTINGS_STATE.jury = await fn();
    renderJuryRoster(slot);
  } catch (e) { toast(e.message, true); }
  slot.style.opacity = "";
}

function judgeModelControl(judge, prov) {
  const models = (prov && prov.models) || [];
  const slot = SETTINGS_STATE.jurySlot;
  const text = () => {
    const inp = node(`<input class="input mono" type="text" value="${esc(judge.model || "")}"
      placeholder="${esc(judge.effective_model || "model id")}" ${IS_ADMIN ? "" : "disabled"}/>`);
    inp.addEventListener("change", (e) =>
      juryAction(slot, () => API.updateJudge(judge.id, { model: e.target.value.trim() })));
    return inp;
  };
  if (!models.length) return text();
  const opts = [...models];
  if (judge.model && !opts.includes(judge.model)) opts.unshift(judge.model);
  const sel = node(`<select class="select mono" ${IS_ADMIN ? "" : "disabled"}>
    <option value="" ${judge.model ? "" : "selected"}>default (${esc(judge.effective_model || "—")})</option>
    ${opts.map((m) => `<option value="${esc(m)}" ${m === judge.model ? "selected" : ""}>${esc(m)}</option>`).join("")}
    <option value="__custom__">Custom model…</option></select>`);
  sel.addEventListener("change", (e) => {
    if (e.target.value === "__custom__") { const i = text(); sel.replaceWith(i); i.focus(); return; }
    juryAction(slot, () => API.updateJudge(judge.id, { model: e.target.value }));
  });
  return sel;
}

function buildJudgeCard(judge, view, provsById, index, total) {
  const slot = SETTINGS_STATE.jurySlot;
  const patch = (body) => juryAction(slot, () => API.updateJudge(judge.id, body));
  const warn = !judge.runnable
    ? `<span class="badge" title="This provider has no key set / isn't installed, so this judge will abstain">unreachable</span>`
    : "";
  const card = node(`<div class="card judge-card ${judge.enabled ? "" : "judge-off"}">
    <div class="row judge-head">
      <label class="switch" title="Seat this judge on the panel">
        <input type="checkbox" ${judge.enabled ? "checked" : ""} ${IS_ADMIN ? "" : "disabled"}/>
        <span class="track"></span></label>
      <input class="input judge-name" value="${esc(judge.name)}" ${IS_ADMIN ? "" : "disabled"}/>
      ${warn}
      <button class="icon-btn" data-move="-1" title="Move up" ${index === 0 || !IS_ADMIN ? "disabled" : ""}></button>
      <button class="icon-btn" data-move="1" title="Move down" ${index === total - 1 || !IS_ADMIN ? "disabled" : ""}></button>
      <button class="icon-btn" data-drop title="Remove this judge" ${IS_ADMIN ? "" : "disabled"}></button>
    </div>
    <div class="judge-grid">
      <div class="field"><label>Perspective</label>
        <select class="select" data-persona ${IS_ADMIN ? "" : "disabled"}>${
          view.personas.map((p) => `<option value="${esc(p.id)}" ${p.id === judge.persona ? "selected" : ""}>${esc(p.name)}</option>`).join("")
        }</select></div>
      <div class="field"><label>Provider</label>
        <select class="select" data-provider ${IS_ADMIN ? "" : "disabled"}>${
          view.provider_options.map((o) => o === ""
            ? `<option value="" ${judge.inherits ? "selected" : ""}>inherit Review stage (${esc(judge.effective_provider)})</option>`
            : providerOption(o, judge.inherits ? null : judge.provider, provsById)).join("")
        }</select></div>
      <div class="field judge-model"><label>Model</label></div>
    </div>
    <div class="small faint judge-summary">${esc(judge.persona_summary)}</div>
  </div>`);

  card.querySelector(".judge-head .switch input")
    .addEventListener("change", (e) => patch({ enabled: e.target.checked }));
  card.querySelector(".judge-name")
    .addEventListener("change", (e) => patch({ name: e.target.value.trim() || judge.name }));
  card.querySelector("[data-persona]")
    .addEventListener("change", (e) => patch({ persona: e.target.value }));
  card.querySelector("[data-provider]").addEventListener("change", (e) =>
    // Switching provider clears the model: the old id almost never exists on the
    // new provider, and an invalid model is an abstention at review time.
    patch({ provider: e.target.value, model: "" }));
  card.querySelector(".judge-model").appendChild(
    judgeModelControl(judge, provsById[judge.inherits ? judge.effective_provider : judge.provider]));

  card.querySelectorAll("[data-move]").forEach((b) => {
    b.innerHTML = icon(b.dataset.move === "-1" ? "chevronUp" : "chevronDown", 14);
    b.addEventListener("click", () =>
      juryAction(slot, () => API.moveJudge(judge.id, +b.dataset.move)));
  });
  const drop = card.querySelector("[data-drop]");
  drop.innerHTML = icon("trash", 14);
  drop.addEventListener("click", async () => {
    if (!(await confirmDialog({
      title: `Remove ${judge.name}?`,
      text: "This perspective will no longer be reviewed on any future delivery.",
      confirmLabel: "Remove", danger: true,
    }))) return;
    juryAction(SETTINGS_STATE.jurySlot, () => API.deleteJudge(judge.id));
  });

  // A custom judge carries its own brief; a built-in one can be given extra
  // house rules appended to the shipped persona.
  const isCustom = judge.persona === "custom";
  const det = node(`<details class="prov-adv" ${isCustom ? "open" : ""}>
    <summary class="small faint">${isCustom ? "Brief — what this judge looks for" : "Extra instructions for this judge"}</summary>
    <textarea class="input mono" rows="4" data-focus ${IS_ADMIN ? "" : "disabled"}
      placeholder="${isCustom
        ? "e.g. Check every change against our API versioning policy: no field may be removed or renamed in a minor release."
        : "Optional — appended to the built-in brief for this perspective."}">${esc(judge.focus || "")}</textarea>
  </details>`);
  det.querySelector("[data-focus]")
    .addEventListener("change", (e) => patch({ focus: e.target.value }));
  card.appendChild(det);
  return card;
}

function renderJuryRoster(slot) {
  SETTINGS_STATE.jurySlot = slot;
  const view = SETTINGS_STATE.jury;
  const provsById = Object.fromEntries((SETTINGS_STATE.view.providers || []).map((p) => [p.id, p]));
  slot.innerHTML = "";
  slot.appendChild(node(`<div class="settings-section-head">The panel</div>`));

  const n = view.enabled_count;
  const distinct = new Set(view.judges.filter((j) => j.enabled).map((j) => j.effective_provider)).size;
  const bar = node(`<div class="card preset-card jury-bar">
    <span class="preset-label">${n} judge${n === 1 ? "" : "s"} seated · ${distinct} distinct model${distinct === 1 ? "" : "s"}</span>
    <span class="grow"></span>
    <button class="btn btn-sm" id="jury-spread" ${IS_ADMIN ? "" : "disabled"}>Spread across providers</button>
    <button class="btn btn-sm" id="jury-reset" ${IS_ADMIN ? "" : "disabled"}>Reset to defaults</button>
    <button class="btn btn-primary btn-sm" id="jury-add" ${IS_ADMIN ? "" : "disabled"}>Add judge</button>
  </div>`);
  slot.appendChild(bar);
  slot.appendChild(node(`<p class="small faint" style="margin:0 2px 10px; line-height:1.5">
    Every seated judge is one LLM call per review round, so the panel multiplies the review
    stage's cost by its size${distinct < n
      ? ` — and ${distinct === 1 ? "they all share one model, which is an ensemble in name only" : "some share a model"}.`
      : "."} Each judge is billed to its own run on the Costs page.</p>`));

  if (!view.judges.length) {
    slot.appendChild(node(`<div class="card small faint">No judges seated — deliveries will be
      recorded as UNREVIEWED. Add one, or reset to the defaults.</div>`));
  }
  view.judges.forEach((j, i) =>
    slot.appendChild(buildJudgeCard(j, view, provsById, i, view.judges.length)));

  bar.querySelector("#jury-spread").addEventListener("click", () =>
    juryAction(slot, async () => {
      const v = await API.spreadJudges();
      toast(v.changed ? `Re-modelled ${v.changed} judge(s)`
                      : "No configured providers to spread across — add API keys under Connections",
            !v.changed);
      return v;
    }));
  bar.querySelector("#jury-reset").addEventListener("click", async () => {
    if (!(await confirmDialog({
      title: "Reset the panel?", danger: true, confirmLabel: "Reset",
      text: "Your current judges, models and briefs are discarded and the default panel is re-seated.",
    }))) return;
    juryAction(slot, () => API.resetJury());
  });
  bar.querySelector("#jury-add").addEventListener("click", () => addJudgeDialog(slot));
}

async function addJudgeDialog(slot) {
  const view = SETTINGS_STATE.jury;
  const seated = new Set(view.judges.map((j) => j.persona));
  const opts = view.personas
    .map((p) => `<option value="${esc(p.id)}">${esc(p.name)}${
      seated.has(p.id) && p.id !== "custom" ? " (already seated)" : ""}</option>`).join("");
  const body = node(`<div>
    <div class="field" style="margin-bottom:10px"><label>Perspective</label>
      <select class="select" id="nj-persona">${opts}</select></div>
    <div class="field" style="margin-bottom:10px"><label>Name</label>
      <input class="input" id="nj-name" placeholder="shown on the run row and in the verdict"/></div>
    <div class="field" id="nj-focus-wrap" hidden><label>Brief — what should this judge look for?</label>
      <textarea class="input mono" rows="5" id="nj-focus"
        placeholder="Be specific about what is and isn't this judge's job — overlapping judges produce correlated reviews."></textarea></div>
  </div>`);
  const personaSel = body.querySelector("#nj-persona");
  const nameInput = body.querySelector("#nj-name");
  const sync = () => {
    const p = view.personas.find((x) => x.id === personaSel.value);
    body.querySelector("#nj-focus-wrap").hidden = personaSel.value !== "custom";
    nameInput.value = p && p.id !== "custom" ? p.name : "";
  };
  personaSel.addEventListener("change", sync);
  sync();

  if (!(await confirmDialog({ title: "Add a judge", bodyNode: body, confirmLabel: "Add" }))) return;
  juryAction(slot, () => API.addJudge({
    persona: personaSel.value,
    name: nameInput.value.trim() || "Judge",
    focus: body.querySelector("#nj-focus").value.trim(),
  }));
}

// A "does this engine actually run here?" row. Both knowledge engines have one:
// which engine is answering changes what the agents can find, and that is worth
// showing rather than leaving an operator to infer it from bad results.
function buildProbeRow({ id, label, help, probe, okText, failText }) {
  const row = node(`
    <div class="setting-row">
      <div class="s-info">
        <div class="s-label">${esc(label)}</div>
        <div class="s-help">${esc(help)}</div>
      </div>
      <div class="s-control">
        <span class="small faint mono" id="${id}-out" style="margin-right:8px"></span>
        <button class="btn btn-sm" id="${id}">Test</button>
      </div>
    </div>`);
  row.querySelector(`#${id}`).addEventListener("click", async (e) => {
    const out = row.querySelector(`#${id}-out`);
    e.currentTarget.disabled = true;
    out.textContent = "testing…";
    try {
      const r = await probe();
      out.textContent = (r.ok ? "✓ " : "✗ ") + (r.output || "").split("\n").join(" · ");
      toast(r.ok ? okText : failText, !r.ok);
    } catch (err) { out.textContent = "✗ " + err.message; toast(err.message, true); }
    e.currentTarget.disabled = false;
  });
  return row;
}

function buildGraphTestRow() {
  return buildProbeRow({
    id: "graph-test", label: "Test code graph",
    help: "Locates the code-graph binary and reads its version (save first). Confirms the "
        + "engine can run on this host; if it can't, the KB degrades to the built-in "
        + "symbol map + ripgrep.",
    probe: () => API.testGraph(),
    okText: "Code graph OK", failText: "Code graph unavailable",
  });
}

function buildSearchTestRow() {
  return buildProbeRow({
    id: "search-test", label: "Test code search",
    help: "Reports which lexical engine is answering (save first): ripgrep, or the git grep "
        + "fallback. The fallback works, but sees only tracked files and can't filter by "
        + "language — so localization is measurably coarser.",
    probe: () => API.testSearch(),
    okText: "Code search OK", failText: "Code search unavailable",
  });
}

async function renderAccessTab(host) {
  host.innerHTML = "";
  // Change own password.
  const pw = node(`
    <div class="card card-pad">
      <div class="row" style="margin-bottom:12px">${icon("lock", 15)}<span style="font-weight:650; font-size:13px">Change your password</span></div>
      <div style="display:grid; grid-template-columns:1fr 1fr 1fr auto; gap:10px; align-items:end">
        <div class="field"><label>Current</label><input class="input" type="password" id="pw-cur" autocomplete="current-password"/></div>
        <div class="field"><label>New</label><input class="input" type="password" id="pw-new" autocomplete="new-password"/></div>
        <div class="field"><label>Confirm</label><input class="input" type="password" id="pw-new2" autocomplete="new-password"/></div>
        <button class="btn btn-primary" id="pw-save">Update</button>
      </div>
    </div>`);
  pw.querySelector("#pw-save").addEventListener("click", async () => {
    const cur = $("#pw-cur").value, nw = $("#pw-new").value, nw2 = $("#pw-new2").value;
    if (!nw || nw !== nw2) { toast("New passwords don't match", true); return; }
    try { await API.changePassword(cur, nw); toast("Password updated"); ["#pw-cur", "#pw-new", "#pw-new2"].forEach((s) => $(s).value = ""); }
    catch (e) { toast(e.message, true); }
  });
  host.appendChild(pw);

  // Connect your own GitHub account, so PRs you open from the board are yours.
  const renderGh = (me) => {
    const u = (me && me.user) || {};
    const connected = u.github_connected;
    const gh = node(`
      <div class="card card-pad">
        <div class="row" style="margin-bottom:6px">${icon("pr", 15)}<span style="font-weight:650; font-size:13px">Your GitHub account</span>
          ${connected ? `<span class="badge badge-ok" style="margin-left:6px">Connected</span>` : ""}</div>
        <p class="small faint" style="margin:0 0 12px">${connected
          ? `Pull requests you open from the board are created as <b>@${esc(u.github_login || "")}</b>${u.github_name ? " (" + esc(u.github_name) + ")" : ""}.`
          : `Paste a GitHub <b>personal access token</b> (scope: <code>repo</code>) to open PRs from the board under your own account. Stored encrypted; revoke it anytime on GitHub.`}</p>
        ${connected
          ? `<button class="btn btn-ghost" id="gh-disconnect">Disconnect</button>`
          : `<div style="display:grid; grid-template-columns:1fr auto; gap:10px; align-items:end">
               <div class="field"><label>Personal access token</label><input class="input" type="password" id="gh-token" placeholder="ghp_… or github_pat_…" autocomplete="off"/></div>
               <button class="btn btn-primary" id="gh-connect">Connect</button>
             </div>`}
      </div>`);
    gh.querySelector("#gh-connect")?.addEventListener("click", async () => {
      const tok = ($("#gh-token")?.value || "").trim();
      if (!tok) { toast("Paste a token first", true); return; }
      const btn = gh.querySelector("#gh-connect"); btn.disabled = true; btn.textContent = "Verifying…";
      try { const r = await API.connectGithub(tok); toast(`Connected as @${r.user.github_login} ✓`); renderGh(r); }
      catch (e) { toast(e.message, true); btn.disabled = false; btn.textContent = "Connect"; }
    });
    gh.querySelector("#gh-disconnect")?.addEventListener("click", async () => {
      try { await API.disconnectGithub(); toast("GitHub disconnected"); renderGh(await API.me()); }
      catch (e) { toast(e.message, true); }
    });
    const prev = host.querySelector("[data-gh-card]");
    gh.setAttribute("data-gh-card", "1");
    if (prev) prev.replaceWith(gh); else host.insertBefore(gh, host.children[1] || null);
  };
  try { renderGh(await API.me()); } catch (e) {}

  if (!IS_ADMIN) {
    host.appendChild(node(`<p class="small faint" style="margin:4px 2px">User management requires the admin role. Roles: <b>admin</b> — settings + users · <b>member</b> — operates the pipeline · <b>viewer</b> — read-only.</p>`));
    return;
  }

  let users = [];
  try { users = await API.users(); } catch (e) { toast(e.message, true); }
  const rows = users.map((u) => `
    <tr data-user="${u.id}">
      <td><div class="row"><div class="avatar" style="width:22px; height:22px; font-size:10px">${esc(u.username[0])}</div>${esc(u.username)}</div></td>
      <td>
        <select class="select role-select" style="width:120px" ${u.username === document.body.dataset.username ? "disabled" : ""}>
          ${["admin", "member", "viewer"].map((r) => `<option ${r === u.role ? "selected" : ""}>${r}</option>`).join("")}
        </select>
      </td>
      <td class="small faint">${fmtWhen(u.created_at)}</td>
      <td class="num">
        ${u.username === document.body.dataset.username ? `<span class="small faint">you</span>`
          : `<button class="icon-btn del-user" title="Delete user" style="color:var(--danger)">${icon("trash", 14)}</button>`}
      </td>
    </tr>`).join("");

  const card = node(`
    <div class="card">
      <div class="panel-head">${icon("users", 15)}<span class="panel-title">Users</span>
        <div class="grow"></div>
        <span class="small faint">admin — settings &amp; users · member — runs the pipeline · viewer — read-only</span>
      </div>
      <table class="table">
        <thead><tr><th>User</th><th>Role</th><th>Created</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>
      <div class="panel-head" style="border-top:1px solid var(--border); border-bottom:none">
        <input class="input" id="nu-name" placeholder="username" style="width:150px"/>
        <input class="input" id="nu-pass" type="password" placeholder="password" style="width:150px"/>
        <select class="select" id="nu-role" style="width:110px">
          <option>member</option><option>viewer</option><option>admin</option>
        </select>
        <button class="btn btn-primary btn-sm" id="nu-add">${icon("plus", 13)} Add user</button>
      </div>
    </div>`);

  card.querySelectorAll("tr[data-user]").forEach((tr) => {
    const uid = parseInt(tr.dataset.user);
    tr.querySelector(".role-select")?.addEventListener("change", async (e) => {
      try { await API.updateUser(uid, { role: e.target.value }); toast("Role updated"); }
      catch (er) { toast(er.message, true); renderAccessTab(host); }
    });
    tr.querySelector(".del-user")?.addEventListener("click", async () => {
      const name = tr.querySelector("td").textContent.trim();
      if (!(await confirmDialog({ title: `Delete ${name}?`, text: "Their sessions are invalidated immediately.", confirmLabel: "Delete", danger: true }))) return;
      try { await API.deleteUser(uid); toast("User deleted"); } catch (er) { toast(er.message, true); }
      renderAccessTab(host);
    });
  });
  card.querySelector("#nu-add").addEventListener("click", async () => {
    const name = $("#nu-name").value.trim(), pass = $("#nu-pass").value, role = $("#nu-role").value;
    if (!name || !pass) { toast("Username and password required", true); return; }
    try { await API.createUser(name, pass, role); toast(`Added ${name} (${role})`); renderAccessTab(host); }
    catch (e) { toast(e.message, true); }
  });
  host.appendChild(card);
}

// --- Boot -----------------------------------------------------------------------
document.addEventListener("DOMContentLoaded", async () => {
  initShell();
  await loadRepos();
  initRepoSwitcher();
  hydrateMetrics();
  setInterval(hydrateMetrics, 5000);

  if (PAGE === "scope") initScope();
  else if (PAGE === "board") initBoard();
  else if (PAGE === "agents") initAgents();
  else if (PAGE === "knowledge") initKnowledge();
  else if (PAGE === "costs") initCosts();
  else if (PAGE === "settings") initSettings();

  $("#drawer-backdrop")?.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
});
