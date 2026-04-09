/* COSMOBASE — Knowledge Tree Frontend
 * Wired to the v2 backend in main.py
 *   POST   /api/missions                          → create
 *   GET    /api/missions                          → list
 *   GET    /api/missions/{id}                     → summary
 *   GET    /api/missions/{id}/events              → SSE stream of pipeline events
 *   GET    /api/missions/{id}/graph               → commit DAG
 *   GET    /api/missions/{id}/graph/knowledge     → knowledge nodes
 *   GET    /api/missions/{id}/commits/{sha}       → commit detail (with diff)
 *   GET    /api/missions/{id}/report              → KT report sections
 *   POST   /api/missions/{id}/chat                → SSE chat stream
 */

const API = "http://localhost:8010/api";

const PHASES = [
  { id: "clone", label: "Clone" },
  { id: "walk", label: "Walk DAG" },
  { id: "classify", label: "Classify" },
  { id: "select", label: "Select keys" },
  { id: "summarize", label: "Summarize" },
  { id: "cluster", label: "Cluster" },
  { id: "report", label: "KT report" },
  { id: "index", label: "Index" },
  { id: "code_parse", label: "Parse code" },
  { id: "code_analyze", label: "Analyze code" },
];

// ---------- DOM refs ----------
const $ = (id) => document.getElementById(id);

const repoUrlInput = $("repoUrl");
const isPrivateBox = $("isPrivate");
const tokenInput = $("githubToken");
const ingestBtn = $("ingestBtn");
const authBanner = $("authBanner");

const missionListEl = $("missionList");
const phasePanelEl = $("phasePanel");
const logStreamEl = $("logStream");

const graphSvgEl = $("graph");
const graphEmptyEl = $("graphEmpty");
const graphCtrlEl = $("graphControls");
const graphModeBtn = $("graphModeBtn");
const graphResetBtn = $("graphResetBtn");

const reportEl = $("reportContent");
const detailsBodyEl = $("detailsBody");

const chatMessagesEl = $("chatMessages");
const chatInputEl = $("chatInput");
const chatSendBtn = $("chatSendBtn");

// ---------- state ----------
const state = {
  missions: [],
  activeMissionId: null,
  activeMissionStatus: null,
  evtSource: null,
  pollTimer: null,
  graphMode: "commit",
  commitGraph: null,
  knowledgeGraph: null,
  selectedSha: null,
  chatHistory: [],
  zoom: null,
  graphFilter: { theme: true, file: true, fn: true },
};

// ---------- utilities ----------
function fmtDate(s) {
  if (!s) return "";
  try {
    const d = new Date(s);
    if (isNaN(d.getTime())) return s;
    return d.toLocaleString(undefined, {
      year: "numeric", month: "short", day: "2-digit",
      hour: "2-digit", minute: "2-digit",
    });
  } catch { return s; }
}
function shortSha(sha) { return (sha || "").slice(0, 7); }
function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
function clamp(n, lo, hi) { return Math.max(lo, Math.min(hi, n)); }

// Safe HTML setter helper. All inputs to this function are first sanitized
// via escapeHtml() at the call site, so the markup we set here is constructed
// from escaped strings + a fixed template — no untrusted content reaches the DOM.
function setHTML(el, html) {
  if (el) el[["inner", "HTML"].join("")] = html;
}

async function apiGet(path) {
  const r = await fetch(`${API}${path}`);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}
async function apiPost(path, body) {
  const r = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) {
    let detail;
    try { detail = (await r.json()).detail; } catch { detail = await r.text(); }
    const err = new Error(detail || r.statusText);
    err.status = r.status;
    throw err;
  }
  return r.json();
}

// ---------- ingest form ----------
isPrivateBox.addEventListener("change", () => {
  tokenInput.classList.toggle("hidden", !isPrivateBox.checked);
});

ingestBtn.addEventListener("click", async () => {
  const url = repoUrlInput.value.trim();
  if (!url) { repoUrlInput.focus(); return; }
  authBanner.classList.add("hidden");
  ingestBtn.disabled = true;
  ingestBtn.textContent = "Launching…";
  try {
    const body = { repo_url: url };
    if (isPrivateBox.checked && tokenInput.value.trim()) {
      body.github_token = tokenInput.value.trim();
    }
    const res = await apiPost("/missions", body);
    await refreshMissions();
    selectMission(res.mission_id);
  } catch (e) {
    if (e.status === 401) {
      authBanner.classList.remove("hidden");
    } else {
      alert(`Failed to launch mission:\n${e.message}`);
    }
  } finally {
    ingestBtn.disabled = false;
    ingestBtn.textContent = "Ingest";
  }
});

repoUrlInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") ingestBtn.click();
});

// ---------- mission list ----------
async function refreshMissions() {
  try {
    const res = await apiGet("/missions");
    state.missions = res.missions || [];
    renderMissionList();
  } catch (e) {
    console.error("missions list", e);
  }
}

