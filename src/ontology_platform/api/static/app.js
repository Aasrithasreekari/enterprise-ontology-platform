/* Enterprise Ontology Platform console.
 * Plain browser JavaScript, no dependencies. Every value received from the API is rendered with
 * textContent, never parsed as HTML. The API key lives in sessionStorage only and is sent solely in the
 * Authorization header; it never appears in URLs or console output. */
(() => {
  "use strict";

  const API = "/api/v1";
  const KEY_STORAGE = "op.console.apiKey";
  const SVG_NS = "http://www.w3.org/2000/svg";
  const ENTITY_TYPES = [
    "organization", "customer", "product", "application", "repository", "service", "api", "database",
    "cloud_resource", "kubernetes_cluster", "deployment", "environment", "documentation",
  ];

  // ------------------------------------------------------------------ small DOM helpers
  const $ = (id) => document.getElementById(id);

  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    applyProps(node, props);
    append(node, children);
    return node;
  }

  function svg(tag, attrs, ...children) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v !== undefined && v !== null && v !== false) node.setAttribute(k, String(v));
    }
    append(node, children);
    return node;
  }

  function applyProps(node, props) {
    if (!props) return;
    for (const [k, v] of Object.entries(props)) {
      if (v === undefined || v === null || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else if (k === "dataset") Object.assign(node.dataset, v);
      else if (k in node && !k.includes("-") && k !== "list") node[k] = v;
      else node.setAttribute(k, v === true ? "" : String(v));
    }
  }

  function append(node, children) {
    for (const c of children.flat(Infinity)) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
  }

  function clear(node) { node.replaceChildren(); return node; }

  function safeUrl(url) {
    if (typeof url !== "string") return null;
    try {
      const u = new URL(url, location.origin);
      return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
    } catch { return null; }
  }

  function extLink(url, label) {
    const href = safeUrl(url);
    if (!href) return el("span", { class: "mono" }, label || url || "");
    return el("a", { href, target: "_blank", rel: "noopener noreferrer" }, label || url);
  }

  function fmtMs(ms) {
    if (ms === null || ms === undefined || Number.isNaN(Number(ms))) return "—";
    const n = Number(ms);
    if (n < 1000) return `${n < 10 ? n.toFixed(1) : Math.round(n)} ms`;
    return `${(n / 1000).toFixed(n < 10000 ? 2 : 1)} s`;
  }

  function parseTime(value) {
    if (!value) return null;
    // Timestamps without a zone are UTC on the server side.
    const s = /[zZ]|[+-]\d\d:?\d\d$/.test(value) ? value : `${value}Z`;
    const t = Date.parse(s);
    return Number.isNaN(t) ? null : t;
  }

  function fmtDate(value) {
    const t = parseTime(value);
    return t === null ? "—" : new Date(t).toLocaleString();
  }

  function fmtClock(value) {
    const t = parseTime(value);
    if (t === null) return "—";
    const d = new Date(t);
    return `${d.toLocaleTimeString([], { hour12: false })}.${String(d.getMilliseconds()).padStart(3, "0")}`;
  }

  function humanize(s) { return String(s || "").replace(/_/g, " "); }

  const STATUS_TONE = {
    completed: "ok", succeeded: "ok", validated: "ok", confirmed: "ok", ok: "ok", active: "ok",
    completed_with_errors: "warn", partially_validated: "warn", inferred: "warn", skipped: "", warning: "warn",
    insufficient_evidence: "warn", stale: "warn",
    failed: "err", error: "err", unsupported: "err",
    running: "info", pending: "", info: "info", cancelled: "",
  };

  function badge(status, label) {
    const tone = STATUS_TONE[String(status || "").toLowerCase()] || "";
    return el("span", { class: `badge ${tone}` }, humanize(label || status || "unknown"));
  }

  function typeBadge(type) {
    const dot = el("span", { class: "type-dot", "aria-hidden": "true" });
    dot.style.background = typeColor(type);
    return el("span", { class: "type-badge" }, dot, humanize(type));
  }

  function typeColor(type) {
    return ENTITY_TYPES.includes(type) ? `var(--t-${type})` : "var(--edge)";
  }

  function table(headers, rows, opts = {}) {
    const thead = el("thead", null, el("tr", null, headers.map((h) => el("th", { scope: "col", class: h.num ? "num" : "" }, h.label || h))));
    const tbody = el("tbody", null, rows);
    return el("div", { class: "table-wrap" }, el("table", opts.caption ? { "aria-label": opts.caption } : null, thead, tbody));
  }

  // ------------------------------------------------------------------ status regions
  function showLoading(region, text) {
    clear(region).append(el("div", { class: "loading" }, el("span", { class: "spinner", "aria-hidden": "true" }), text));
  }

  function showNotice(region, kind, title, detail) {
    clear(region).append(el("div", { class: `notice ${kind}`, role: kind === "error" ? "alert" : null },
      el("div", null, el("strong", null, title), detail ? el("span", null, detail) : null)));
  }

  function showError(region, err, context = {}) {
    if (err instanceof ApiError) {
      if (err.status === 401) {
        setKeyStatus("err", "Key rejected");
        return showNotice(region, "error", "Authentication required.", " Enter a valid API key above and select Connect.");
      }
      if (err.status === 403) {
        return showNotice(region, "warn", context.forbidden || "Not permitted.", ` ${err.message}`);
      }
      if (err.status === 404) {
        return showNotice(region, "warn", context.notFound || "Not found.", ` ${err.message}`);
      }
      if (err.status === 422) return showNotice(region, "error", "The request was rejected.", ` ${err.message}`);
      if (err.status === 0) return showNotice(region, "error", "The platform could not be reached.", ` ${err.message}`);
      return showNotice(region, "error", `Request failed (${err.status}).`, ` ${err.message}`);
    }
    if (err instanceof MissingKeyError) {
      $("api-key").focus();
      return showNotice(region, "info", "Connect first.", " Enter your API key above and select Connect.");
    }
    return showNotice(region, "error", "Unexpected error.", ` ${err && err.message ? err.message : String(err)}`);
  }

  // ------------------------------------------------------------------ API client
  class ApiError extends Error {
    constructor(status, code, message) { super(message); this.status = status; this.code = code; }
  }
  class MissingKeyError extends Error {}

  const keyStore = {
    get() { try { return sessionStorage.getItem(KEY_STORAGE) || ""; } catch { return memoryKey; } },
    set(v) { memoryKey = v; try { sessionStorage.setItem(KEY_STORAGE, v); } catch { /* storage unavailable */ } },
    clear() { memoryKey = ""; try { sessionStorage.removeItem(KEY_STORAGE); } catch { /* storage unavailable */ } },
  };
  let memoryKey = "";

  async function api(path, { method = "GET", params, body, key } = {}) {
    const apiKey = key !== undefined ? key : keyStore.get();
    if (!apiKey) throw new MissingKeyError("missing api key");
    const url = new URL(API + path, location.origin);
    for (const [k, v] of Object.entries(params || {})) {
      if (v === undefined || v === null || v === "") continue;
      for (const item of Array.isArray(v) ? v : [v]) url.searchParams.append(k, String(item));
    }
    const headers = { Accept: "application/json", Authorization: `Bearer ${apiKey}` };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    let resp;
    try {
      resp = await fetch(url.pathname + url.search, {
        method, headers, body: body !== undefined ? JSON.stringify(body) : undefined,
        credentials: "omit", cache: "no-store", referrerPolicy: "no-referrer",
      });
    } catch (e) {
      throw new ApiError(0, "network", "Check that the API server is running.");
    }
    let data = null;
    const text = await resp.text();
    if (text) { try { data = JSON.parse(text); } catch { data = null; } }
    if (!resp.ok) {
      let message = resp.statusText || "request failed";
      if (data && typeof data.message === "string") message = data.message;
      else if (data && Array.isArray(data.detail)) {
        message = data.detail.map((d) => `${(d.loc || []).filter((x) => x !== "body" && x !== "query").join(".")}: ${d.msg}`).join("; ");
      } else if (data && typeof data.detail === "string") message = data.detail;
      throw new ApiError(resp.status, data && data.error, message);
    }
    return data;
  }

  // entity references containing "/" cannot travel in a path segment; prefer the id
  function entityPathRef(entity) {
    if (typeof entity === "string") return encodeURIComponent(entity);
    return encodeURIComponent(entity.id || entity.key || `${entity.type}:${entity.name}`);
  }

  // ------------------------------------------------------------------ API key & connection
  function setKeyStatus(kind, text) {
    const s = $("key-status");
    s.className = `key-status ${kind || ""}`;
    s.textContent = text;
  }

  let connected = false;

  async function verifyKey() {
    if (!keyStore.get()) { connected = false; setKeyStatus("", "Not connected"); return false; }
    setKeyStatus("", "Checking…");
    try {
      const stats = await api("/graph/stats");
      const entities = Object.values(stats.entities || {}).reduce((a, b) => a + Number(b || 0), 0);
      const rels = Object.values(stats.relationships || {}).reduce((a, b) => a + Number(b || 0), 0);
      connected = true;
      setKeyStatus("ok", `Connected · ${entities.toLocaleString()} entities · ${rels.toLocaleString()} relationships`);
      onConnected();
      return true;
    } catch (err) {
      connected = false;
      if (err instanceof ApiError && err.status === 401) setKeyStatus("err", "Key rejected");
      else if (err instanceof ApiError && err.status === 0) setKeyStatus("err", "Server unreachable");
      else setKeyStatus("err", `Error: ${err.message}`);
      return false;
    }
  }

  function initKeyForm() {
    const input = $("api-key");
    if (keyStore.get()) input.value = keyStore.get();
    $("key-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const v = input.value.trim();
      if (!v) { keyStore.clear(); setKeyStatus("err", "Enter an API key"); input.focus(); return; }
      if (v !== keyStore.get()) clearViews();
      keyStore.set(v);
      loaded.clear();
      verifyKey();
    });
    $("key-clear").addEventListener("click", () => {
      keyStore.clear();
      input.value = "";
      connected = false;
      loaded.clear();
      clearViews();
      setKeyStatus("", "Not connected");
      input.focus();
    });
  }

  // results fetched with one key must not stay on screen after switching to another
  function clearViews() {
    for (const id of ["ask-result", "search-results", "path-results", "deps-result", "deploy-result", "docs-result",
      "history-result", "connectors-result", "runs-result", "history-page"]) clear($(id));
    for (const region of document.querySelectorAll(".status-region")) clear(region);
    clear($("entity-details")).append(el("p", { class: "empty" }, "No entity selected."));
    resetGraph();
    renderGraph();
    history.offset = 0;
    runsFilter = null;
    schemaLoaded = false;
  }

  // ------------------------------------------------------------------ tabs
  const loaded = new Set();
  const tabLoaders = {
    history: () => loadHistory(),
    ingestion: () => loadIngestion(),
  };

  function activateTab(name, { focus = false } = {}) {
    for (const tab of document.querySelectorAll('[role="tab"]')) {
      const on = tab.dataset.tab === name;
      tab.setAttribute("aria-selected", String(on));
      tab.tabIndex = on ? 0 : -1;
      $(tab.getAttribute("aria-controls")).hidden = !on;
      if (on && focus) tab.focus();
    }
    try { sessionStorage.setItem("op.console.tab", name); } catch { /* ignore */ }
    if (connected && tabLoaders[name] && !loaded.has(name)) { loaded.add(name); tabLoaders[name](); }
    if (name === "explorer") renderGraph();
  }

  function initTabs() {
    const tabs = [...document.querySelectorAll('[role="tab"]')];
    tabs.forEach((tab, i) => {
      tab.addEventListener("click", () => activateTab(tab.dataset.tab));
      tab.addEventListener("keydown", (e) => {
        let j = null;
        if (e.key === "ArrowRight") j = (i + 1) % tabs.length;
        else if (e.key === "ArrowLeft") j = (i - 1 + tabs.length) % tabs.length;
        else if (e.key === "Home") j = 0;
        else if (e.key === "End") j = tabs.length - 1;
        if (j !== null) { e.preventDefault(); activateTab(tabs[j].dataset.tab, { focus: true }); }
      });
    });
    let initial = "ask";
    try { initial = sessionStorage.getItem("op.console.tab") || "ask"; } catch { /* ignore */ }
    if (!tabs.some((t) => t.dataset.tab === initial)) initial = "ask";
    activateTab(initial);
  }

  function currentTab() {
    const t = document.querySelector('[role="tab"][aria-selected="true"]');
    return t ? t.dataset.tab : "ask";
  }

  function onConnected() {
    loadSchemaTypes();
    const t = currentTab();
    if (tabLoaders[t] && !loaded.has(t)) { loaded.add(t); tabLoaders[t](); }
  }

  // ================================================================== Ask
  function initAsk() {
    const q = $("question");
    const count = $("question-count");
    const update = () => { count.textContent = `${q.value.length} / 1000`; };
    q.addEventListener("input", update);
    q.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); $("ask-form").requestSubmit(); }
    });
    for (const chip of document.querySelectorAll("[data-example]")) {
      chip.addEventListener("click", () => { q.value = chip.textContent; update(); q.focus(); });
    }
    $("ask-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const question = q.value.trim();
      const status = $("ask-status");
      if (question.length < 3) { showNotice(status, "error", "Question too short.", " Enter at least 3 characters."); q.focus(); return; }
      const btn = $("ask-submit");
      btn.disabled = true;
      clear($("ask-result"));
      showLoading(status, "Planning and running the agent workflow…");
      try {
        const run = await api("/agent/query", { method: "POST", body: { question } });
        clear(status);
        renderRun(run);
        loaded.delete("history");
      } catch (err) {
        showError(status, err);
      } finally {
        btn.disabled = false;
      }
    });
    update();
  }

  async function openRun(runId) {
    activateTab("ask");
    const status = $("ask-status");
    clear($("ask-result"));
    showLoading(status, "Loading workflow run…");
    try {
      const run = await api(`/agent/runs/${encodeURIComponent(runId)}`);
      clear(status);
      $("question").value = run.query || "";
      $("question-count").textContent = `${(run.query || "").length} / 1000`;
      renderRun(run);
    } catch (err) {
      showError(status, err, { notFound: "Workflow run not found." });
    }
  }

  function renderRun(run) {
    const out = clear($("ask-result"));
    const answer = run.answer || {};
    const validation = run.validation || null;

    // header
    const header = el("div", { class: "card run-header" },
      el("div", { class: "run-question" }, run.query || ""),
      el("div", { class: "meta-line" },
        el("span", null, "Run ", badge(run.status)),
        validation ? el("span", null, "Validation ", badge(validation.status)) : null,
        el("span", null, `Duration ${fmtMs(run.duration_ms)}`),
        run.started_at ? el("span", null, fmtDate(run.started_at)) : null,
        el("span", { class: "mono" }, `id ${run.id}`)),
      (run.intents && run.intents.length)
        ? el("div", { class: "meta-line" }, "Intents:", run.intents.map((i) => el("span", { class: "badge info" }, humanize(i))))
        : null,
      (run.linked_entities && run.linked_entities.length)
        ? el("div", { class: "meta-line" }, "Entities:",
          run.linked_entities.map((e) => el("button", {
            type: "button", class: "chip", title: `Open ${e.key || e.name} in Explorer`,
            onclick: () => openEntityInExplorer(e),
          }, `${humanize(e.type)} · ${e.name}`)))
        : null);
    out.append(header);

    // answer
    const answerCard = el("div", { class: "card stack" }, el("h2", { class: "card-title" }, "Answer"));
    if (answer.summary) answerCard.append(el("p", { class: "summary" }, answer.summary));
    else answerCard.append(el("p", { class: "empty" }, "The workflow did not produce an answer."));
    for (const section of answer.sections || []) {
      answerCard.append(el("div", { class: "answer-section" },
        el("h4", null, section.title),
        (section.items || []).length ? el("ul", { class: "plain-list" }, section.items.map((i) => el("li", null, i)))
          : el("p", { class: "muted small" }, "No items.")));
    }
    out.append(answerCard);

    // facts
    const facts = run.facts || [];
    const byStatus = { confirmed: [], inferred: [], unsupported: [] };
    for (const f of facts) (byStatus[f.status] || (byStatus[f.status] = [])).push(f);
    const factCol = (title, status, list, fallback) => el("div", { class: "card stack" },
      el("div", { class: "row between" }, el("h3", { class: "section-title" }, title), badge(status, String(list.length || fallback.length))),
      list.length ? el("div", null, list.map(renderFact))
        : fallback.length ? el("ul", { class: "plain-list" }, fallback.map((s) => el("li", null, s)))
          : el("p", { class: "empty" }, `No ${status} facts.`));
    const factsGrid = el("div", { class: "two-col" },
      factCol("Confirmed facts", "confirmed", byStatus.confirmed, answer.confirmed_facts || []),
      factCol("Inferred facts", "inferred", byStatus.inferred, answer.inferred_facts || []));
    out.append(factsGrid);
    if (byStatus.unsupported.length) out.append(factCol("Unsupported facts (excluded from the answer)", "unsupported", byStatus.unsupported, []));

    // missing / limitations / validation
    const listCard = (title, items, emptyText) => el("div", { class: "card stack" },
      el("h3", { class: "section-title" }, title),
      items && items.length ? el("ul", { class: "plain-list" }, items.map((i) => el("li", null, i))) : el("p", { class: "muted small" }, emptyText));
    const validationCard = el("div", { class: "card stack" }, el("h3", { class: "section-title" }, "Validation"));
    if (validation) {
      validationCard.append(
        el("div", { class: "counts" }, badge(validation.status),
          badge("confirmed", `${validation.confirmed} confirmed`), badge("inferred", `${validation.inferred} inferred`),
          badge("unsupported", `${validation.unsupported} unsupported`)),
        (validation.issues || []).length
          ? el("ul", { class: "plain-list" }, validation.issues.map((i) => el("li", null, badge(i.severity), " ", i.message)))
          : el("p", { class: "muted small" }, "No validation issues."));
    } else validationCard.append(el("p", { class: "muted small" }, "Validation did not run."));
    out.append(el("div", { class: "three-col" },
      validationCard,
      listCard("Missing information", answer.missing_information, "Nothing reported missing."),
      listCard("Limitations", answer.limitations, "No limitations reported.")));

    // sources
    const sources = answer.sources || [];
    out.append(el("div", { class: "card stack" },
      el("h3", { class: "section-title" }, `Sources (${sources.length})`),
      sources.length ? el("ul", { class: "source-list" }, sources.map(renderEvidence)) : el("p", { class: "muted small" }, "No sources cited.")));

    // timeline
    out.append(renderTimeline(run));

    // events
    const events = run.events || [];
    out.append(el("div", { class: "card stack" },
      el("h3", { class: "section-title" }, `Events (${events.length})`),
      events.length ? table(["Time", "Type", "Agent", "Step", "Message"], events.map((ev) => el("tr", null,
        el("td", { class: "mono" }, fmtClock(ev.at)), el("td", null, badge(eventTone(ev.type), ev.type)),
        el("td", null, humanize(ev.agent || "")), el("td", { class: "mono" }, ev.step_id || ""), el("td", null, ev.message))), { caption: "Workflow events" })
        : el("p", { class: "muted small" }, "No events recorded.")));
  }

  function eventTone(type) {
    const t = String(type || "");
    if (/fail|error/.test(t)) return "failed";
    if (/skip/.test(t)) return "skipped";
    if (/complete|finish|done/.test(t)) return "completed";
    if (/start|running|retry/.test(t)) return "running";
    return "";
  }

  function renderEvidence(ev) {
    const label = ev.source_url ? ev.source_url : ev.source_system;
    return el("li", null,
      el("span", { class: "badge" }, ev.source_system || "source"), " ",
      ev.source_url ? extLink(ev.source_url, label) : el("span", null, label || ""),
      ev.extractor ? el("span", { class: "muted small" }, ` · ${ev.extractor}`) : null,
      typeof ev.confidence === "number" ? el("span", { class: "muted small" }, ` · confidence ${ev.confidence.toFixed(2)}`) : null,
      ev.excerpt || ev.evidence ? el("div", { class: "muted small" }, ev.excerpt || ev.evidence) : null);
  }

  function renderFact(f) {
    const evidence = f.evidence || [];
    const path = f.path || [];
    const details = (evidence.length || path.length || (f.validation_notes || []).length)
      ? el("details", null,
        el("summary", null, `Evidence (${evidence.length})${path.length ? ` · path of ${path.length} edge${path.length > 1 ? "s" : ""}` : ""}`),
        path.length ? el("div", { class: "path-chain" }, renderChain(path)) : null,
        evidence.length ? el("ul", { class: "source-list" }, evidence.map(renderEvidence)) : null,
        (f.validation_notes || []).length ? el("ul", { class: "plain-list" }, f.validation_notes.map((n) => el("li", null, n))) : null)
      : null;
    return el("div", { class: `fact ${f.status || ""}` }, el("div", { class: "fact-statement" }, f.statement), details);
  }

  function renderChain(path) {
    const parts = [];
    path.forEach((edge, i) => {
      if (i === 0) parts.push(el("span", null, edge.source));
      parts.push(el("span", { class: "rel" }, `—${edge.type}→`));
      parts.push(el("span", null, edge.target));
    });
    return parts;
  }

  function renderTimeline(run) {
    const steps = run.plan || [];
    const card = el("div", { class: "card stack" }, el("h3", { class: "section-title" }, `Execution timeline (${steps.length} steps)`));
    if (!steps.length) { card.append(el("p", { class: "muted small" }, "No plan steps were recorded.")); return card; }

    // dependency stages: steps in the same stage have no dependency on each other and run in parallel
    const byId = new Map(steps.map((s) => [s.id, s]));
    const level = new Map();
    const levelOf = (s, seen = new Set()) => {
      if (level.has(s.id)) return level.get(s.id);
      if (seen.has(s.id)) return 0;
      seen.add(s.id);
      const deps = (s.depends_on || []).map((d) => byId.get(d)).filter(Boolean);
      const lv = deps.length ? 1 + Math.max(...deps.map((d) => levelOf(d, seen))) : 0;
      level.set(s.id, lv);
      return lv;
    };
    steps.forEach((s) => levelOf(s));

    // time axis: real start offsets when timestamps exist, otherwise durations only
    const runStart = parseTime(run.started_at);
    let span = Number(run.duration_ms) || 0;
    for (const s of steps) {
      const st = parseTime(s.started_at);
      if (runStart !== null && st !== null) span = Math.max(span, st - runStart + Number(s.duration_ms || 0));
      else span = Math.max(span, Number(s.duration_ms || 0));
    }
    span = span || 1;

    const stages = [...new Set([...level.values()])].sort((a, b) => a - b);
    for (const lv of stages) {
      const group = steps.filter((s) => level.get(s.id) === lv);
      const label = group.length > 1 ? `Stage ${lv + 1} · ${group.length} steps in parallel` : `Stage ${lv + 1} · sequential`;
      const stage = el("div", null, el("div", { class: "stage-label" }, label));
      for (const s of group) {
        const st = parseTime(s.started_at);
        const offset = runStart !== null && st !== null ? Math.max(0, st - runStart) : 0;
        const bar = el("div", { class: `bar ${s.status}`, role: "img", "aria-label": `${humanize(s.status)}, ${fmtMs(s.duration_ms)}` });
        bar.style.left = `${Math.min(100, (offset / span) * 100)}%`;
        bar.style.width = `${Math.max(0.5, Math.min(100, (Number(s.duration_ms || 0) / span) * 100))}%`;
        stage.append(el("div", { class: "step" },
          el("div", { class: "step-head" },
            el("div", { class: "row wrap" }, el("span", { class: "step-agent" }, humanize(s.agent)), badge(s.status)),
            el("span", { class: "step-action" }, `${s.id} · ${s.action}`),
            s.purpose ? el("span", { class: "muted small" }, s.purpose) : null),
          el("div", { class: "step-body" },
            el("div", { class: "row" }, el("div", { class: "bar-track grow" }, bar), el("span", { class: "muted small mono" }, fmtMs(s.duration_ms))),
            el("div", { class: "step-detail" },
              (s.depends_on || []).length ? `after ${s.depends_on.join(", ")}` : "no dependencies",
              (s.tools || []).length ? ` · tools: ${s.tools.join(", ")}` : "",
              s.attempts > 1 ? ` · ${s.attempts} attempts` : ""),
            s.summary ? el("div", { class: "step-detail" }, s.summary) : null,
            s.skip_reason ? el("div", { class: "step-detail" }, `Skipped: ${s.skip_reason}`) : null,
            s.error ? el("div", { class: "step-detail step-error" }, `Error: ${s.error}`) : null)));
      }
      card.append(stage);
    }
    return card;
  }

  // ================================================================== History
  const history = { limit: 20, offset: 0, total: 0 };

  async function loadHistory() {
    const status = $("history-status");
    const out = $("history-result");
    showLoading(status, "Loading workflow runs…");
    try {
      const data = await api("/agent/runs", { params: { limit: history.limit, offset: history.offset } });
      clear(status);
      history.total = Number(data.total || 0);
      const runs = data.runs || [];
      clear(out);
      if (!runs.length) {
        out.append(el("p", { class: "empty" }, history.offset ? "No more runs." : "No workflow runs yet. Ask a question to create one."));
      } else {
        out.append(table(
          ["Question", "Status", "Validation", "Intents", "Started", { label: "Duration", num: true }, "Principal", ""],
          runs.map((r) => {
            const row = el("tr", { class: "clickable", onclick: () => openRun(r.id) },
              el("td", null, r.query), el("td", null, badge(r.status)),
              el("td", null, r.validation_status ? badge(r.validation_status) : "—"),
              el("td", null, (r.intents || []).map(humanize).join(", ") || "—"),
              el("td", null, fmtDate(r.started_at)), el("td", { class: "num" }, fmtMs(r.duration_ms)),
              el("td", { class: "mono" }, r.principal || ""),
              el("td", null, el("button", {
                type: "button", class: "btn btn-sm", "aria-label": `Open run: ${r.query}`,
                onclick: (e) => { e.stopPropagation(); openRun(r.id); },
              }, "Open")));
            return row;
          }), { caption: "Workflow runs" }));
      }
      const from = runs.length ? history.offset + 1 : 0;
      $("history-page").textContent = history.total ? `${from}–${history.offset + runs.length} of ${history.total}` : "";
      $("history-prev").disabled = history.offset === 0;
      $("history-next").disabled = history.offset + runs.length >= history.total;
    } catch (err) {
      clear(out);
      showError(status, err);
    }
  }

  function initHistory() {
    $("history-refresh").addEventListener("click", () => { loaded.add("history"); loadHistory(); });
    $("history-prev").addEventListener("click", () => { history.offset = Math.max(0, history.offset - history.limit); loadHistory(); });
    $("history-next").addEventListener("click", () => { history.offset += history.limit; loadHistory(); });
  }

  // ================================================================== Explorer: search & details
  let schemaLoaded = false;

  async function loadSchemaTypes() {
    if (schemaLoaded) return;
    try {
      const schema = await api("/ontology/schema");
      const types = (schema.entity_types || []).map((t) => t.type);
      const sel = $("search-type");
      clear(sel).append(el("option", { value: "" }, "All types"));
      for (const t of types) sel.append(el("option", { value: t }, humanize(t)));
      schemaLoaded = true;
    } catch { /* the select keeps "All types"; errors surface on the actual search */ }
  }

  function initExplorer() {
    $("search-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const status = $("search-status");
      const list = clear($("search-results"));
      showLoading(status, "Searching…");
      try {
        const type = $("search-type").value;
        const data = await api("/entities", { params: { q: $("search-q").value.trim(), type: type || undefined, limit: 50 } });
        clear(status);
        const results = data.results || [];
        if (!results.length) { showNotice(status, "info", "No matching entities.", " Try another name, alias or type."); return; }
        for (const r of results) {
          const btn = el("button", { type: "button", onclick: () => {
            for (const b of list.querySelectorAll("button")) b.removeAttribute("aria-current");
            btn.setAttribute("aria-current", "true");
            focusEntity(r);
          } },
          el("span", { class: "result-name" }, r.name),
          el("span", { class: "result-meta" }, typeBadge(r.type), el("span", { class: "mono" }, r.key || "")),
          r.description ? el("span", { class: "muted small" }, r.description) : null);
          list.append(el("li", null, btn));
        }
      } catch (err) { showError(status, err); }
    });

    $("path-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const status = $("path-status");
      const out = clear($("path-results"));
      showLoading(status, "Finding paths…");
      try {
        const data = await api("/paths", { params: {
          source: $("path-source").value.trim(), target: $("path-target").value.trim(), max_depth: $("path-depth").value || 4,
        } });
        clear(status);
        if (!data.connected || !(data.paths || []).length) {
          showNotice(status, "info", "Not connected.", ` No path within ${$("path-depth").value} hops.`);
          return;
        }
        out.append(el("p", { class: "muted small" }, `${data.paths.length} path${data.paths.length > 1 ? "s" : ""} found — highlighted in the graph.`));
        data.paths.forEach((p, i) => {
          out.append(el("div", { class: "path-item" },
            el("div", { class: "row between" }, el("strong", { class: "small" }, `Path ${i + 1} · ${p.length} hop${p.length > 1 ? "s" : ""}`),
              el("button", { type: "button", class: "btn btn-sm", onclick: () => showPathsInGraph(data, [p]) }, "Highlight")),
            el("div", { class: "path-chain" }, renderChain(p))));
        });
        showPathsInGraph(data, data.paths);
      } catch (err) { showError(status, err, { notFound: "Entity not found." }); }
    });

    $("graph-reset").addEventListener("click", () => { resetGraph(); renderGraph(); });
    $("graph-clear-highlight").addEventListener("click", () => { graph.hlEdges.clear(); graph.hlNodes.clear(); renderGraph(); });
    buildLegend();
  }

  let detailsToken = 0;

  async function showDetails(node, { history: withHistory = false } = {}) {
    const token = ++detailsToken;
    const status = $("details-status");
    const out = $("entity-details");
    showLoading(status, `Loading ${node.name}…`);
    try {
      const ref = await resolveNode(node);
      const d = await api(`/entities/${entityPathRef(ref)}`, { params: { history: withHistory ? "true" : undefined } });
      if (token !== detailsToken) return;
      clear(status);
      renderDetails(d, node, withHistory);
    } catch (err) {
      if (token !== detailsToken) return;
      clear(out).append(el("p", { class: "empty" }, "No entity selected."));
      showError(status, err, { notFound: "Entity not found or not visible to your key." });
    }
  }

  function renderDetails(d, node, withHistory) {
    const e = d.entity;
    const out = clear($("entity-details"));
    const ref = e.key || e.id;
    const historyToggle = el("input", { type: "checkbox", id: "details-history", checked: withHistory,
      onchange: (ev) => showDetails(node, { history: ev.target.checked }) });
    out.append(el("div", { class: "stack" },
      el("div", { class: "row between wrap" },
        el("div", null, el("h2", { class: "card-title" }, e.name), el("div", { class: "meta-line" }, typeBadge(e.type), badge(e.status), el("span", { class: "mono" }, e.key || ""), e.version ? `version ${e.version}` : null)),
        el("label", { class: "row small nowrap", for: "details-history" }, historyToggle, "Version history")),
      e.description ? el("p", { class: "summary" }, e.description) : null,
      el("div", { class: "row wrap" },
        el("button", { type: "button", class: "btn btn-sm", onclick: () => { $("path-source").value = ref; $("path-target").focus(); } }, "Use as path source"),
        el("button", { type: "button", class: "btn btn-sm", onclick: () => { $("path-target").value = ref; $("path-source").focus(); } }, "Use as path target"),
        el("button", { type: "button", class: "btn btn-sm", onclick: () => runImpact(ref, "upstream") }, "Impact analysis"),
        el("button", { type: "button", class: "btn btn-sm", onclick: () => runImpact(ref, "downstream") }, "Dependencies"),
        e.type === "application" ? el("button", { type: "button", class: "btn btn-sm", onclick: () => runDeployments(ref) }, "Deployments") : null,
        el("button", { type: "button", class: "btn btn-sm", onclick: () => runDocs(e.name, ref) }, "Related documents")),
      el("dl", { class: "kv" },
        el("dt", null, "ID"), el("dd", { class: "mono" }, e.id),
        e.customer_scope ? [el("dt", null, "Customer scope"), el("dd", null, String(e.customer_scope))] : null,
        el("dt", null, "Updated"), el("dd", null, fmtDate(e.updated_at)),
        (d.aliases || []).length ? [el("dt", null, "Aliases"), el("dd", null, d.aliases.join(", "))] : null),
      Object.keys(e.properties || {}).length ? el("div", null, el("h3", { class: "section-title" }, "Properties"),
        el("dl", { class: "kv" }, Object.entries(e.properties).map(([k, v]) => [el("dt", null, humanize(k)),
          el("dd", null, typeof v === "object" && v !== null ? el("pre", { class: "json" }, JSON.stringify(v, null, 2)) : String(v))]))) : null,
      el("div", null, el("h3", { class: "section-title" }, `Relationships (${d.relationship_count ?? (d.relationships || []).length})`),
        (d.relationships || []).length ? table(["Direction", "Type", "Entity", { label: "Confidence", num: true }],
          d.relationships.map((r) => el("tr", null,
            el("td", null, r.direction === "out" ? "outgoing →" : "← incoming"),
            el("td", { class: "mono" }, r.type),
            el("td", null, el("button", { type: "button", class: "linklike", onclick: () => focusEntity(r.other, { keepGraph: true }) }, r.other.name), " ", typeBadge(r.other.type)),
            el("td", { class: "num" }, typeof r.confidence === "number" ? r.confidence.toFixed(2) : "—"))), { caption: "Relationships" })
          : el("p", { class: "muted small" }, "No relationships.")),
      (d.provenance || []).length ? el("div", null, el("h3", { class: "section-title" }, `Provenance (${d.provenance.length})`),
        el("ul", { class: "source-list" }, d.provenance.map((p) => el("li", null, [...renderEvidence(p).childNodes],p.observed_at ? el("span", { class: "muted small" }, ` · observed ${fmtDate(p.observed_at)}`) : null)))) : null,
      withHistory ? el("div", null, el("h3", { class: "section-title" }, `Version history (${(d.history || []).length})`),
        (d.history || []).length ? el("div", { class: "stack" }, d.history.map((h) => el("details", null,
          el("summary", null, h.version !== undefined ? `Version ${h.version}` : "Snapshot", h.valid_from || h.recorded_at || h.changed_at ? ` · ${fmtDate(h.valid_from || h.recorded_at || h.changed_at)}` : ""),
          el("pre", { class: "json" }, JSON.stringify(h, null, 2)))))
          : el("p", { class: "muted small" }, "No earlier versions.")) : null));
  }

  // ================================================================== Explorer: graph
  const graph = {
    nodes: new Map(), // ref ("type:name") -> {ref, id, key, type, name, expanded}
    edges: new Map(), // relationship id -> {id, from, to, type}
    root: null,
    selected: null,
    hidden: new Set(),
    hlEdges: new Set(),
    hlNodes: new Set(),
  };

  const refOf = (e) => `${e.type}:${e.name}`;

  function parseRef(ref) {
    const i = ref.indexOf(":");
    return i > 0 ? { type: ref.slice(0, i), name: ref.slice(i + 1) } : { type: "unknown", name: ref };
  }

  function upsertNode(entityOrRef) {
    const info = typeof entityOrRef === "string" ? parseRef(entityOrRef) : entityOrRef;
    const ref = typeof entityOrRef === "string" ? entityOrRef : refOf(info);
    let n = graph.nodes.get(ref);
    if (!n) { n = { ref, type: info.type, name: info.name, id: null, key: null, expanded: false }; graph.nodes.set(ref, n); }
    if (info.id) n.id = info.id;
    if (info.key) n.key = info.key;
    return n;
  }

  function resetGraph() {
    graph.nodes.clear(); graph.edges.clear();
    graph.root = null; graph.selected = null;
    graph.hlEdges.clear(); graph.hlNodes.clear();
  }

  // nodes discovered only through path edges carry "type:name"; resolve them to an id once
  async function resolveNode(node) {
    if (node.id || node.key) return node;
    const data = await api("/entities", { params: { q: node.name, type: node.type, limit: 10 } });
    const hit = (data.results || []).find((r) => r.name === node.name && r.type === node.type) || (data.results || [])[0];
    if (!hit) throw new ApiError(404, "not_found", `no entity matches '${node.ref}'`);
    node.id = hit.id; node.key = hit.key;
    return node;
  }

  function focusEntity(entity, { keepGraph = false } = {}) {
    if (!keepGraph) resetGraph();
    const n = upsertNode(entity);
    if (!keepGraph || !graph.root) graph.root = n.ref;
    selectNode(n.ref);
  }

  function openEntityInExplorer(entity) {
    activateTab("explorer");
    focusEntity(entity);
  }

  async function selectNode(ref) {
    const node = graph.nodes.get(ref);
    if (!node) return;
    graph.selected = ref;
    renderGraph();
    showDetails(node);
    if (!node.expanded) await expandNode(node);
  }

  async function expandNode(node) {
    const status = $("details-status");
    try {
      await resolveNode(node);
      const data = await api(`/entities/${entityPathRef(node)}/related`, { params: { depth: 1 } });
      const rootRef = data.root ? refOf(data.root) : node.ref;
      const mapRef = (r) => (r === rootRef ? node.ref : r);
      for (const item of data.related || []) {
        upsertNode(item.entity);
        for (const edge of item.path || []) {
          const from = mapRef(edge.source);
          const to = mapRef(edge.target);
          if (!graph.nodes.has(from)) upsertNode(from);
          if (!graph.nodes.has(to)) upsertNode(to);
          const id = edge.relationship_id || `${from}|${edge.type}|${to}`;
          graph.edges.set(id, { id, from, to, type: edge.type });
        }
      }
      node.expanded = true;
      renderGraph();
    } catch (err) {
      showError(status, err, { notFound: "Entity not found or not visible to your key." });
    }
  }

  function showPathsInGraph(data, paths) {
    resetGraph();
    const src = upsertNode(data.source);
    upsertNode(data.target);
    graph.root = src.ref;
    for (const path of paths) {
      for (const edge of path) {
        upsertNode(edge.source); upsertNode(edge.target);
        const id = edge.relationship_id || `${edge.source}|${edge.type}|${edge.target}`;
        graph.edges.set(id, { id, from: edge.source, to: edge.target, type: edge.type });
        graph.hlEdges.add(id); graph.hlNodes.add(edge.source); graph.hlNodes.add(edge.target);
      }
    }
    renderGraph();
    $("graph").scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function buildLegend() {
    const legend = $("graph-legend");
    for (const t of ENTITY_TYPES) {
      const cb = el("input", { type: "checkbox", checked: true, value: t, onchange: (e) => {
        if (e.target.checked) graph.hidden.delete(t); else graph.hidden.add(t);
        renderGraph();
      } });
      const dot = el("span", { class: "type-dot", "aria-hidden": "true" });
      dot.style.background = typeColor(t);
      legend.append(el("label", { dataset: { type: t } }, cb, dot, humanize(t)));
    }
  }

  function updateLegend() {
    const present = new Set([...graph.nodes.values()].map((n) => n.type));
    for (const label of $("graph-legend").querySelectorAll("label")) {
      label.hidden = present.size > 0 && !present.has(label.dataset.type);
    }
  }

  function layout(visible) {
    // radial layout: BFS rings around the root, children ordered by their parent's angle
    const pos = new Map();
    const adj = new Map([...visible.keys()].map((r) => [r, []]));
    for (const e of graph.edges.values()) {
      if (adj.has(e.from) && adj.has(e.to)) { adj.get(e.from).push(e.to); adj.get(e.to).push(e.from); }
    }
    const root = visible.has(graph.root) ? graph.root : [...visible.keys()][0];
    const levels = [[root]];
    const parent = new Map([[root, null]]);
    let frontier = [root];
    while (frontier.length) {
      const next = [];
      for (const r of frontier) {
        for (const nb of adj.get(r).slice().sort()) {
          if (!parent.has(nb)) { parent.set(nb, r); next.push(nb); }
        }
      }
      if (next.length) levels.push(next);
      frontier = next;
    }
    const orphans = [...visible.keys()].filter((r) => !parent.has(r));
    if (orphans.length) levels.push(orphans);

    pos.set(root, { x: 0, y: 0, angle: 0 });
    let prevR = 0;
    for (let k = 1; k < levels.length; k++) {
      const ring = levels[k].slice().sort((a, b) => {
        const pa = pos.get(parent.get(a))?.angle ?? 0;
        const pb = pos.get(parent.get(b))?.angle ?? 0;
        return pa - pb || visible.get(a).type.localeCompare(visible.get(b).type) || a.localeCompare(b);
      });
      const r = Math.max(prevR + 150, (ring.length * 44) / (2 * Math.PI), k * 170);
      ring.forEach((ref, i) => {
        const angle = (2 * Math.PI * i) / ring.length - Math.PI / 2 + (k % 2 ? 0 : Math.PI / ring.length);
        pos.set(ref, { x: r * Math.cos(angle), y: r * Math.sin(angle), angle: (angle + 2 * Math.PI) % (2 * Math.PI) });
      });
      prevR = r;
    }
    return pos;
  }

  function renderGraph() {
    const svgEl = $("graph");
    const empty = $("graph-empty");
    updateLegend();
    $("graph-reset").disabled = graph.nodes.size === 0;
    $("graph-clear-highlight").disabled = graph.hlEdges.size === 0;
    if (!graph.nodes.size) { svgEl.toggleAttribute("hidden", true); empty.hidden = false; clear(svgEl); return; }
    svgEl.toggleAttribute("hidden", false); empty.hidden = true;

    const visible = new Map([...graph.nodes].filter(([ref, n]) =>
      !graph.hidden.has(n.type) || ref === graph.root || graph.hlNodes.has(ref)));
    const pos = layout(visible);
    const highlighting = graph.hlEdges.size > 0;
    svgEl.classList.toggle("highlighting", highlighting);

    let minX = 0, maxX = 0, minY = 0, maxY = 0;
    for (const p of pos.values()) { minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x); minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y); }
    const padX = 110, padY = 50;
    svgEl.setAttribute("viewBox", `${minX - padX} ${minY - padY} ${maxX - minX + 2 * padX} ${maxY - minY + 2 * padY}`);
    svgEl.setAttribute("preserveAspectRatio", "xMidYMid meet");

    const defs = svg("defs", null,
      svg("marker", { id: "arrow", viewBox: "0 0 10 10", refX: 10, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" }, svg("path", { d: "M0,0 L10,5 L0,10 z" })),
      svg("marker", { id: "arrow-hl", viewBox: "0 0 10 10", refX: 10, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" }, svg("path", { d: "M0,0 L10,5 L0,10 z" })));

    // collapse parallel edges between the same pair into one line with a combined label
    const pairs = new Map();
    for (const e of graph.edges.values()) {
      if (!pos.has(e.from) || !pos.has(e.to) || e.from === e.to) continue;
      const k = [e.from, e.to].sort().join("\u0000");
      if (!pairs.has(k)) pairs.set(k, []);
      pairs.get(k).push(e);
    }
    const edgeLayer = svg("g", null);
    const labelLayer = svg("g", null);
    const radius = (ref) => (ref === graph.root ? 17 : 13);
    for (const list of pairs.values()) {
      const e0 = list[0];
      const a = pos.get(e0.from), b = pos.get(e0.to);
      const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1;
      const ux = dx / len, uy = dy / len;
      const x1 = a.x + ux * radius(e0.from), y1 = a.y + uy * radius(e0.from);
      const x2 = b.x - ux * (radius(e0.to) + 2), y2 = b.y - uy * (radius(e0.to) + 2);
      const hl = list.some((e) => graph.hlEdges.has(e.id));
      const bothWays = list.some((e) => e.from !== e0.from);
      const line = svg("line", { x1, y1, x2, y2, class: `edge${hl ? " hl" : ""}`,
        "marker-end": `url(#${hl ? "arrow-hl" : "arrow"})`, "marker-start": bothWays ? `url(#${hl ? "arrow-hl" : "arrow"})` : null });
      line.append(svg("title", null, list.map((e) => `${e.from} —${e.type}→ ${e.to}`).join("\n")));
      edgeLayer.append(line);
      const label = [...new Set(list.map((e) => humanize(e.type)))].join(" · ");
      labelLayer.append(svg("text", { x: (x1 + x2) / 2, y: (y1 + y2) / 2 - 3, "text-anchor": "middle", class: `edge-label${hl ? " hl" : ""}` }, label));
    }

    const nodeLayer = svg("g", null);
    for (const [ref, n] of visible) {
      const p = pos.get(ref);
      if (!p) continue;
      const r = radius(ref);
      const classes = ["node", ref === graph.root ? "root" : "", ref === graph.selected ? "selected" : "", graph.hlNodes.has(ref) ? "hl" : ""].join(" ").trim();
      const circle = svg("circle", { cx: p.x, cy: p.y, r });
      circle.style.fill = typeColor(n.type);
      const name = n.name.length > 26 ? `${n.name.slice(0, 25)}…` : n.name;
      const below = p.y >= 0 || ref === graph.root;
      const g = svg("g", {
        class: classes, tabindex: 0, role: "button",
        "aria-label": `${humanize(n.type)} ${n.name}${n.expanded ? "" : ", not expanded"}${ref === graph.selected ? ", selected" : ""}`,
      },
      svg("title", null, `${humanize(n.type)}: ${n.name}${n.key ? `\n${n.key}` : ""}\n${n.expanded ? "Select to view details" : "Select to view details and expand"}`),
      circle,
      n.expanded ? null : svg("text", { x: p.x, y: p.y + 4, "text-anchor": "middle", class: "expand-mark", "aria-hidden": "true" }, "+"),
      svg("text", { x: p.x, y: below ? p.y + r + 13 : p.y - r - 6, "text-anchor": "middle" }, name));
      g.addEventListener("click", () => selectNode(ref));
      g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectNode(ref); } });
      nodeLayer.append(g);
    }

    const focusedRef = document.activeElement && document.activeElement.closest && document.activeElement.closest("#graph .node")
      ? document.activeElement.getAttribute("aria-label") : null;
    clear(svgEl).append(defs, edgeLayer, labelLayer, nodeLayer);
    if (focusedRef) {
      const again = [...nodeLayer.children].find((g) => g.getAttribute("aria-label").split(",")[0] === focusedRef.split(",")[0]);
      if (again) again.focus();
    }
  }

  // ================================================================== Impact & deployments
  function runImpact(ref, direction) {
    activateTab("impact");
    $("deps-ref").value = ref;
    $("deps-direction").value = direction;
    $("deps-form").requestSubmit();
  }

  function runDeployments(ref) {
    activateTab("impact");
    $("deploy-app").value = ref;
    $("deploy-form").requestSubmit();
  }

  function entityButton(e) {
    return el("button", { type: "button", class: "linklike", title: `Open ${e.key || e.name} in Explorer`, onclick: () => openEntityInExplorer(e) }, e.name);
  }

  function initImpact() {
    $("deps-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const status = $("deps-status");
      const out = clear($("deps-result"));
      const ref = $("deps-ref").value.trim();
      const direction = $("deps-direction").value;
      showLoading(status, "Tracing dependencies…");
      try {
        const data = await api(`/dependencies/${encodeURIComponent(ref)}`, { params: { direction, depth: $("deps-depth").value || 4 } });
        clear(status);
        const items = data.items || [];
        out.append(el("p", { class: "muted small" }, data.root ? [entityButton(data.root), " ", typeBadge(data.root.type), ` · ${direction} · ${items.length} result${items.length === 1 ? "" : "s"}`] : ""));
        if (data.impact) {
          const imp = data.impact;
          const cell = (label, list) => el("div", { class: "impact-cell" }, el("div", { class: "n" }, String(list.length)), el("div", { class: "muted small" }, label),
            list.length ? el("div", { class: "small" }, list.map((x, i) => [i ? ", " : "", typeof x === "string" ? x : entityButton(x)])) : null);
          out.append(el("h3", { class: "section-title" }, "Impact summary"), el("div", { class: "impact-grid" },
            cell("Affected services", imp.affected_services || []),
            cell("Affected applications", imp.affected_applications || []),
            cell("Affected deployments", imp.affected_deployments || []),
            cell("Affected customers", imp.affected_customers || [])));
        }
        if (!items.length) { out.append(el("p", { class: "empty" }, direction === "upstream" ? "Nothing depends on this entity." : "This entity has no recorded dependencies.")); return; }
        out.append(table(["Entity", "Type", { label: "Hops", num: true }, "Path", "Evidence"], items.map((it) => {
          const ev = (it.path || []).flatMap((p) => p.evidence || []);
          const systems = [...new Set(ev.map((x) => x.source_system))];
          return el("tr", null, el("td", null, entityButton(it.entity)), el("td", null, typeBadge(it.entity.type)),
            el("td", { class: "num" }, String(it.hops ?? "")), el("td", null, el("div", { class: "path-chain" }, renderChain(it.path || []))),
            el("td", { class: "small" }, systems.join(", ") || "—"));
        }), { caption: "Dependencies" }));
      } catch (err) { showError(status, err, { notFound: "Entity not found." }); }
    });

    $("deploy-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const status = $("deploy-status");
      const out = clear($("deploy-result"));
      showLoading(status, "Loading deployments…");
      try {
        const data = await api("/deployments", { params: {
          application: $("deploy-app").value.trim(), environment: $("deploy-env").value || undefined,
          customer: $("deploy-customer").value.trim() || undefined,
        } });
        clear(status);
        const deps = data.deployments || [];
        out.append(el("p", { class: "muted small" }, data.application ? [entityButton(data.application), ` · ${deps.length} deployment${deps.length === 1 ? "" : "s"}`] : ""));
        if (!deps.length) { out.append(el("p", { class: "empty" }, "No deployments match these filters.")); return; }
        out.append(table(["Customer", "Environment", "Deployment", "Clusters", "Namespace", "Workloads", "Cloud resources", "Sources"], deps.map((d) => el("tr", null,
          el("td", null, d.customer || "—"),
          el("td", null, badge("info", d.environment_tier || "—")),
          el("td", null, d.deployment ? entityButton(d.deployment) : "—"),
          el("td", null, (d.clusters || []).map((c, i) => [i ? ", " : "", entityButton(c)])),
          el("td", { class: "mono" }, d.namespace || "—"),
          el("td", { class: "small" }, Object.entries(d.workloads || {}).map(([name, w]) => el("div", null,
            el("span", { class: "mono" }, name.split("/").pop()), ` ${w.kind || ""}${w.replicas !== undefined ? ` ×${w.replicas}` : ""}`,
            (w.images || []).length ? el("div", { class: "muted mono" }, w.images.join(", ")) : null))),
          el("td", null, (d.cloud_resources || []).length ? (d.cloud_resources || []).map((c, i) => [i ? ", " : "", c && c.name ? entityButton(c) : String(c)]) : "—"),
          el("td", { class: "small" }, (d.sources || []).join(", ") || "—"))), { caption: "Deployments" }));
      } catch (err) { showError(status, err, { notFound: "Application not found." }); }
    });
  }

  // ================================================================== Documents
  function runDocs(query, entityRef) {
    activateTab("docs");
    $("docs-q").value = query;
    $("docs-entity").value = entityRef || "";
    $("docs-form").requestSubmit();
  }

  function initDocs() {
    $("docs-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const status = $("docs-status");
      const out = clear($("docs-result"));
      showLoading(status, "Searching documentation…");
      try {
        const data = await api("/documents/search", { params: {
          q: $("docs-q").value.trim(), entity: $("docs-entity").value.trim() || undefined, limit: $("docs-limit").value || 10,
        } });
        clear(status);
        const results = data.results || [];
        if (!results.length) { showNotice(status, "info", "No matching passages.", " Try different keywords or remove the entity filter."); return; }
        out.append(el("div", { class: "card" }, results.map((r) => el("article", { class: "doc-result" },
          el("div", { class: "row between wrap" },
            el("div", { class: "doc-title" }, r.url ? extLink(r.url, r.title || r.url) : r.title),
            el("span", { class: "muted small" }, `score ${Number(r.score || 0).toFixed(2)}`)),
          r.url ? el("div", { class: "muted small mono" }, r.url) : null,
          el("p", { class: "doc-text" }, r.text || ""),
          r.document_entity_id ? el("button", { type: "button", class: "btn btn-sm", onclick: () => openEntityInExplorer({ id: r.document_entity_id, type: "documentation", name: r.title || "document" }) }, "Open in Explorer") : null))));
      } catch (err) { showError(status, err, { notFound: "Entity filter did not match an entity." }); }
    });
  }

  // ================================================================== Ingestion
  const ROLE_MESSAGE = "This view requires the ingestor or admin role.";
  let connectorNames = new Map();
  let runsFilter = null;

  async function loadIngestion() {
    await loadConnectors();
    await loadRuns();
  }

  async function loadConnectors() {
    const status = $("connectors-status");
    const out = clear($("connectors-result"));
    showLoading(status, "Loading connectors…");
    try {
      const data = await api("/connectors");
      clear(status);
      const list = data.connectors || [];
      connectorNames = new Map(list.map((c) => [c.id, c.name]));
      if (!list.length) { out.append(el("p", { class: "empty" }, "No connectors are registered for this tenant.")); return; }
      out.append(table(["Name", "Type", "Enabled", "Cursor", "Created", "Credentials", ""], list.map((c) => el("tr", null,
        el("td", null, el("strong", null, c.name), el("div", { class: "muted mono small" }, c.id)),
        el("td", { class: "mono" }, c.type),
        el("td", null, c.enabled ? badge("ok", "enabled") : badge("", "disabled")),
        el("td", { class: "mono small" }, c.cursor || "—"),
        el("td", null, fmtDate(c.created_at)),
        el("td", { class: "mono small" }, c.secret_ref || "none"),
        el("td", null, el("button", { type: "button", class: "btn btn-sm", onclick: () => { runsFilter = c.id; loadRuns(); } }, "View runs")))), { caption: "Connectors" }));
    } catch (err) {
      showError(status, err, { forbidden: ROLE_MESSAGE });
    }
  }

  async function loadRuns() {
    const status = $("runs-status");
    const out = clear($("runs-result"));
    $("runs-title").textContent = runsFilter ? `Ingestion runs · ${connectorNames.get(runsFilter) || runsFilter}` : "Ingestion runs";
    $("runs-all").hidden = !runsFilter;
    showLoading(status, "Loading ingestion runs…");
    try {
      const data = await api("/ingestion/runs", { params: { connector_id: runsFilter || undefined, limit: 100 } });
      clear(status);
      const runs = data.runs || [];
      if (!runs.length) { out.append(el("p", { class: "empty" }, "No ingestion runs yet.")); return; }
      out.append(table(["Connector", "Mode", "Status", "Started", "Finished", { label: "Records", num: true }, { label: "Failed", num: true },
        { label: "Entities +/~", num: true }, { label: "Relationships +", num: true }, "Triggered by", "Error"], runs.map((r) => {
        const s = r.stats || {};
        return el("tr", null,
          el("td", null, connectorNames.get(r.connector_id) || el("span", { class: "mono small" }, r.connector_id)),
          el("td", null, r.mode), el("td", null, badge(r.status)),
          el("td", null, fmtDate(r.started_at)), el("td", null, fmtDate(r.finished_at)),
          el("td", { class: "num" }, String(s.records_processed ?? "—")),
          el("td", { class: "num" }, String(s.records_failed ?? "—")),
          el("td", { class: "num" }, s.entities_created !== undefined ? `${s.entities_created} / ${s.entities_updated ?? 0}` : "—"),
          el("td", { class: "num" }, String(s.relationships_created ?? "—")),
          el("td", { class: "mono small" }, r.triggered_by || "—"),
          el("td", { class: "small step-error" }, r.error || ""));
      }), { caption: "Ingestion runs" }));
    } catch (err) {
      showError(status, err, { forbidden: ROLE_MESSAGE });
    }
  }

  function initIngestion() {
    $("ingestion-refresh").addEventListener("click", () => { loaded.add("ingestion"); loadIngestion(); });
    $("runs-all").addEventListener("click", () => { runsFilter = null; loadRuns(); });
  }

  // ------------------------------------------------------------------ boot
  document.addEventListener("DOMContentLoaded", () => {
    initKeyForm();
    initAsk();
    initExplorer();
    initImpact();
    initDocs();
    initHistory();
    initIngestion();
    initTabs();
    verifyKey();
  });
})();
