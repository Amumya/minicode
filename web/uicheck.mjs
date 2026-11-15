#!/usr/bin/env node
/**
 * 用 jsdom 把构建出来的 bundle 真跑一遍，看界面逻辑能不能把布局和流式链路跑起来。
 *
 * 覆盖的链路：左栏（工作区/历史会话）→ 点会话加载 → 新会话 → 提交 → 流式更新
 *           → 结束 → 接着说 → 任务看板展开 → 状态徽章复位 → 拒绝运行中重复提交。
 *
 * 用法（在 web/ 下）：npm install --no-save jsdom && node uicheck.mjs
 */

import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ASSETS = join(HERE, "..", "src", "minicode", "service", "web", "assets");

let JSDOM;
let VirtualConsole;
try {
  ({ JSDOM, VirtualConsole } = await import("jsdom"));
} catch {
  console.error("需要 jsdom：npm install --no-save jsdom");
  process.exit(2);
}

const jsName = readdirSync(ASSETS).find((f) => f.endsWith(".js"));
if (!jsName) {
  console.error("找不到构建产物，先跑 npm run build");
  process.exit(2);
}

const html = readFileSync(join(HERE, "index.html"), "utf8").replace(
  /<script type="module"[^>]*><\/script>/,
  "",
);
const bundle = readFileSync(join(ASSETS, jsName), "utf8");

const errors = [];
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => errors.push("jsdom: " + e.message));
vc.on("error", (...a) => errors.push("console.error: " + a.join(" ")));

const dom = new JSDOM(html, {
  runScripts: "outside-only",
  virtualConsole: vc,
  url: "http://127.0.0.1:8765/",
  pretendToBeVisual: true,
});
const { window } = dom;

const calls = [];
const bodies = new Map();
const nowSec = () => Math.floor(Date.now() / 1000);

const HISTORY_RUN = {
  id: "run-a",
  task: "数一下文件数量",
  status: "done",
  result: { ok: true, final_text: "一共 12 个文件" },
  created_at: nowSec() - 3600,
  event_count: 2,
  events: [
    { kind: "start", ts: nowSec(), data: { task: "数一下文件数量" } },
    { kind: "tool_call", ts: nowSec(), data: {
        step: 1, tool: "bash", args: { command: "Get-ChildItem" }, exit_code: 0, failed: false } },
  ],
};

window.fetch = async (url, opts = {}) => {
  const method = opts.method ?? "GET";
  calls.push(`${method} ${url}`);
  if (opts.body) bodies.set(`${method} ${url}`, JSON.parse(opts.body));
  const reply = (obj) => ({
    ok: true,
    status: 200,
    json: async () => obj,
    text: async () => JSON.stringify(obj),
  });

  if (url === "/healthz") return reply({ ok: true, store: "StubStore", runs: 3 });
  if (url.startsWith("/workspaces")) {
    return reply({
      default: "D:\\work\\demo",
      workspaces: [
        { path: "D:\\work\\demo", name: "demo", exists: true, is_default: true },
        { path: "D:\\work\\other", name: "other", exists: true, is_default: false },
      ],
    });
  }
  if (url.startsWith("/conversations")) {
    return reply({
      conversations: [{
        root_id: "run-a", title: "数一下文件数量", updated_at: nowSec() - 60,
        status: "done", turns: 1, run_ids: ["run-a"], latest_run_id: "run-a",
        workspace: "D:\\work\\demo",
      }],
    });
  }
  if (url.startsWith("/runs/run-a")) return reply(HISTORY_RUN);
  if (url === "/runs" && method === "POST") return reply({ id: "run-new" });
  if (url.startsWith("/runs/run-new/followup")) return reply({ id: "run-followup" });
  if (url.endsWith("/tasks")) {
    return reply({
      enabled: true,
      tasks: [
        { id: "task_aaaa", subject: "读代码", status: "completed", owner: "agent", blockedBy: [] },
        { id: "task_bbbb", subject: "改函数", status: "in_progress", owner: "agent", blockedBy: ["task_aaaa"] },
      ],
      progress: { done: 1, total: 2 },
    });
  }
  return reply({ ok: true });
};

const sources = [];
class StubEventSource {
  constructor(url) {
    this.url = url;
    this.readyState = 1;
    this.listeners = new Map();
    sources.push(this);
  }
  addEventListener(kind, fn) {
    if (!this.listeners.has(kind)) this.listeners.set(kind, []);
    this.listeners.get(kind).push(fn);
  }
  close() {
    this.readyState = 2;
  }
  emit(kind, data) {
    for (const fn of this.listeners.get(kind) ?? []) fn({ data: JSON.stringify(data) });
  }
}
window.EventSource = StubEventSource;

try {
  window.eval(bundle);
} catch (e) {
  console.error("bundle 执行失败:", e.message);
  process.exit(1);
}

const doc = window.document;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
await sleep(150);

const checks = [];
const check = (name, ok, detail = "") => checks.push({ name, ok, detail });

// ---------------------------------------------------------------- 初始化与左栏
check("左栏工作区下拉有选项", doc.querySelectorAll("#workspace option").length >= 2,
  `${doc.querySelectorAll("#workspace option").length} 个`);
check("工作区默认选中默认目录", doc.querySelector("#workspace").value === "D:\\work\\demo");
check("左栏渲染了历史会话", doc.querySelectorAll("#conversations .conv").length === 1);
check("底部显示存储信息", doc.querySelector("#storeInfo").textContent.includes("StubStore"));

