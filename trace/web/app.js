"use strict";

const state = {
  graph: null,
  sources: new Map(),
  selectedNode: null,
  activeTab: "__all__",
  ticker: "AAPL",
  period: "1y",
  greyed: false,
};

const $ = (sel) => document.querySelector(sel);

async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(url + " -> " + res.status);
  return res.json();
}

async function getSource(file) {
  if (!file) return null;
  if (state.sources.has(file)) return state.sources.get(file);
  try {
    const data = await fetchJSON("/api/source?file=" + encodeURIComponent(file));
    state.sources.set(file, data.lines);
    return data.lines;
  } catch (err) {
    state.sources.set(file, null);
    return null;
  }
}

function pct(x) {
  return (x * 100).toFixed(2) + "%";
}

function cacheKeyFor(node) {
  if (!node.cache) return null;
  const tickerKeys = new Set(["symbol", "ticker", "sym"]);
  const parts = [];
  for (const param of node.cache.key_params) {
    if (tickerKeys.has(param)) parts.push(state.ticker);
    else if (param === "period") parts.push(state.period);
    else parts.push("{" + param + "}");
  }
  return node.name + ":" + parts.join(":");
}

function invalidationFor(node) {
  if (!node.cache) return [];
  const g = state.graph;
  const matched = g.invalidations.filter((inv) => inv.target && inv.target.includes(node.name));
  const lines = matched.map((m) => m.file + ":" + m.lineno + " " + m.condition);
  if (lines.length === 0) {
    const generic = g.invalidations.find(
      (inv) => inv.target === "st.cache_data" || inv.target === "st.cache_resource"
    );
    if (generic) lines.push(generic.file + ":" + generic.lineno + " " + generic.condition);
  }
  lines.push(node.cache.ttl ? "TTL " + node.cache.ttl + "s 后自动失效" : "无 TTL（常驻到手动清除）");
  if (node.cache.key_params.length) {
    lines.push("入参变化即未命中：" + node.cache.key_params.join(", "));
  }
  return [...new Set(lines)];
}

function edgeClassification(edge) {
  return edge.tab_boundary ? edge.tab_boundary.classification : "outside-tabs";
}

function nodeVisible(node) {
  if (state.activeTab === "__all__") return true;
  const inTab = state.graph.tabs.some(
    (t) => t.name === state.activeTab && t.file === node.file &&
      t.span_start <= node.lineno && node.lineno <= t.span_end
  );
  if (inTab) return true;
  return state.graph.edges.some(
    (e) => (e.src === node.id || e.dst === node.id) &&
      (e.tab_boundary ? e.tab_boundary.caller_tabs : []).includes(state.activeTab)
  );
}

function renderMetrics() {
  const s = state.graph.stats;
  const boxes = [
    { v: pct(s.coverage), l: "解析覆盖率", cls: s.coverage_gate_passed ? "good" : "bad" },
    { v: pct(s.unresolvable_ratio), l: "不可解析占比", cls: s.unresolvable_gate_passed ? "good" : "bad" },
    { v: s.total_callsites, l: "调用点", cls: "" },
    { v: s.unresolved, l: "不可解析", cls: s.unresolvable_gate_passed ? "" : "bad" },
    { v: s.functions, l: "函数/方法", cls: "" },
    { v: s.modules_scanned, l: "扫描模块", cls: "" },
  ];
  $("#metrics").innerHTML = boxes
    .map((b) => '<div class="metric ' + b.cls + '"><div class="v">' + b.v + '</div><div class="l">' + b.l + "</div></div>")
    .join("");

  const pill = $("#statusPill");
  state.greyed = s.unresolvable_ratio > state.graph.thresholds.unresolvable_ratio;
  document.body.classList.toggle("greyed", state.greyed);
  $("#greyNotice").classList.toggle("hidden", !state.greyed);
  if (state.greyed) {
    pill.className = "status-pill greyed";
    pill.textContent = "结论已置灰：不可解析 " + pct(s.unresolvable_ratio) + " > 15%";
  } else {
    pill.className = "status-pill ok";
    pill.textContent = "结论可用：覆盖率 " + pct(s.coverage) + " · 不可解析 " + pct(s.unresolvable_ratio);
  }
}