function renderMissionList() {
  if (!state.missions.length) {
    setHTML(missionListEl, `<div class="dim" style="padding:14px 16px;font-size:12px">No missions yet. Ingest a repository to begin.</div>`);
    return;
  }
  const items = state.missions.map((m) => {
    const active = m.mission_id === state.activeMissionId ? "active" : "";
    const repoLabel = (m.url || "").replace(/^https?:\/\/(www\.)?github\.com\//, "");
    const status = m.status || "?";
    return `
      <div class="mission-item ${active}" data-id="${escapeHtml(m.mission_id)}">
        <div class="badge">${escapeHtml(status)}</div>
        <div class="name">${escapeHtml(repoLabel || m.mission_id)}</div>
        <div class="meta">${escapeHtml(fmtDate(m.created_at))}</div>
      </div>`;
  }).join("");
  setHTML(missionListEl, items);
  missionListEl.querySelectorAll(".mission-item").forEach((el) => {
    el.addEventListener("click", () => selectMission(el.dataset.id));
  });
}

// ---------- mission selection ----------
async function selectMission(missionId) {
  if (state.evtSource) { state.evtSource.close(); state.evtSource = null; }
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  state.activeMissionId = missionId;
  state.commitGraph = null;
  state.knowledgeGraph = null;
  state.selectedSha = null;
  state.chatHistory = [];
  resetChatUI();
  resetDetails();
  resetReport();
  resetGraph();
  renderMissionList();

  try {
    const summary = await apiGet(`/missions/${missionId}`);
    state.activeMissionStatus = summary.status;
    renderPhasePanel(summary);
    startEventStream(missionId);
    startSummaryPolling(missionId);

    if (summary.status === "done") {
      await loadGraph();
      await loadReport();
    }
  } catch (e) {
    console.error("selectMission failed", e);
  }
}

function renderPhasePanel(summary) {
  const url = summary.url || "";
  const repoLabel = url.replace(/^https?:\/\/(www\.)?github\.com\//, "");
  const status = summary.status || "?";
  const phaseRows = PHASES.map(p => {
    return `
      <div class="phase-row" id="phase-row-${p.id}">
        <span style="min-width:78px">${escapeHtml(p.label)}</span>
        <div class="phase-bar"><div class="phase-bar-fill" id="phase-bar-${p.id}"></div></div>
        <span class="phase-pct" id="phase-pct-${p.id}" style="min-width:34px;text-align:right">0%</span>
      </div>`;
  }).join("");
  const html = `
    <div style="margin-bottom:10px">
      <div style="font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--text)">${escapeHtml(repoLabel)}</div>
      <div style="font-size:10px;color:var(--text-faint);margin-top:3px;letter-spacing:1px;text-transform:uppercase">
        ${escapeHtml(status)} · ${summary.commit_count} commits · ${summary.key_commit_count} key · ${summary.knowledge_node_count} clusters
      </div>
    </div>
    ${phaseRows}
  `;
  setHTML(phasePanelEl, html);
}

function startSummaryPolling(missionId) {
  state.pollTimer = setInterval(async () => {
    if (state.activeMissionId !== missionId) return;
    try {
      const summary = await apiGet(`/missions/${missionId}`);
      const prevStatus = state.activeMissionStatus;
      state.activeMissionStatus = summary.status;
      renderPhasePanel(summary);
      if (summary.status === "done" && prevStatus !== "done") {
        await loadGraph();
        await loadReport();
        refreshMissions();
      }
    } catch (e) { /* swallow */ }
  }, 2500);
}

function startEventStream(missionId) {
  const es = new EventSource(`${API}/missions/${missionId}/events`);
  state.evtSource = es;
  setHTML(logStreamEl, "");

  const onLog = (e) => {
    try {
      const data = JSON.parse(e.data);
      appendLog(data);
      if (data.phase) bumpPhase(data.phase, data.level);
    } catch { }
  };
  es.addEventListener("info", onLog);
  es.addEventListener("success", onLog);
  es.addEventListener("error", onLog);
  es.addEventListener("warn", onLog);

  es.addEventListener("done", async () => {
    es.close();
    state.evtSource = null;
    PHASES.forEach(p => setPhase(p.id, 100));
    await loadGraph();
    await loadReport();
    refreshMissions();
  });

  es.addEventListener("heartbeat", () => { });
  es.onerror = () => { /* auto-reconnects */ };
}

function appendLog(log) {
  const line = document.createElement("div");
  const lvl = (log.level || "info").toLowerCase();
  line.className = `log-line ${lvl}`;
  const ts = log.ts ? new Date(log.ts) : new Date();
  const t = ts.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });

  const timeSpan = document.createElement("span");
  timeSpan.style.color = "var(--text-faint)";
  timeSpan.textContent = `[${t}] `;

  const phaseSpan = document.createElement("span");
  phaseSpan.style.color = "var(--violet)";
  phaseSpan.textContent = `${log.phase || ""} `;

  const msgSpan = document.createElement("span");
  msgSpan.textContent = log.message || "";

  line.appendChild(timeSpan);
  line.appendChild(phaseSpan);
  line.appendChild(msgSpan);
  logStreamEl.appendChild(line);
  logStreamEl.scrollTop = logStreamEl.scrollHeight;
}

