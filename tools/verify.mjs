#!/usr/bin/env node
/**
 * Verification gate for the static call-graph tracing.
 *
 * Requires the local trace server to already be running:
 *   python trace/server.py        (default http://127.0.0.1:8899)
 *
 * Checks (real numbers are printed, not just pass/fail):
 *   1. server reachable + graph served from trace/graph.json
 *   2. coverage >= coverage gate
 *   3. unresolvable ratio <= 15%
 *   4. every listed entry resolves to a concrete source line
 *   5. incremental rebuild is edge-for-edge identical to a full rebuild
 *   6. cross-module indirect edge changes force a full fallback
 */

import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

const ROOT = process.cwd();
const BASE = process.env.TRACE_URL || "http://127.0.0.1:8899";
const PY = process.env.PYTHON || "python";

let failures = 0;
const log = (ok, label, detail = "") => {
  console.log(`${ok ? "PASS" : "FAIL"}  ${label}${detail ? "  -- " + detail : ""}`);
  if (!ok) failures += 1;
};
const get = async (urlPath) => {
  const res = await fetch(BASE + urlPath);
  return { status: res.status, body: await res.text() };
};
const getJson = async (urlPath) => {
  const { status, body } = await get(urlPath);
  if (status !== 200) throw new Error(`${urlPath} -> HTTP ${status}: ${body.slice(0, 200)}`);
  return JSON.parse(body);
};

function runBuilder(args, cwd = ROOT) {
  return execFileSync(PY, ["trace/builder.py", ...args], {
    cwd, encoding: "utf-8", stdio: ["ignore", "pipe", "pipe"],
  });
}

function edgeIds(graph) {
  return graph.edges.map((e) => e.key).sort();
}

function makeTmpProject() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "trace-verify-"));
  fs.mkdirSync(path.join(dir, "pkg"), { recursive: true });
  fs.writeFileSync(path.join(dir, "pkg", "__init__.py"), "");
  return dir;
}

function writeScenario(dir, dispatchBody) {
  fs.writeFileSync(path.join(dir, "pkg", "helpers.py"),
    "def target_one():\n    return 1\n\n" +
    "def target_two():\n    return 2\n\n" + dispatchBody + "\n");
  fs.writeFileSync(path.join(dir, "app.py"),
    "from pkg import helpers\n\n" +
    "def runner():\n" +
    "    key = 'a'\n" +
    "    return helpers.DISPATCH[key]()\n\n" +
    "def direct():\n" +
    "    return helpers.target_one()\n");
}