function renderBuildMeta() {
  const b = state.graph.build;
  $("#buildMeta").innerHTML =
    "mode: <code>" + b.mode + "</code><br>" +
    "重扫模块: <code>" + (b.rescanned_modules.join(", ") || "-") + "</code><br>" +
    "复用模块: <code>" + (b.reused_modules.join(", ") || "-") + "</code><br>" +
    (b.fallback_reason ? "回退原因: " + b.fallback_reason + "<br>" : "") +
    "生成于 <code>" + state.graph.generated_at + "</code>";
}

function renderTabs() {
  const bar = $("#tabBar");
  const tabs = state.graph.tabs;
  const all = [{ name: "全部", file: "", span_start: 0, span_end: 1e9 }];
  bar.innerHTML = all
    .concat(tabs)
    .map((t) => {
      const key = t.name === "全部" ? "__all__" : t.name;
      const active = state.activeTab === key ? "active" : "";
      const count = key === "__all__"
        ? state.graph.edges.length
        : state.graph.edges.filter(
            (e) => e.tab_boundary && e.tab_boundary.caller_tabs.includes(t.name)
          ).length;
      const range = t.file ? t.span_start + "-" + t.span_end : "";
      return '<button class="' + active + '" data-tab="' + key + '">' +
        t.name + '<span class="tab-lines">' + range + " · " + count + "边</span></button>";
    })
    .join("");
  bar.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.activeTab = btn.dataset.tab;
      renderTabs();
      renderGraphNodes();
      renderEntries();
    });
  });
}

function renderGraphNodes() {
  const filter = $("#nodeFilter").value.trim().toLowerCase();
  const view = $("#graphView");
  const nodes = state.graph.nodes.filter((n) => n.type !== "module" && nodeVisible(n));
  const chips = nodes.filter((n) => {
    if (!filter) return true;
    const hay = [
      n.qualname, n.file,
      n.cache ? n.cache.key_template : "",
      (n.state_keys || []).map((k) => k.key).join(" "),
    ].join(" ").toLowerCase();
    return hay.includes(filter);
  });
  view.innerHTML = chips
    .map((n) => {
      const unresolved = state.graph.edges.some((e) => e.src === n.id && !e.resolved);
      const selected = state.selectedNode === n.id ? "selected" : "";
      const badges = [];
      if (n.cache) badges.push('<span class="badge cache">cache</span>');
      if ((n.state_keys || []).length) badges.push('<span class="badge state">state ×' + n.state_keys.length + "</span>");
      return '<div class="node-chip ' + selected + '" data-id="' + n.id + '" title="' + n.id + '">' +
        '<span class="nc-name">' + (unresolved ? "◌ " : "") + n.qualname + "</span>" +
        '<span class="nc-meta">' + n.file + ":" + n.lineno + " · " + n.type + "</span>" +
        '<span class="nc-badges">' + badges.join("") + "</span></div>";
    })
    .join("");
  view.querySelectorAll(".node-chip").forEach((chip) => {
    chip.addEventListener("click", () => selectNode(chip.dataset.id));
  });
}