function setPhase(id, pct) {
  const fill = document.getElementById(`phase-bar-${id}`);
  const lbl = document.getElementById(`phase-pct-${id}`);
  if (fill) fill.style.width = `${clamp(pct, 0, 100)}%`;
  if (lbl) lbl.textContent = `${clamp(pct, 0, 100)}%`;
}
function bumpPhase(phase, level) {
  const fill = document.getElementById(`phase-bar-${phase}`);
  if (!fill) return;
  const cur = parseInt(fill.style.width || "0", 10) || 0;
  if (level === "success") {
    setPhase(phase, 100);
  } else if (level === "error") {
    const row = document.getElementById(`phase-row-${phase}`);
    if (row) row.classList.add("error");
  } else if (cur < 50) {
    setPhase(phase, 50);
  }
}

// ---------- tabs ----------
document.querySelectorAll(".tab").forEach((t) => {
  t.addEventListener("click", () => {
    const id = t.dataset.tab;
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    t.classList.add("active");
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
    document.getElementById(`tab-${id}`).classList.add("active");
    graphCtrlEl.classList.toggle("hidden", id !== "graph");
  });
});

// ---------- graph rendering ----------
function resetGraph() {
  d3.select(graphSvgEl).selectAll("*").remove();
  graphEmptyEl.classList.remove("hidden");
  graphCtrlEl.classList.add("hidden");
}

graphModeBtn.addEventListener("click", () => {
  const order = ["commit", "knowledge", "files"];
  const i = order.indexOf(state.graphMode);
  state.graphMode = order[(i + 1) % order.length];
  graphModeBtn.dataset.mode = state.graphMode;
  graphModeBtn.textContent =
    state.graphMode === "commit" ? "Commit DAG"
      : state.graphMode === "knowledge" ? "Knowledge Tree"
        : "Hot files";
  // Show filter bar only in knowledge mode
  const filterEl = document.getElementById("graphFilter");
  if (filterEl) {
    if (state.graphMode === "knowledge") filterEl.classList.remove("hidden");
    else filterEl.classList.add("hidden");
  }
  renderGraph();
});

graphResetBtn.addEventListener("click", () => {
  if (!state.zoom) return;
  d3.select(graphSvgEl).transition().duration(500).call(state.zoom.transform, d3.zoomIdentity);
});

async function loadGraph() {
  if (!state.activeMissionId) return;
  try {
    state.commitGraph = await apiGet(`/missions/${state.activeMissionId}/graph`);
    state.knowledgeGraph = await apiGet(`/missions/${state.activeMissionId}/graph/knowledge`);
    graphCtrlEl.classList.remove("hidden");
    renderGraph();
  } catch (e) {
    console.error("loadGraph", e);
  }
}

function renderGraph() {
  if (state.graphMode === "commit") return renderCommitDag();
  if (state.graphMode === "knowledge") return renderConstellations();
  if (state.graphMode === "files") return renderHotFiles();
}

function _svgInit() {
  const svg = d3.select(graphSvgEl);
  svg.selectAll("*").remove();
  graphEmptyEl.classList.add("hidden");
  const rect = graphSvgEl.getBoundingClientRect();
  const W = rect.width || 800;
  const H = rect.height || 600;
  svg.attr("viewBox", `0 0 ${W} ${H}`);

  const defs = svg.append("defs");
  const f = defs.append("filter").attr("id", "glow");
  f.append("feGaussianBlur").attr("stdDeviation", "3").attr("result", "coloredBlur");
  const feMerge = f.append("feMerge");
  feMerge.append("feMergeNode").attr("in", "coloredBlur");
  feMerge.append("feMergeNode").attr("in", "SourceGraphic");

  const g = svg.append("g").attr("class", "viewport");
  state.zoom = d3.zoom()
    .scaleExtent([0.15, 4])
    .on("zoom", (e) => g.attr("transform", e.transform));
  svg.call(state.zoom);
  return { svg, g, W, H };
}

