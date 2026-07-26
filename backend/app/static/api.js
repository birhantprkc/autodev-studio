// REST client for the AutoDev Studio backend. Auth is cookie-based; a 401
// anywhere bounces the browser to /login.
const API = {
  async _req(method, path, body) {
    const opts = { method, headers: {} };
    if (body !== undefined) { opts.headers["content-type"] = "application/json"; opts.body = JSON.stringify(body); }
    const r = await fetch(path, opts);
    if (r.status === 401 && location.pathname !== "/login") { location.href = "/login"; throw new Error("Signed out"); }
    if (!r.ok) {
      let detail = `${method} ${path} → ${r.status}`;
      try { const b = await r.json(); if (b.detail) detail = typeof b.detail === "string" ? b.detail : JSON.stringify(b.detail); } catch (e) {}
      throw new Error(detail);
    }
    return r.status === 204 ? null : r.json();
  },
  get(p) { return this._req("GET", p); },
  post(p, b) { return this._req("POST", p, b); },
  put(p, b) { return this._req("PUT", p, b); },
  patch(p, b) { return this._req("PATCH", p, b); },
  del(p) { return this._req("DELETE", p); },

  // auth
  me() { return this.get("/auth/me"); },
  logout() { return this.post("/auth/logout"); },
  changePassword(current_password, new_password) { return this.post("/auth/change-password", { current_password, new_password }); },
  connectGithub(token) { return this.post("/auth/github", { token }); },
  disconnectGithub() { return this.del("/auth/github"); },
  users() { return this.get("/auth/users"); },
  createUser(username, password, role) { return this.post("/auth/users", { username, password, role }); },
  updateUser(id, body) { return this.patch(`/auth/users/${id}`, body); },
  deleteUser(id) { return this.del(`/auth/users/${id}`); },

  // settings
  settings() { return this.get("/api/settings"); },
  saveSettings(values) { return this.put("/api/settings", { values }); },
  applyProviderPreset(provider) { return this.post("/api/settings/preset", { provider }); },
  refreshBackends() { return this.post("/api/settings/backends/refresh"); },
  installBackend(id) { return this.post(`/api/settings/backends/${id}/install`); },
  providerModels(id) { return this.get(`/api/settings/providers/${id}/models`); },
  testEmbeddings() { return this.post("/api/settings/embeddings/test"); },

  // overview / repos
  overview(repoId, sessionId) { const q = sessionId ? `?session_id=${sessionId}` : (repoId ? `?repo_id=${repoId}` : ""); return this.get("/overview" + q); },
  repos() { return this.get("/repos"); },
  ingest(url) { return this.post("/repos/ingest", { git_url: url }); },
  reindex(id) { return this.post(`/repos/${id}/reindex`); },
  deleteRepo(id) { return this.del(`/repos/${id}`); },
  knowledge(id) { return this.get(`/repos/${id}/knowledge`); },

  // scope sessions
  sessions(repoId, kind) { return this.get(`/sessions?repo_id=${repoId}` + (kind ? `&kind=${kind}` : "")); },
  createSession(repoId, kind, title) { return this.post("/sessions", { repo_id: repoId, kind, title }); },
  session(id) { return this.get(`/sessions/${id}`); },
  scopeTurn(id, content) { return this.post(`/sessions/${id}/scope-turn`, { content }); },
  createTasks(id) { return this.post(`/sessions/${id}/create-tasks`); },
  runScope(id) { return this.post(`/sessions/${id}/run-scope`); },

  // tasks / board
  tasks(repoId) { return this.get(`/tasks?repo_id=${repoId}`); },
  board(repoId) { return this.get("/tasks/board" + (repoId ? `?repo_id=${repoId}` : "")); },
  approve(id) { return this.post(`/tasks/${id}/approve`); },
  taskDetail(id) { return this.get(`/tasks/${id}/detail`); },
  review(id) { return this.get(`/tasks/${id}/review`); },
  merge(id) { return this.post(`/tasks/${id}/merge`); },
  createPr(id) { return this.post(`/tasks/${id}/create-pr`); },
  requestChanges(id, note) { return this.post(`/tasks/${id}/request-changes`, { note }); },

  // agent runs
  runs(params = {}) { const q = new URLSearchParams(params).toString(); return this.get("/agents/runs" + (q ? `?${q}` : "")); },
  run(id) { return this.get(`/agents/runs/${id}`); },
  stats(repoId) { return this.get("/agents/stats" + (repoId ? `?repo_id=${repoId}` : "")); },
  costs(repoId) { return this.get("/costs/data" + (repoId ? `?repo_id=${repoId}` : "")); },
};
