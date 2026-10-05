"use strict";

const PRESETS = ["AAPL", "MSFT", "GOOGL", "AMZN", "TSLA", "NVDA", "META", "NFLX"];

const state = {
  graph: null,
  nodesById: new Map(),
  edgesBySource: new Map(),
  ticker: "AAPL",
  activeScopes: new Set(),
  hideNoLine: true,
  filter: "",
  selectedFile: null,
  selectedNode: null,
  sourceCache: new Map(),
  collapsed: new Set(),
};

const $ = (sel) => document.querySelector(sel);

function pct(x) {
  return (x * 100).toFixed(2) + "%";
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function escapeAttr(text) {
  return escapeHtml(text);
}

function cssEscapeAttr(text) {
  return window.CSS && CSS.escape ? CSS.escape(text)
    : text.replace(/["\\]/g, "\\$&");
}

function short(label) {
  return label.length > 26 ? label.slice(0, 25) + "…" : label;
}

function init() {
  renderPresets();
  bindEvents();
  loadGraph();
}

function bindEvents() {
  $("#tickerInput").addEventListener("input", (e) => {
    state.ticker = e.target.value.trim().toUpperCase();
    renderPresets();
  });
  $("#hideNoLine").checked = state.hideNoLine;
  $("#hideNoLine").addEventListener("change", (e) => {
    state.hideNoLine = e.target.checked;
    renderEntries();
  });
  $("#filterInput").addEventListener("input", (e) => {
    state.filter = e.target.value.trim().toLowerCase();
    renderEntries();
  });
  $("#rescanBtn").addEventListener("click", loadGraph);
}

function renderPresets() {
  const box = $("#presets");
  box.innerHTML = "";
  for (const sym of PRESETS) {
    const btn = document.createElement("button");
    btn.textContent = sym;
    if (sym === state.ticker) btn.classList.add("active");
    btn.addEventListener("click", () => {
      state.ticker = sym;
      $("#tickerInput").value = sym;
      renderPresets();
    });
    box.appendChild(btn);
  }
}

async function loadGraph() {
  const res = await fetch("/api/graph");
  if (!res.ok) {
    $("#entryList").textContent = "graph.json 不可用";
    return;
  }
  state.graph = await res.json();
  state.nodesById = new Map(state.graph.nodes.map((n) => [n.id, n]));
  state.edgesBySource = new Map();
  for (const edge of state.graph.edges) {
    if (!state.edgesBySource.has(edge.source)) {
      state.edgesBySource.set(edge.source, []);
    }
    state.edgesBySource.get(edge.source).push(edge);
  }
  renderMeters();
  renderTabs();
  renderScanInfo();
  renderEntries();
  applyThresholdGate();
}

function renderMeters() {
  const s = state.graph.stats;
  const coveragePass = s.coverage >= s.coverage_floor;
  const ratioPass = !s.threshold_exceeded;
  $("#meters").innerHTML =
    '<div class="meter ' + (coveragePass ? "pass" : "fail") + '">' +
      '<div class="v">' + pct(s.coverage) + "</div>" +
      '<div class="k">函数覆盖率 (' + s.reached_functions + "/" +
        s.total_functions + ")</div></div>" +
    '<div class="meter ' + (ratioPass ? "pass" : "fail") + '">' +
      '<div class="v">' + pct(s.unresolvable_ratio) + "</div>" +
      '<div class="k">不可解析比例 (' + s.unresolved_calls + "/" +
        s.total_calls + ")</div></div>";
}

function renderScanInfo() {
  const scan = state.graph.scan || {};
  const lines = [
    "模式: " + scan.mode,
    "变更模块: " + ((scan.changed_modules || []).join(", ") || "-"),
  ];
  if (scan.fallback_reason) lines.push("退回原因: " + scan.fallback_reason);
  $("#scanInfo").textContent = lines.join("\n");
}

function applyThresholdGate() {
  const s = state.graph.stats;
  const grey = $("#greyout");
  if (s.threshold_exceeded) {
    grey.classList.remove("hidden");
    document.body.classList.add("greyed");
    $("#thresholdBanner").classList.remove("hidden");
    $("#thresholdBanner").textContent =
      "不可解析 " + pct(s.unresolvable_ratio) + " 超过 " +
      pct(s.unresolvable_limit) + "，页面转灰，不出结论";
    $("#greyText").textContent =
      "当前不可解析调用 " + s.unresolved_calls + "/" + s.total_calls +
      " = " + pct(s.unresolvable_ratio) + "。";
  } else {
    grey.classList.add("hidden");
    document.body.classList.remove("greyed");
    $("#thresholdBanner").classList.add("hidden");
  }
}

document.addEventListener("DOMContentLoaded", init);

// --- 标签页 / 分区 -------------------------------------------------------

function renderTabs() {
  const counts = new Map();
  const kinds = new Map();
  for (const edge of state.graph.edges) {
    for (const scope of edge.scopes || []) {
      counts.set(scope.label, (counts.get(scope.label) || 0) + 1);
      kinds.set(scope.label, scope.kind);
    }
  }
  const list = $("#tabList");
  list.innerHTML = "";

  const all = document.createElement("button");
  all.className = "tab-item" + (state.activeScopes.size === 0 ? " active" : "");
  all.innerHTML =
    '<span class="dot tab"></span><span>全部（不按分区过滤）</span>' +
    '<span class="n">' + state.graph.edges.length + "</span>";
  all.addEventListener("click", () => {
    state.activeScopes.clear();
    renderTabs();
    renderEntries();
  });
  list.appendChild(all);

  for (const [label, n] of [...counts.entries()].sort()) {
    const btn = document.createElement("button");
    const active = state.activeScopes.has(label);
    btn.className = "tab-item" + (active ? " active" : "");
    const kind = kinds.get(label);
    btn.innerHTML =
      '<span class="dot ' + kind + '"></span>' +
      "<span>" + escapeHtml(short(label)) + "</span>" +
      '<span class="n">' + n + "</span>";
    btn.title = label;
    btn.addEventListener("click", () => {
      if (state.activeScopes.has(label)) state.activeScopes.delete(label);
      else state.activeScopes.add(label);
      renderTabs();
      renderEntries();
    });
    list.appendChild(btn);
  }
}

function edgeInScopes(edge) {
  if (state.activeScopes.size === 0) return true;
  return (edge.scopes || []).some((s) => state.activeScopes.has(s.label));
}

function nodeInScopes(node) {
  if (state.activeScopes.size === 0) return true;
  return (state.edgesBySource.get(node.id) || []).some(edgeInScopes);
}

// --- 函数 / 条目列表 -----------------------------------------------------

function renderEntries() {
  const list = $("#entryList");
  list.innerHTML = "";
  let shownEntries = 0;

  const sorted = [...state.graph.nodes].sort((a, b) => a.lineno - b.lineno);
  for (const node of sorted) {
    if (state.filter &&
        !node.qualname.toLowerCase().includes(state.filter)) continue;
    if (!nodeInScopes(node)) continue;

    const block = document.createElement("div");
    block.className = "func-block";

    const head = document.createElement("button");
    head.className = "func-head";
    const zone = node.reuse_zone ? "reuse" : "recompute";
    const zoneLabel = node.reuse_zone ? "复用区" : "重算区";
    head.innerHTML =
      '<span class="zone-tag ' + zone + '">' + zoneLabel + "</span>" +
      '<span class="func-name">' + escapeHtml(node.name) + "</span>" +
      (node.class
        ? '<span class="func-class">' + escapeHtml(node.class) + "</span>"
        : "") +
      '<span class="func-line">' + node.file + ":" + node.lineno + "</span>";
    list.appendChild(block);
    block.appendChild(head);

    const body = document.createElement("div");
    body.className =
      "func-body" + (state.collapsed.has(node.id) ? " collapsed" : "");
    block.appendChild(body);
    head.addEventListener("click", async () => {
      if (state.collapsed.has(node.id)) state.collapsed.delete(node.id);
      else state.collapsed.add(node.id);
      body.classList.toggle("collapsed");
      await showNode(node, false);
    });

    renderMeta(body, node);

    const edges = (state.edgesBySource.get(node.id) || [])
      .filter(edgeInScopes)
      .sort((a, b) => a.lineno - b.lineno);

    for (const edge of edges) {
      if (state.hideNoLine && !edge.lineno) continue;
      shownEntries += 1;
      body.appendChild(renderEdgeRow(node, edge));
    }
  }
  $("#entryCount").textContent = "· " + shownEntries + " 条可定位条目";
}

function renderMeta(body, node) {
  const row = document.createElement("div");
  row.className = "meta-row";
  if (node.cache) {
    const chip = document.createElement("span");
    chip.className = "chip cache";
    chip.textContent = "缓存键 " + node.cache_key;
    row.appendChild(chip);
    const inv = document.createElement("div");
    inv.className = "invalidation";
    inv.textContent = "失效条件：" + node.cache.invalidation;
    row.appendChild(inv);
  }
  for (const sk of node.session_keys || []) {
    const chip = document.createElement("span");
    chip.className = "chip state";
    chip.textContent =
      "session_state['" + sk.key + "'] · " + sk.access + " · L" + sk.lineno;
    row.appendChild(chip);
  }
  if (!node.cache &&
      (!node.session_keys || node.session_keys.length === 0)) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = "无缓存键 / 无 session_state 键";
    row.appendChild(chip);
  }
  body.appendChild(row);
}

function renderEdgeRow(node, edge) {
  const row = document.createElement("button");
  const cls = edge.kind === "unresolved" ? "kind-unresolved"
    : edge.cross_boundary ? "kind-cross"
    : edge.kind === "local" ? "kind-local" : "kind-external";
  row.className = "entry-row " + cls;

  let targetHtml = "";
  if (edge.kind === "local" && edge.target) {
    const targetNode = state.nodesById.get(edge.target);
    targetHtml = targetNode
      ? escapeHtml(targetNode.qualname) +
        ' <span class="entry-preview">→ ' + targetNode.file + ":" +
        targetNode.lineno + "</span>"
      : escapeHtml(edge.target);
  } else if (edge.kind === "external") {
    targetHtml =
      '<span class="external">' + escapeHtml(edge.external) + "</span>";
  } else {
    targetHtml = '<span class="unresolved">不可解析</span>';
  }

  const kindLabel = edge.cross_boundary && edge.kind === "local" ? "跨分界"
    : edge.kind === "local" ? "本地"
    : edge.kind === "external" ? "外部" : "不可解析";

  const scopeTags = (edge.scopes || []).map((s) =>
    '<span class="scope-tag ' + s.kind + '">[' +
    escapeHtml(s.label) + "]</span>").join("");

  row.innerHTML =
    '<span class="entry-line">L' + edge.lineno + "</span>" +
    '<span class="entry-target">' + targetHtml + "<br>" +
      '<span class="entry-preview">' +
      escapeHtml(edge.preview) + "</span></span>" +
    '<span class="entry-kind ' +
      (edge.cross_boundary ? "cross" : edge.kind) + '">' +
      kindLabel + "</span>" +
    (edge.reason
      ? '<span class="entry-reason">' +
        escapeHtml(edge.reason) + "</span>" : "") +
    (scopeTags
      ? '<span class="scope-tags">' + scopeTags + "</span>" : "");

  row.addEventListener("click", async () => {
    await showNode(node, true);
    highlightLine(node.file, edge.lineno, "edge");
    if (edge.kind === "local" && edge.target) {
      const target = state.nodesById.get(edge.target);
      if (target) {
        highlightRange(target.file, target.lineno, target.end_lineno, "hit");
      }
    }
  });
  return row;
}

// --- 源码视图 ------------------------------------------------------------

async function showNode(node, scrollToNode) {
  state.selectedNode = node;
  state.selectedFile = node.file;
  await renderSource(node.file);
  highlightRange(node.file, node.lineno, node.end_lineno, "hit");
  for (const edge of state.edgesBySource.get(node.id) || []) {
    if (edge.lineno) markZone(node.file, edge.lineno, node);
  }
  $("#sourceTitle").textContent = node.qualname + " — " + node.file;
  $("#edgeSummary").textContent = node.reuse_zone
    ? "复用区（绿标）：" + (node.cache_key || "session_state 驱动")
    : "重算区（橙标）：每次脚本整体重跑时重新执行";
  if (scrollToNode) scrollToLine(node.file, node.lineno);
}

async function renderSource(file) {
  let source = state.sourceCache.get(file);
  if (source === undefined) {
    const res = await fetch("/api/source?file=" + encodeURIComponent(file));
    const payload = await res.json();
    source = payload.source || "";
    state.sourceCache.set(file, source);
  }
  const code = $("#sourceView code");
  const lines = source.split("\n");
  code.innerHTML = lines.map((text, i) => {
    const n = i + 1;
    return '<span class="ln" data-file="' + escapeAttr(file) +
      '" data-line="' + n + '">' +
      '<span class="gutter">' + n + "</span>" +
      (escapeHtml(text) || " ") + "</span>";
  }).join("");
}

function lineEl(file, n) {
  return document.querySelector(
    '.ln[data-file="' + cssEscapeAttr(file) + '"][data-line="' + n + '"]');
}

function highlightLine(file, n, cls) {
  const el = lineEl(file, n);
  if (el) {
    el.classList.add(cls);
    el.scrollIntoView({ block: "center", behavior: "smooth" });
  }
}

function highlightRange(file, start, end, cls) {
  for (let n = start; n <= (end || start); n++) {
    const el = lineEl(file, n);
    if (el) el.classList.add(cls);
  }
}

function markZone(file, lineno, node) {
  const el = lineEl(file, lineno);
  if (!el) return;
  el.classList.add(node.reuse_zone ? "zone-reuse" : "zone-recompute");
}

function scrollToLine(file, n) {
  const el = lineEl(file, n);
  if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
}