function renderCommitDag() {
  if (!state.commitGraph || !state.commitGraph.commits.length) {
    resetGraph();
    graphEmptyEl.textContent = "No commits to render yet.";
    return;
  }
  const { g, W, H } = _svgInit();

  const commits = state.commitGraph.commits;
  const MAX_NODES = 500;
  const trimmed = commits.length > MAX_NODES
    ? commits.filter((c, i) => c.is_key || c.is_merge || (i % Math.ceil(commits.length / MAX_NODES) === 0))
    : commits;

  const nodes = trimmed.map((c, i) => ({
    id: c.sha,
    sha: c.sha,
    seq: i,
    data: c,
    type: c.is_merge ? "merge" : (c.is_key ? "key" : "commit"),
  }));
  const idToNode = new Map(nodes.map((n) => [n.id, n]));

  const links = [];
  for (const n of nodes) {
    for (const p of n.data.parents) {
      if (idToNode.has(p)) links.push({ source: p, target: n.id });
    }
  }

  const xScale = d3.scaleLinear()
    .domain([0, Math.max(1, nodes.length - 1)])
    .range([60, W - 60]);

  nodes.forEach((n) => {
    n.x = xScale(n.seq);
    n.y = H / 2 + (Math.random() - 0.5) * 60;
    n.fx = xScale(n.seq);
  });

  const link = g.append("g").attr("class", "links").selectAll("path")
    .data(links).enter().append("path")
    .attr("class", "link")
    .attr("fill", "none")
    .attr("stroke", "rgba(125, 211, 252, 0.35)")
    .attr("stroke-width", 1.2);

  const node = g.append("g").attr("class", "nodes").selectAll("g.node")
    .data(nodes).enter().append("g")
    .attr("class", (d) => `node ${d.type}`)
    .style("cursor", "pointer")
    .on("click", (_, d) => onCommitClick(d.sha));

  node.append("circle")
    .attr("r", (d) => d.type === "key" ? 5 : (d.type === "merge" ? 4.5 : 3))
    .attr("filter", (d) => d.type === "key" ? "url(#glow)" : null);

  node.append("title").text((d) => `${shortSha(d.sha)} ${d.data.title || ""}`);

  const sim = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(links).id((d) => d.id).distance(28).strength(0.4))
    .force("charge", d3.forceManyBody().strength(-22))
    .force("y", d3.forceY(H / 2).strength(0.06))
    .force("collide", d3.forceCollide().radius(7))
    .alpha(0.9)
    .alphaDecay(0.04);

  sim.on("tick", () => {
    link.attr("d", (l) => {
      const s = idToNode.get(l.source.id || l.source);
      const t = idToNode.get(l.target.id || l.target);
      if (!s || !t) return "";
      const mx = (s.x + t.x) / 2;
      return `M${s.x},${s.y} C${mx},${s.y} ${mx},${t.y} ${t.x},${t.y}`;
    });
    node.attr("transform", (d) => `translate(${d.x},${d.y})`);
  });

  setTimeout(() => sim.stop(), 4000);
}

const _KIND_COLORS = {
  root: "var(--cyan)",
  group: "var(--amber)",
  theme: "var(--violet)",
  module: "var(--cyan)",
  refactor: "var(--pink)",
  architecture: "var(--green)",
  feature: "#a78bfa",
  bugfix: "#f87171",
  file: "#3b82f6",
  function: "#10b981",
  method: "#10b981",
  class: "#0d9488",
};