async function main() {
  console.log("== trace verification ==");
  console.log("server:", BASE);

  // 1. health
  let graph;
  try {
    const health = await getJson("/api/health");
    log(health.ok === true, "server /api/health reachable");
  } catch (err) {
    log(false, "server reachable", String(err.message));
    finish();
    return;
  }

  // rebuild fresh full graph so numbers reflect current source
  const fullOut = runBuilder(["--full"]);
  process.stdout.write("builder(full): " + fullOut.trim() + "\n");

  graph = await getJson("/api/graph");
  const s = graph.stats;

  console.log("\n-- gate numbers --");
  console.log(`callsites total     : ${s.total_callsites}`);
  console.log(`resolved local      : ${s.resolved_local}`);
  console.log(`resolved external   : ${s.resolved_external}`);
  console.log(`unresolved          : ${s.unresolved}`);
  console.log(`coverage            : ${(s.coverage * 100).toFixed(2)}%  (gate >= ${(graph.thresholds.coverage_gate * 100).toFixed(0)}%)`);
  console.log(`unresolvable ratio  : ${(s.unresolvable_ratio * 100).toFixed(2)}%  (gate <= ${(graph.thresholds.unresolvable_ratio * 100).toFixed(0)}%)`);

  log(s.coverage >= graph.thresholds.coverage_gate,
    "coverage gate", `${(s.coverage * 100).toFixed(2)}% >= ${(graph.thresholds.coverage_gate * 100).toFixed(0)}%`);
  log(s.unresolvable_ratio <= graph.thresholds.unresolvable_ratio,
    "unresolvable gate", `${(s.unresolvable_ratio * 100).toFixed(2)}% <= 15%`);

  // 2. every displayed entry must have a concrete source line
  const modules = new Set(graph.modules);
  let checkedSources = 0;
  const sourceCache = new Map();
  for (const node of graph.nodes) {
    if (node.type === "module") continue;
    if (!node.lineno || node.lineno < 1) {
      log(false, "entry has concrete source line", node.id);
      continue;
    }
    if (!sourceCache.has(node.file)) {
      const src = await getJson("/api/source?file=" + encodeURIComponent(node.file));
      sourceCache.set(node.file, src.lines);
    }
    const lines = sourceCache.get(node.file);
    const within = node.lineno <= lines.length;
    if (!within) log(false, "entry line within file", `${node.id} L${node.lineno}`);
    checkedSources += 1;
  }
  log(checkedSources > 0, "all entries map to real source lines", `${checkedSources} nodes checked via server`);

  // source endpoint refuses path traversal
  const blocked = await get("/api/source?file=" + encodeURIComponent("../server.py"));
  log(blocked.status === 404, "server blocks source path traversal", "HTTP " + blocked.status);

  // 3. graph.json is under trace/ and server only reads
  const graphPath = path.join(ROOT, "trace", "graph.json");
  log(fs.existsSync(graphPath), "trace/graph.json present");

  // 4. incremental == full, edge for edge
  console.log("\n-- incremental consistency --");
  const fullEdges = edgeIds(graph);
  // touch the dashboard with a no-op change then revert
  const dash = path.join(ROOT, "stock_dashboard.py");
  const original = fs.readFileSync(dash, "utf-8");
  let incOut = "";
  try {
    fs.writeFileSync(dash, original + "\n# incremental probe comment\n");
    incOut = runBuilder([]);
  } finally {
    fs.writeFileSync(dash, original);
  }
  process.stdout.write("builder(incremental): " + incOut.trim() + "\n");
  const incGraph = JSON.parse(fs.readFileSync(graphPath, "utf-8"));
  const incMode = incGraph.build.mode;
  const incEdges = edgeIds(incGraph);
  const sameEdges = JSON.stringify(fullEdges) === JSON.stringify(incEdges);
  // restore the official full graph so the UI shows real data
  runBuilder(["--full"]);
  log(incMode === "incremental" && sameEdges,
    "incremental output edge-for-edge identical to full",
    `mode=${incMode}, edges full=${fullEdges.length} inc=${incEdges.length}`);

  // 5. cross-module indirect stale edge -> forced full fallback
  console.log("\n-- cross-module indirect fallback --");
  const tmp = makeTmpProject();
  try {
    const g = path.join(tmp, "graph.json");
    const st = path.join(tmp, "state.json");
    writeScenario(tmp, "DISPATCH = {'a': target_one}");
    const b1 = runBuilder(["--root", tmp, "--graph", g, "--state", st]);
    process.stdout.write("scenario full: " + b1.trim() + "\n");
    writeScenario(tmp, "DISPATCH = {'a': target_two}");
    const b2 = runBuilder(["--root", tmp, "--graph", g, "--state", st]);
    process.stdout.write("scenario changed: " + b2.trim() + "\n");
    const scenarioGraph = JSON.parse(fs.readFileSync(g, "utf-8"));
    log(
      scenarioGraph.build.mode === "full" &&
      /indirect|stale/i.test(scenarioGraph.build.fallback_reason || ""),
      "cross-module indirect change forces full fallback",
      "mode=" + scenarioGraph.build.mode
    );
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }

  finish();
}

function finish() {
  console.log("\n" + (failures === 0 ? "ALL GATES PASSED" : `${failures} GATE(S) FAILED`));
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error("verify error:", err);
  process.exit(2);
});