// ---------------------------------------------------------------- 打开历史会话
doc.querySelector("#conversations .conv").click();
await sleep(150);
check("点会话后加载出对话", doc.querySelectorAll("#chat .turn").length === 1);
check("助手回答显示出来", doc.querySelector("#chat .answer").textContent.includes("一共 12 个文件"));
check("过程可以展开", !!doc.querySelector("#chat details.process"));

// 展开"过程"后收到新事件，展开状态要保持（之前每次重画都会被关掉）
const processDetails = doc.querySelector("#chat details.process");
processDetails.open = true;
processDetails.dispatchEvent(new window.Event("toggle"));
await sleep(30);

// ---------------------------------------------------------------- 新会话
doc.querySelector("#newChat").click();
await sleep(60);
check("新会话回到空状态", !doc.querySelector("#empty").hidden);
check("新会话没有残留消息", doc.querySelectorAll("#chat .turn").length === 0);

// ---------------------------------------------------------------- 提交 + 流式
doc.querySelector("#task").value = "列出 tests 目录";
doc.querySelector("#optPlan").checked = true;
doc.querySelector("#submit").click();
await sleep(120);
const es = sources.at(-1);
check("提交后订阅了事件流", es?.url === "/runs/run-new/events", es?.url ?? "(无)");
const postBody = bodies.get("POST /runs");
check("提交 body 带上了任务和开关",
  postBody?.task === "列出 tests 目录" && postBody?.plan === true,
  JSON.stringify(postBody ?? {}));

// 运行中再点提交 → 不该再发 POST
const before = calls.filter((c) => c === "POST /runs").length;
doc.querySelector("#task").value = "再开一个";
doc.querySelector("#submit").click();
await sleep(80);
check("运行中不允许重复提交",
  calls.filter((c) => c === "POST /runs").length === before,
  `${before} → ${calls.filter((c) => c === "POST /runs").length}`);
doc.querySelector("#task").value = "";

const ts = nowSec();
es.emit("start", { kind: "start", ts, data: { task: "列出 tests 目录" } });
es.emit("model_call", { kind: "model_call", ts,
  data: { step: 1, stop_reason: "tool_use", usage: { input_tokens: 120, output_tokens: 30 }, tool_calls: 1 } });
es.emit("tool_call", { kind: "tool_call", ts,
  data: { step: 1, tool: "bash", args: { command: "Get-ChildItem tests" }, exit_code: 0, failed: false } });
es.emit("tool_call", { kind: "tool_call", ts,
  data: { step: 2, tool: "create_task", args: { subject: "跑测试" }, exit_code: 0, failed: false } });
await sleep(150);

check("流式事件只渲染一次（不重复）",
  doc.querySelectorAll("#chat .turn").length === 1 &&
  doc.querySelectorAll("#chat .tool-list .tool").length === 2,
  `${doc.querySelectorAll("#chat .tool-list .tool").length} 个工具行`);
check("顶栏花费在累加", doc.querySelector("#usage").textContent.includes("入 120"),
  doc.querySelector("#usage").textContent);
check("运行中显示徽章", doc.querySelector("#runStatus").hidden === false);

// ---------------------------------------------------------------- 任务看板
const board = doc.querySelector("#chat details.board");
check("对话里有任务看板", !!board);
if (board) {
  board.open = true;
  board.dispatchEvent(new window.Event("toggle"));
  await sleep(150);
  check("看板拉到数据并渲染卡片", doc.querySelectorAll("#chat .task-card").length === 2,
    `${doc.querySelectorAll("#chat .task-card").length} 张`);
}

// 展开状态在后续事件后仍保持
es.emit("model_call", { kind: "model_call", ts,
  data: { step: 2, stop_reason: "end_turn", usage: { input_tokens: 10, output_tokens: 5 } } });
await sleep(120);
check("新事件后展开状态保持", doc.querySelector("#chat details.board")?.open === true);

// ---------------------------------------------------------------- 结束
es.emit("end", { kind: "end", ts,
  data: { status: "done", result: { ok: true, final_text: "tests 下有 5 个文件", steps: 2 } } });
await sleep(150);
check("结束后显示回答", doc.querySelector("#chat .answer").textContent.includes("tests 下有 5 个文件"));
check("结束后按钮变成「接着说」", doc.querySelector("#submit").textContent === "接着说");
check("结束后状态徽章复位", doc.querySelector("#runStatus").hidden === true);

// ---------------------------------------------------------------- 接着说
doc.querySelector("#task").value = "再数一下 src";
doc.querySelector("#submit").click();
await sleep(150);
check("接着说走 followup", calls.includes("POST /runs/run-new/followup"));
check("followup body 带上了文本",
  bodies.get("POST /runs/run-new/followup")?.text === "再数一下 src",
  JSON.stringify(bodies.get("POST /runs/run-new/followup") ?? {}));
check("接着说订阅新的 run", sources.at(-1)?.url === "/runs/run-followup/events");

// ---------------------------------------------------------------- 出错分支
const es2 = sources.at(-1);
es2.emit("end", { kind: "end", ts,
  data: { status: "error", error: "模型端点挂了" } });
await sleep(150);
check("出错时显示错误文案", doc.querySelector("#chat .answer.error")?.textContent.includes("模型端点挂了"),
  doc.querySelector("#chat .answer.error")?.textContent ?? "(没有错误气泡)");
check("出错后出现「继续跑」", doc.querySelector("#resume").hidden === false);

check("页面没有报错", errors.length === 0, errors.slice(0, 3).join(" | "));

for (const c of checks) {
  console.log(`  ${c.ok ? "✅" : "❌"} ${c.name}${c.detail ? "  " + c.detail : ""}`);
}
const failed = checks.filter((c) => !c.ok).length;
console.log(failed === 0 ? `\n界面链路正常（${checks.length} 项）` : `\n${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