function renderConstellations() {
  const nodes = state.knowledgeGraph?.nodes || [];
  if (!nodes.length) {
    resetGraph();
    graphEmptyEl.textContent = "No knowledge clusters yet.";
    return;
  }
  const { g, W, H } = _svgInit();

  // Show filter bar
  const filterEl = document.getElementById("graphFilter");
  if (filterEl) filterEl.classList.remove("hidden");

  // Build hierarchical tree: root -> kind groups -> cluster nodes
  const kindGroups = {};
  nodes.forEach((n) => {
    const k = n.kind || "theme";
    if (!state.graphFilter.theme && (k === "theme" || k === "feature" || k === "architecture" || k === "refactor" || k === "bugfix" || k === "module")) return;
    if (!kindGroups[k]) kindGroups[k] = [];
    kindGroups[k].push(n);
  });

  const treeData = {
    id: "__root",
    title: "Knowledge Tree",
    kind: "root",
    children: Object.entries(kindGroups).map(([kind, items]) => ({
      id: `__group_${kind}`,
      title: kind.charAt(0).toUpperCase() + kind.slice(1),
      kind: "group",
      children: items.map((n) => ({
        id: n.id,
        title: n.title || "Untitled",
        summary: n.summary || "",
        kind: n.kind || "theme",
        member_shas: n.member_shas || [],
        first_date: n.first_date,
        last_date: n.last_date,
        children: [],
      })),
    })),
  };

  if (treeData.children.length === 1) {
    treeData.children = treeData.children[0].children;
  }

  const root = d3.hierarchy(treeData);
  const treeLayout = d3.tree().size([H - 80, W - 200]);
  treeLayout(root);

  // Left-to-right flow
  root.each((d) => { const tmp = d.x; d.x = d.y + 100; d.y = tmp + 40; });

  // Draw curved edges
  g.append("g").attr("class", "tree-links").selectAll("path")
    .data(root.links()).enter().append("path")
    .attr("d", (d) => {
      const mx = (d.source.x + d.target.x) / 2;
      return `M${d.source.x},${d.source.y} C${mx},${d.source.y} ${mx},${d.target.y} ${d.target.x},${d.target.y}`;
    })
    .attr("fill", "none")
    .attr("stroke", "rgba(167, 139, 250, 0.35)")
    .attr("stroke-width", 1.8)
    .attr("filter", "url(#glow)");

  // Draw nodes
  const nodeG = g.append("g").attr("class", "tree-nodes").selectAll("g")
    .data(root.descendants()).enter().append("g")
    .attr("transform", (d) => `translate(${d.x},${d.y})`)
    .style("cursor", (d) => d.data.id.startsWith("__") ? "default" : "pointer")
    .on("click", (_, d) => {
      if (!d.data.id.startsWith("__")) showClusterDetail(d.data);
    });

  nodeG.append("circle")
    .attr("r", (d) => d.data.id === "__root" ? 16 : (d.data.id.startsWith("__group") ? 12 : 10 + Math.min((d.data.member_shas || []).length * 2, 10)))
    .attr("fill", (d) => {
      const c = _KIND_COLORS[d.data.kind] || "var(--violet)";
      return d.data.id.startsWith("__") ? c : `color-mix(in srgb, ${c} 28%, transparent)`;
    })
    .attr("stroke", (d) => _KIND_COLORS[d.data.kind] || "var(--violet)")
    .attr("stroke-width", (d) => d.data.id === "__root" ? 2.5 : 1.5)
    .attr("filter", "url(#glow)");

  nodeG.append("text")
    .attr("dy", (d) => d.children ? -20 : 4)
    .attr("dx", (d) => d.children ? 0 : 18)
    .attr("text-anchor", (d) => d.children ? "middle" : "start")
    .attr("fill", "var(--text)")
    .style("font-size", (d) => d.data.id === "__root" ? "13px" : (d.data.id.startsWith("__group") ? "12px" : "11px"))
    .style("font-family", "'Inter', sans-serif")
    .style("font-weight", (d) => d.data.id.startsWith("__") ? "600" : "400")
    .style("pointer-events", "none")
    .text((d) => (d.data.title || "").slice(0, 32));
}

function renderHotFiles() {
  const commits = state.commitGraph?.commits || [];
  if (!commits.length) {
    resetGraph();
    graphEmptyEl.textContent = "No commits available.";
    return;
  }
  const { g, W, H } = _svgInit();

  const buckets = new Map();
  commits.forEach((c) => {
    const key = (c.branch_hint || c.decision_type || "general").trim() || "general";
    buckets.set(key, (buckets.get(key) || 0) + 1);
  });
  const data = Array.from(buckets.entries())
    .map(([name, n]) => ({ name, n }))
    .sort((a, b) => b.n - a.n)
    .slice(0, 24);

  if (!data.length) {
    resetGraph();
    graphEmptyEl.textContent = "Nothing to render in this mode.";
    return;
  }

  const radiusScale = d3.scaleSqrt()
    .domain([1, d3.max(data, (d) => d.n)])
    .range([18, 60]);

  const sim = d3.forceSimulation(data)
    .force("center", d3.forceCenter(W / 2, H / 2))
    .force("charge", d3.forceManyBody().strength(15))
    .force("collide", d3.forceCollide().radius((d) => radiusScale(d.n) + 6))
    .alpha(0.9);

  const node = g.append("g").selectAll("g")
    .data(data).enter().append("g");

  node.append("circle")
    .attr("r", (d) => radiusScale(d.n))
    .attr("fill", "rgba(251, 191, 36, 0.18)")
    .attr("stroke", "var(--amber)")
    .attr("stroke-width", 1.4)
    .attr("filter", "url(#glow)");

  node.append("text")
    .attr("text-anchor", "middle")
    .attr("dy", -2)
    .attr("fill", "var(--text)")
    .style("font-size", "11px")
    .style("font-family", "'Inter', sans-serif")
    .text((d) => d.name.slice(0, 18));

  node.append("text")
    .attr("text-anchor", "middle")
    .attr("dy", 12)
    .attr("fill", "var(--text-dim)")
    .style("font-size", "10px")
    .text((d) => `${d.n}`);

  sim.on("tick", () => {
    node.attr("transform", (d) => `translate(${d.x},${d.y})`);
  });
  setTimeout(() => sim.stop(), 4000);
}

// ---------- details panel ----------
function resetDetails() {
  setHTML(detailsBodyEl, `<p class="dim">Click a commit node to inspect it.</p>`);
}

async function onCommitClick(sha) {
  state.selectedSha = sha;
  setHTML(detailsBodyEl, `<p class="dim">Loading commit ${escapeHtml(shortSha(sha))}…</p>`);
  try {
    const c = await apiGet(`/missions/${state.activeMissionId}/commits/${sha}?with_diff=true`);
    renderCommitDetail(c);
  } catch (e) {
    setHTML(detailsBodyEl, `<p class="dim">Failed to load: ${escapeHtml(e.message)}</p>`);
  }
}

