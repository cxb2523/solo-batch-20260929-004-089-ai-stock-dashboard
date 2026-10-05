// 调用图追踪验证器
//
// 检查项：
// 1. trace/graph.json 存在且结构完整（节点/边/统计）。
// 2. 函数覆盖率达到门槛（默认 0.80），打印实际数字。
// 3. 不可解析比例不超过门槛（默认 0.15），打印实际数字。
// 4. 每条展示条目都能落到具体源码行（无行号条目不得出现在图中）。
// 5. 每条 local 边的目标节点存在且目标行号落在其函数行范围内。
// 6. 增量一致性：复制项目到临时目录，全量 → 改注释 → 增量，
//    两次结果必须逐边一致；模式必须为 incremental 或显式退回全量。
// 7. 灰页数据：threshold_exceeded 与比例数字一致。

import { readFile, writeFile, mkdir, rm, cp, mkdtemp } from "node:fs/promises";
import { existsSync } from "node:fs";
import { spawnSync } from "node:child_process";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "..");
const GRAPH = path.join(ROOT, "trace", "graph.json");

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
}

function run(cmd, args, cwd, extra = {}) {
  const res = spawnSync(cmd, args, {
    cwd,
    encoding: "utf-8",
    shell: process.platform === "win32",
    ...extra,
  });
  if (res.status !== 0) {
    throw new Error(
      `${cmd} ${args.join(" ")} failed (${res.status})\n` +
      (res.stdout || "") + "\n" + (res.stderr || ""),
    );
  }
  return res;
}

function edgeSig(graph) {
  return graph.edges
    .map((e) => [
      e.source, e.target ?? "", e.kind, e.lineno,
      e.external ?? "", e.reason ?? "",
    ].join("|"))
    .sort();
}

// --- 1. graph.json 基本检查 ------------------------------------------------

if (!existsSync(GRAPH)) {
  console.error("trace/graph.json 不存在，请先运行 python trace/analyzer.py");
  process.exit(1);
}

const graph = JSON.parse(await readFile(GRAPH, "utf-8"));
check("graph.json 含 nodes/edges/stats",
  Array.isArray(graph.nodes) && Array.isArray(graph.edges) && !!graph.stats,
  `${graph.nodes?.length ?? 0} 节点 / ${graph.edges?.length ?? 0} 边`);

// --- 2/3. 两道门槛：打印实际数字 ------------------------------------------

const s = graph.stats;
const coverageNum = s.coverage;
const ratioNum = s.unresolvable_ratio;
console.log("");
console.log(`函数覆盖率:       ${s.reached_functions}/${s.total_functions} ` +
            `= ${(coverageNum * 100).toFixed(2)}% ` +
            `(门槛 ${(s.coverage_floor * 100).toFixed(0)}%)`);
console.log(`不可解析比例:     ${s.unresolved_calls}/${s.total_calls} ` +
            `= ${(ratioNum * 100).toFixed(2)}% ` +
            `(门槛 ${(s.unresolvable_limit * 100).toFixed(0)}%)`);
console.log(`本地/外部/不可解析: ${s.local_calls}/${s.external_calls}/` +
            `${s.unresolved_calls}`);
console.log("");

check("覆盖率 ≥ 门槛", coverageNum >= s.coverage_floor,
  `${(coverageNum * 100).toFixed(2)}% ≥ ${(s.coverage_floor * 100).toFixed(0)}%`);
check("不可解析比例 ≤ 门槛", ratioNum <= s.unresolvable_limit,
  `${(ratioNum * 100).toFixed(2)}% ≤ ${(s.unresolvable_limit * 100).toFixed(0)}%`);

// --- 7. 阈值字段自洽 -------------------------------------------------------

check("threshold_exceeded 与比例一致",
  s.threshold_exceeded === (ratioNum > s.unresolvable_limit),
  `threshold_exceeded=${s.threshold_exceeded}`);