function renderEntries() {
  const tbody = $("#entryTable tbody");
  const g = state.graph;
  const rows = [];

  for (const node of g.nodes) {
    if (node.type === "module" || !nodeVisible(node)) continue;
    if (!node.lineno) continue; // 落不到具体源码行 -> 不显示
    const stateKeys = (node.state_keys || []).filter((k) => k.key !== "<dynamic>");
    const outgoing = g.edges.filter((e) => e.src === node.id && e.lineno);
    const inTabEdges = state.activeTab === "__all__"
      ? outgoing
      : outgoing.filter((e) =>
          g.tabs.some((t) => t.name === state.activeTab && t.file === e.file &&
            t.span_start <= e.lineno && e.lineno <= t.span_end)
        );
    const calledFromTabs = [...new Set(
      outgoing.flatMap((e) => e.tab_boundary ? e.tab_boundary.caller_tabs : [])
    )];
    rows.push({
      node,
      file: node.file,
      lineno: node.lineno,
      stateKeys,
      cacheTag: node.cache ? cacheKeyFor(node) : null,
      cacheMeta: node.cache,
      invalidations: invalidationFor(node),
      calledFromTabs,
      unresolved: inTabEdges.filter((e) => !e.resolved).length,
      total: inTabEdges.length,
      edges: inTabEdges,
    });
  }

  if (state.activeTab !== "__all__") {
    const tab = g.tabs.find((t) => t.name === state.activeTab);
    const visibleRows = rows.filter((r) =>
      r.edges.length > 0 ||
      (tab && r.file === tab.file && r.lineno >= tab.span_start && r.lineno <= tab.span_end)
    );
    rows.length = 0;
    rows.push(...visibleRows);
  }

  $("#entryCount").textContent = rows.length + " 个可定位条目（已隐藏无源码行/仅动态键条目）";
  tbody.innerHTML = rows
    .map((r) => {
      const keys = r.stateKeys
        .map((k) => '<span class="key-tag">' + k.key + " · " + k.access + " · L" + k.lines.join("/") + "</span>")
        .join("");
      const cache = r.cacheTag
        ? '<span class="cache-tag">' + r.cacheTag + "</span>" +
          '<div class="invalid">参数: ' + (r.cacheMeta.key_params.join(", ") || "-") +
          " · TTL: " + (r.cacheMeta.ttl != null ? r.cacheMeta.ttl : "-") + "</div>"
        : "";
      const invalid = r.invalidations.map((i) => '<div class="invalid">' + i + "</div>").join("");
      const dots = r.edges
        .map((e) => {
          let color = "var(--recompute)";
          let title = "重算（tab 外/每次执行）";
          if (!e.resolved) { color = "var(--unresolved)"; title = "不可解析：" + e.reason; }
          else if (e.dst) {
            const target = g.nodes.find((n) => n.id === e.dst);
            if (target && target.cache) { color = "var(--reuse)"; title = "复用：命中缓存 " + cacheKeyFor(target); }
          }
          const cls = edgeClassification(e);
          if (cls === "cross-tab") { color = "var(--cross)"; title = "跨标签页边界"; }
          return '<span class="edge-dot" data-ln="' + e.lineno + '" title="L' + e.lineno + " " +
            (title || "").replace(/"/g, "&quot;") + '" style="background:' + color + '"></span>';
        })
        .join("");
      const status = (r.unresolved
        ? '<span class="cls-unresolved">' + r.unresolved + "/" + r.total + " 不可解析</span><br>"
        : '<span class="cls-resolved">全部可解析</span><br>') +
        '<span class="edge-dots">' + dots + "</span>";
      return '<tr class="row" data-id="' + r.node.id + '">' +
        "<td><code>" + r.node.qualname + "</code></td>" +
        "<td>" + r.file + "</td>" +
        '<td><span class="loc-link">' + r.lineno + "</span></td>" +
        '<td>' + (keys || '<span class="invalid">—</span>') + "</td>" +
        '<td>' + (cache || '<span class="invalid">—</span>') + "</td>" +
        '<td>' + (invalid || '<span class="invalid">—</span>') + "</td>" +
        "<td>" + status + "</td></tr>";
    })
    .join("");

  tbody.querySelectorAll("tr.row").forEach((tr) => {
    tr.addEventListener("click", () => selectNode(tr.dataset.id));
  });
}

async function selectNode(nodeId) {
  state.selectedNode = nodeId;
  const node = state.graph.nodes.find((n) => n.id === nodeId);
  if (!node) return;
  renderGraphNodes();
  renderEntries();
  const lines = await getSource(node.file);
  renderSource(node, lines);
}

function escapeHtml(text) {
  return text.replace(/[&<>"]/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]
  ));
}

function renderSource(node, lines) {
  const view = $("#sourceView");
  $("#sourceTitle").textContent = "源码 · " + node.qualname;
  const edges = state.graph.edges.filter((e) => e.src === node.id);
  const unresolvedLines = new Set(edges.filter((e) => !e.resolved).map((e) => e.lineno));
  const callLines = new Map();
  for (const e of edges) {
    if (!callLines.has(e.lineno)) callLines.set(e.lineno, []);
    callLines.get(e.lineno).push(e);
  }
  const tabOfLine = (file, ln) =>
    state.graph.tabs.find((t) => t.file === file && t.span_start <= ln && ln <= t.span_end);
  const nodeById = new Map(state.graph.nodes.map((n) => [n.id, n]));
  const cachedCallLines = new Set(
    edges
      .filter((e) => e.dst && nodeById.get(e.dst) && nodeById.get(e.dst).cache)
      .map((e) => e.lineno)
  );
  const stateLines = new Set();
  for (const k of node.state_keys || []) (k.lines || []).forEach((ln) => stateLines.add(ln));
  if (!lines) {
    view.innerHTML = "<code>无法读取 " + node.file + "</code>";
    return;
  }
  const start = Math.max(1, node.lineno - 2);
  const end = node.end_lineno + 2;
  let html = "<table><tbody>";
  for (let i = 1; i <= lines.length; i++) {
    if (i < start || i > end) continue;
    let cls = "srcline";
    if (i === node.lineno) cls += " hl-def";
    if (callLines.has(i)) cls += " hl-in";
    if (unresolvedLines.has(i)) cls += " hl-unresolved";
    if (cachedCallLines.has(i)) cls += " hl-reuse";
    if (stateLines.has(i)) cls += " hl-state";
    const tab = tabOfLine(node.file, i);
    if (tab) cls += " hl-tab";
    const tip = (callLines.get(i) || [])
      .map((e) => e.name + " -> " + (e.resolved ? e.dst || e.target : "不可解析: " + e.reason))
      .join("\n");
    html += '<tr class="' + cls + '" data-line="' + i + '" title="' + escapeHtml(tip) + '">' +
      '<td class="ln">' + i + '</td><td class="code-cell">' + escapeHtml(lines[i - 1] || "") + "</td></tr>";
  }
  html += "</tbody></table>";
  view.innerHTML = html;
  $("#sourceHint").textContent =
    node.file + ":" + node.lineno + "-" + node.end_lineno +
    " · 绿=定义 蓝=调用点 红=不可解析 橙=缓存复用行 紫=session_state 紫底=标签页块内（切到别页不重算）";
}

function updateCachePreview() {
  $("#cacheKeyPreview").textContent =
    "fetch_price_history:" + state.ticker + ":" + state.period +
    " · fetch_stock_info:" + state.ticker;
}

async function init() {
  try {
    state.graph = await fetchJSON("/api/graph");
  } catch (err) {
    document.body.innerHTML =
      '<pre style="padding:24px;color:#ff5d6c">无法加载 trace/graph.json：' + err +
      "\n请先运行 python trace/builder.py</pre>";
    return;
  }
  renderMetrics();
  renderBuildMeta();
  renderTabs();
  renderGraphNodes();
  renderEntries();
  updateCachePreview();

  $("#ticker").addEventListener("change", (e) => {
    state.ticker = (e.target.value.trim() || "AAPL").toUpperCase();
    updateCachePreview();
    renderEntries();
  });
  document.querySelectorAll(".ticker-quick button").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.ticker = btn.dataset.ticker;
      $("#ticker").value = state.ticker;
      updateCachePreview();
      renderEntries();
    });
  });
  $("#nodeFilter").addEventListener("input", renderGraphNodes);
}

init();