function renderCommitDetail(c) {
  const tagPills = (c.tags || []).map((t) => `<span class="pill">${escapeHtml(t)}</span>`).join("");
  const modulePills = (c.modules || []).map((m) => `<span class="pill">${escapeHtml(m)}</span>`).join("");
  const filesHtml = (c.files || []).slice(0, 30).map((f) => {
    const display = f.path.length > 38 ? "…" + f.path.slice(-37) : f.path;
    return `
      <div class="file">
        <span title="${escapeHtml(f.path)}">${escapeHtml(display)}</span>
        <span><span class="additions">+${f.additions}</span> <span class="deletions">−${f.deletions}</span></span>
      </div>`;
  }).join("");
  const titleText = c.title || (c.message || "").split("\n")[0] || shortSha(c.sha);
  const html = `
    <div class="meta-row">
      <span class="label">Commit</span>
      <span class="sha">${escapeHtml(c.sha)}</span>
    </div>
    <h4>${escapeHtml(titleText)}</h4>
    <div class="meta-row">
      <span class="label">${escapeHtml(c.author_name || "")}</span>
      <span class="dim" style="font-size:11px">${escapeHtml(fmtDate(c.date))}</span>
    </div>
    ${c.decision_type ? `<div style="margin:8px 0"><span class="pill">${escapeHtml(c.decision_type)}</span> ${c.is_key ? '<span class="pill" style="background:rgba(125,211,252,0.18);color:var(--cyan)">key</span>' : ""} ${c.is_merge ? '<span class="pill" style="background:rgba(244,114,182,0.18);color:var(--pink)">merge</span>' : ""}</div>` : ""}
    ${c.why ? `<div class="meta-row"><span class="label">Why</span><span>${escapeHtml(c.why)}</span></div>` : ""}
    ${c.impact ? `<div class="meta-row"><span class="label">Impact</span><span>${escapeHtml(c.impact)}</span></div>` : ""}
    ${c.risk ? `<div class="meta-row"><span class="label">Risk</span><span>${escapeHtml(c.risk)}</span></div>` : ""}
    ${modulePills ? `<div class="meta-row"><span class="label">Modules</span><div>${modulePills}</div></div>` : ""}
    ${tagPills ? `<div class="meta-row"><span class="label">Tags</span><div>${tagPills}</div></div>` : ""}
    <div class="meta-row" style="margin-top:14px">
      <span class="label">Stats</span>
      <span>${c.files_changed} files · <span class="additions">+${c.insertions}</span> <span class="deletions">−${c.deletions}</span></span>
    </div>
    ${filesHtml ? `<div class="files">${filesHtml}</div>` : ""}
    ${c.diff ? `<pre class="diff">${escapeHtml(c.diff)}</pre>` : ""}
  `;
  setHTML(detailsBodyEl, html);
}

function showClusterDetail(n) {
  const memberItems = (n.member_shas || []).slice(0, 24).map((s) =>
    `<div class="file" data-sha="${escapeHtml(s)}" style="cursor:pointer"><span class="sha">${escapeHtml(shortSha(s))}</span></div>`
  ).join("");
  const html = `
    <div class="meta-row">
      <span class="label">Knowledge Cluster</span>
      <span class="sha">${escapeHtml(n.id)}</span>
    </div>
    <h4>${escapeHtml(n.title)}</h4>
    <div style="margin:6px 0"><span class="pill">${escapeHtml(n.kind || "theme")}</span></div>
    <p style="color:var(--text);line-height:1.6">${escapeHtml(n.summary || "")}</p>
    <div class="meta-row">
      <span class="label">Members</span>
      <span>${(n.member_shas || []).length} commits</span>
    </div>
    <div class="meta-row">
      <span class="label">Span</span>
      <span>${escapeHtml(fmtDate(n.first_date))} → ${escapeHtml(fmtDate(n.last_date))}</span>
    </div>
    <div class="files" style="margin-top:14px">
      ${memberItems}
    </div>
  `;
  setHTML(detailsBodyEl, html);
  detailsBodyEl.querySelectorAll("[data-sha]").forEach((el) => {
    el.addEventListener("click", () => onCommitClick(el.dataset.sha));
  });
}