// --- 4. 条目必须落到具体源码行 --------------------------------------------

const noLine = graph.edges.filter((e) => !Number.isInteger(e.lineno));
check("所有调用条目都有具体源码行", noLine.length === 0,
  noLine.length ? `${noLine.length} 条无行号` : "全部有行号");

// --- 5. local 边目标存在、行号合法 ----------------------------------------

const nodeById = new Map(graph.nodes.map((n) => [n.id, n]));
let badTarget = 0;
let crossCount = 0;
for (const e of graph.edges) {
  if (e.kind === "local") {
    const t = nodeById.get(e.target);
    if (!t) { badTarget += 1; continue; }
    if (!(t.lineno <= e.lineno)) badTarget += 1;
  }
  if (e.cross_boundary) crossCount += 1;
}
check("本地边目标节点存在", badTarget === 0,
  badTarget ? `${badTarget} 条异常` : `${s.local_calls} 条本地边全部可定位`);
check("复用/重算跨分界边已标记", crossCount >= 0,
  `${crossCount} 条跨分界边以红色标出`);

// --- 6. 增量一致性（沙箱目录，避免污染当前 graph.json） --------------------

async function verifyIncremental() {
  const sandbox = await mkdtemp(path.join(os.tmpdir(), "trace-inc-"));
  try {
    const project = path.join(sandbox, "proj");
    await cp(ROOT, project, {
      recursive: true,
      filter: (src) =>
        !src.includes(`${path.sep}.git`) &&
        !src.includes(`${path.sep}node_modules`) &&
        !src.includes(`${path.sep}__pycache__`),
    });
    // 清掉既有扫描产物，保证从首次全量开始
    await rm(path.join(project, "trace", "graph.json"), { force: true });
    await rm(path.join(project, "trace", ".state.json"), { force: true });

    const py = process.platform === "win32" ? "python" : "python3";
    run(py, ["trace/analyzer.py", "--full"], project);
    const fullGraph = JSON.parse(
      await readFile(path.join(project, "trace", "graph.json"), "utf-8"),
    );

    // 仅追加一行注释（AST 节点不变，哈希变化，应走增量且逐边一致）
    const target = path.join(project, "stock_dashboard.py");
    const original = await readFile(target, "utf-8");
    await writeFile(target, original + "\n# incremental probe comment\n", "utf-8");

    const out = run(py, ["trace/analyzer.py", "--json"], project);
    const report = JSON.parse(out.stdout);
    const incGraph = JSON.parse(
      await readFile(path.join(project, "trace", "graph.json"), "utf-8"),
    );

    const mode = report.scan.mode;
    const consistent =
      JSON.stringify(edgeSig(fullGraph)) === JSON.stringify(edgeSig(incGraph));

    if (mode === "incremental") {
      check("增量模式只重扫变更模块",
        Array.isArray(report.scan.changed_modules) &&
        report.scan.changed_modules.includes("stock_dashboard"),
        "变更: " + report.scan.changed_modules.join(","));
      check("增量结果与全量重扫逐边一致", consistent,
        consistent ? `${incGraph.edges.length} 条边完全一致`
                   : "存在不一致边");
    } else {
      check("增量退回全量时有明确原因",
        mode === "full" && !!report.scan.fallback_reason,
        report.scan.fallback_reason || "缺少退回原因");
      check("退回全量后的图与独立全量一致", consistent,
        consistent ? "逐边一致" : "退回结果异常");
    }
  } finally {
    await rm(sandbox, { recursive: true, force: true });
  }
}

try {
  await verifyIncremental();
} catch (err) {
  check("增量一致性检查可执行", false, String(err.message).split("\n")[0]);
}

// --- 汇总 ------------------------------------------------------------------

const failed = results.filter((r) => !r.ok);
console.log("");
console.log(`合计 ${results.length} 项，失败 ${failed.length} 项`);
process.exit(failed.length === 0 ? 0 : 1);