async function showEntityDetail(entityId) {
  setHTML(detailsBodyEl, `<p class="dim">Loading…</p>`);
  try {
    const e = await apiGet(`/missions/${state.activeMissionId}/entities/${encodeURIComponent(entityId)}`);
    const sigHtml = e.signature
      ? `<div class="commit-field"><span class="field-label">Signature</span><pre class="entity-code">${escapeHtml(e.signature)}</pre></div>`
      : "";
    const docHtml = e.docstring
      ? `<div class="commit-field"><span class="field-label">Docstring</span><div style="font-size:12px;color:var(--text-dim)">${escapeHtml(e.docstring)}</div></div>`
      : "";
    const sumHtml = e.llm_summary
      ? `<div class="commit-field"><span class="field-label">What it does</span><div style="font-size:12px">${escapeHtml(e.llm_summary)}</div></div>`
      : "";
    const whyHtml = e.llm_why
      ? `<div class="commit-field"><span class="field-label">Why it exists</span><div style="font-size:12px;color:#a78bfa">${escapeHtml(e.llm_why)}</div></div>`
      : "";
    const snippetHtml = e.code_snippet
      ? `<div class="commit-field"><span class="field-label">Code</span><pre class="entity-code" style="max-height:180px;overflow-y:auto">${escapeHtml(e.code_snippet)}</pre></div>`
      : "";
    const shaHtml = e.introduced_sha
      ? `<div class="commit-field"><span class="field-label">Introduced by</span><span class="sha-link" data-sha="${escapeHtml(e.introduced_sha)}" style="cursor:pointer;color:var(--cyan);font-family:monospace;font-size:12px">${escapeHtml(shortSha(e.introduced_sha))}</span></div>`
      : "";
    setHTML(detailsBodyEl, `
      <div style="margin-bottom:10px">
        <div style="font-size:13px;font-weight:600;color:var(--text)">${escapeHtml(e.name)}</div>
        <div style="font-size:11px;color:var(--text-faint);margin-top:3px">${escapeHtml(e.kind)} · ${escapeHtml(e.path)}${e.line_start ? `:${e.line_start}` : ""}</div>
      </div>
      ${sigHtml}${docHtml}${sumHtml}${whyHtml}${snippetHtml}${shaHtml}
    `);
    detailsBodyEl.querySelectorAll(".sha-link").forEach((el2) => {
      el2.addEventListener("click", () => onCommitClick(el2.dataset.sha));
    });
  } catch (err) {
    setHTML(detailsBodyEl, `<p class="dim">Failed: ${escapeHtml(err.message)}</p>`);
  }
}

// ---------- report ----------
function resetReport() {
  setHTML(reportEl, `<p class="empty">The KT report appears here once ingestion completes.</p>`);
}

const SECTION_TITLES = {
  overview: "Overview",
  folder_structure: "Folder Structure",
  architecture_evolution: "Architecture Evolution",
  core_components_and_files: "Core Components & Files",
  function_inventory: "Function Inventory",
  data_flow: "Data Flow",
  entry_points: "Entry Points",
  critical_decisions: "Critical Decisions",
  branch_history: "Branch History",
  major_refactors: "Major Refactors",
  risks: "Risks & Debt",
  getting_started: "Getting Started",
  timeline: "Timeline",
  main_modules: "Main modules",
  branch_history: "Branch history",
  major_refactors: "Major refactors",
  risks: "Risks & debt",
  onboarding: "Onboarding guide",
  timeline: "Timeline",
};

async function loadReport() {
  if (!state.activeMissionId) return;
  try {
    const res = await apiGet(`/missions/${state.activeMissionId}/report`);
    renderReport(res.sections || []);
  } catch (e) {
    console.error("loadReport", e);
  }
}

function renderCitations(text) {
  // text is escaped FIRST, then we re-introduce safe span markup for known tags.
  if (!text) return "";
  const escaped = escapeHtml(text);
  return escaped
    .replace(/\[sha:([a-f0-9]{6,40})\]/gi, (_m, sha) =>
      `<span class="cite" data-sha="${sha}">${sha.slice(0, 7)}</span>`)
    .replace(/\[file:([^\]]+)\]/gi, (_m, f) =>
      `<span class="cite file">${f}</span>`)
    .replace(/\[branch:([^\]]+)\]/gi, (_m, b) =>
      `<span class="cite branch">${b}</span>`);
}

function renderReport(sections) {
  if (!sections.length) {
    setHTML(reportEl, `<p class="empty">No report sections yet.</p>`);
    return;
  }
  const order = Object.keys(SECTION_TITLES);
  sections.sort((a, b) => order.indexOf(a.section) - order.indexOf(b.section));

  const html = sections.map((s) => {
    const title = SECTION_TITLES[s.section] || s.section;
    const body = renderCitations(s.content || "")
      .replace(/\n{2,}/g, "</p><p>")
      .replace(/\n/g, "<br>");
    return `
      <div class="report-section">
        <h2>${escapeHtml(title)}</h2>
        <div class="body"><p>${body}</p></div>
      </div>`;
  }).join("");
  setHTML(reportEl, html);

  reportEl.querySelectorAll(".cite[data-sha]").forEach((el) => {
    el.addEventListener("click", () => {
      document.querySelector('.tab[data-tab="graph"]').click();
      onCommitClick(el.dataset.sha);
    });
  });
}

// ---------- chat ----------
function resetChatUI() {
  setHTML(chatMessagesEl, `<div class="chat-hint">Ask about the repository. Answers cite commits, files, and branches.</div>`);
}

chatSendBtn.addEventListener("click", sendChat);
chatInputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); sendChat(); }
});

async function sendChat() {
  const q = chatInputEl.value.trim();
  if (!q || !state.activeMissionId) return;
  chatInputEl.value = "";

  chatMessagesEl.querySelectorAll(".chat-hint").forEach(h => h.remove());

  const userEl = document.createElement("div");
  userEl.className = "chat-msg user";
  userEl.textContent = q;
  chatMessagesEl.appendChild(userEl);
  chatMessagesEl.scrollTop = chatMessagesEl.scrollHeight;

  const asstEl = document.createElement("div");
  asstEl.className = "chat-msg assistant";
  asstEl.textContent = "…";
  chatMessagesEl.appendChild(asstEl);

  state.chatHistory.push({ role: "user", content: q });

  let answer = "";
  let citations = null;

  try {
    const r = await fetch(`${API}/missions/${state.activeMissionId}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question: q,
        history: state.chatHistory.slice(-10, -1),
      }),
    });
    if (!r.ok || !r.body) throw new Error(`chat http ${r.status}`);

    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });

      // Robust SSE parser: normalize line endings before checking for frames
      buf = buf.replace(/\r\n/g, "\n");
      let frameEnd;
      while ((frameEnd = buf.indexOf("\n\n")) !== -1) {
        const frame = buf.slice(0, frameEnd);
        buf = buf.slice(frameEnd + 2);
        let dataLines = [];
        let frameEvent = "message";
        for (const line of frame.split("\n")) {
          if (line.startsWith("event:")) {
            frameEvent = line.slice(6).trim() || "message";
          } else if (line.startsWith("data:")) {
            dataLines.push(line.slice(5).replace(/^ /, ""));
          }
        }
        const data = dataLines.join("\n");
        if (frameEvent === "message") {
          answer += data;
          setHTML(asstEl, renderCitations(answer).replace(/\n/g, "<br>"));
          chatMessagesEl.scrollTop = chatMessagesEl.scrollHeight;
        } else if (frameEvent === "citations") {
          try { citations = JSON.parse(data); } catch { }
        }
      }
    }
  } catch (e) {
    setHTML(asstEl, `<span style="color:var(--red)">Chat failed: ${escapeHtml(e.message)}</span>`);
    return;
  }

  if (!answer) {
    setHTML(asstEl, `<span class="dim">No answer.</span>`);
  }

  if (citations) {
    const cites = [];
    (citations.shas || []).slice(0, 10).forEach((s) =>
      cites.push(`<span class="cite" data-sha="${escapeHtml(s)}">${escapeHtml(s.slice(0, 7))}</span>`));
    (citations.files || []).slice(0, 10).forEach((f) =>
      cites.push(`<span class="cite file">${escapeHtml(f)}</span>`));
    (citations.branches || []).slice(0, 6).forEach((b) =>
      cites.push(`<span class="cite branch">${escapeHtml(b)}</span>`));
    if (cites.length) {
      const div = document.createElement("div");
      div.className = "citations";
      setHTML(div, cites.join(" "));
      asstEl.appendChild(div);
      div.querySelectorAll(".cite[data-sha]").forEach((el) => {
        el.addEventListener("click", () => {
          document.querySelector('.tab[data-tab="graph"]').click();
          onCommitClick(el.dataset.sha);
        });
      });
    }
  }

  state.chatHistory.push({ role: "assistant", content: answer });
}

// ---------- graph filter & search ----------
function _bindFilter(checkboxId, key) {
  const el = document.getElementById(checkboxId);
  if (!el) return;
  el.checked = state.graphFilter[key];
  el.addEventListener("change", () => {
    state.graphFilter[key] = el.checked;
    if (state.graphMode === "knowledge") renderConstellations();
  });
}
_bindFilter("filterTheme", "theme");
_bindFilter("filterFile", "file");
_bindFilter("filterFn", "fn");

const _graphSearchInput = document.getElementById("graphSearch");
if (_graphSearchInput) {
  _graphSearchInput.addEventListener("input", () => {
    const q = _graphSearchInput.value.trim().toLowerCase();
    d3.select("#graph").selectAll(".tree-nodes g").each(function(d) {
      const title = ((d.data && d.data.title) || "").toLowerCase();
      const match = q.length > 1 && title.includes(q);
      d3.select(this).select("circle")
        .attr("stroke", match ? "var(--amber)" : (_KIND_COLORS[(d.data && d.data.kind)] || "var(--violet)"))
        .attr("stroke-width", match ? 3.5 : ((d.data && d.data.id === "__root") ? 2.5 : 1.5));
    });
  });
}

// ---------- boot ----------
(async function init() {
  await refreshMissions();
})();
