#!/usr/bin/env node
/**
 * 用 jsdom 把构建出来的 bundle 真跑一遍，看界面逻辑能不能把各个面板都点亮。
 *
 * 为什么需要它：`node --test src/*.test.ts` 测的是纯逻辑和接线（id、路径对不对），
 * 但"提交 → 收事件 → 各面板更新 → 显示结果"这条链路是 DOM 操作，静态检查测不到。
 * 这个脚本用假的 fetch 和 EventSource 把整条链路跑一遍。
 *
 * 用法（在 web/ 下）：
 *     npm install --no-save jsdom
 *     node uicheck.mjs
 *
 * jsdom 故意没写进 devDependencies：它只在这一个检查里用得上，用到时临时装。
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

const dom = new JSDOM(html, { runScripts: "outside-only", virtualConsole: vc });
const { window } = dom;

const calls = [];
window.fetch = async (url, opts) => {
  calls.push(`${opts?.method ?? "GET"} ${url}`);
  const reply = (obj) => ({
    ok: true,
    status: 200,
    json: async () => obj,
    text: async () => JSON.stringify(obj),
  });
  if (url === "/healthz") return reply({ ok: true, store: "StubStore", runs: 1 });
  if (url === "/runs" && (opts?.method ?? "GET") === "GET") {
    return reply({ runs: [
      { id: "old-1", task: "跑一半的任务", status: "done", event_count: 3, result: { ok: false } },
    ] });
  }
  if (url === "/runs" && opts?.method === "POST") return reply({ id: "stub-run" });
  if (url.endsWith("/tasks")) {
    return reply({
      enabled: true,
      tasks: [
        { id: "task_aaaa", subject: "读代码", status: "completed", owner: "agent", blockedBy: [] },
        { id: "task_bbbb", subject: "改函数", status: "in_progress", owner: "agent", blockedBy: ["task_aaaa"] },
        { id: "task_cccc", subject: "跑测试", status: "pending", owner: null, blockedBy: ["task_bbbb"] },
      ],
      progress: { done: 1, total: 3 },
    });
  }
  if (url.endsWith("/resume")) return reply({ id: "stub-run", status: "running" });
  return { ok: false, status: 404, json: async () => ({}), text: async () => "404" };
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
  close() { this.readyState = 2; }
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
await sleep(60);

const checks = [];
const check = (name, ok, detail = "") => checks.push({ name, ok, detail });

check("页面加载会拉后端状态", doc.querySelector("#store").textContent.startsWith("store:"),
  doc.querySelector("#store").textContent);
check("历史里给未完成的 run 显示「继续」",
  [...doc.querySelectorAll("#history button")].some((b) => b.textContent === "继续"));

// ---- 提交 ----
doc.querySelector("#task").value = "stub 任务";
doc.querySelector("#optPlan").checked = true;
doc.querySelector("#optTasks").checked = true;
doc.querySelector("#submit").click();
await sleep(60);

const es = sources.at(-1);
check("提交后会订阅事件流", !!es, es?.url ?? "(没有 EventSource)");
check("提交时把开关带上了", calls.some((c) => c === "POST /runs"));

// ---- 事件 ----
const ts = Math.floor(Date.now() / 1000);
es.emit("start", { kind: "start", ts, data: { task: "stub 任务", model: "m", tools: ["bash"] } });
es.emit("model_call", {
  kind: "model_call", ts,
  data: { step: 1, stop_reason: "tool_use", usage: { input_tokens: 120, output_tokens: 30 }, tool_calls: 1 },
});
es.emit("tool_call", {
  kind: "tool_call", ts,
  data: { step: 1, tool: "todo_write", args: { todos: [
    { content: "读代码", status: "completed" },
    { content: "改函数", status: "in_progress", blockedBy: [1] },
    { content: "跑测试", status: "pending", blockedBy: [2] },
  ] }, exit_code: 0, failed: false },
});
await sleep(30);

check("花费汇总出现", doc.querySelector("#usage").textContent.includes("入 120"),
  doc.querySelector("#usage").textContent);
check("计划面板出现", doc.querySelector("#planPanel").hidden === false);
check("计划显示进度", doc.querySelector("#planProgress").textContent.includes("1/3"),
  doc.querySelector("#planProgress").textContent);
check("计划显示依赖", doc.querySelector("#plan").textContent.includes("等 1"),
  doc.querySelector("#plan").textContent.slice(0, 60));

// 任务看板：触发一次任务工具调用后前端会去拉 /tasks
es.emit("tool_call", {
  kind: "tool_call", ts,
  data: { step: 2, tool: "create_task", args: { subject: "跑测试" }, exit_code: 0, failed: false },
});
await sleep(60);
check("任务看板出现", doc.querySelector("#boardPanel").hidden === false);
check("看板显示三条任务", doc.querySelectorAll("#board .task").length === 3,
  `${doc.querySelectorAll("#board .task").length} 张卡`);

// 一条失败的工具调用，用来验筛选
es.emit("tool_call", {
  kind: "tool_call", ts,
  data: { step: 3, tool: "bash", args: { command: "exit 1" }, exit_code: 1, failed: true },
});
await sleep(20);
check("失败事件被标成 warn", doc.querySelectorAll("#events .tone-warn").length >= 1);

// ---- 筛选 ----
const problems = [...doc.querySelectorAll("#filters button")].find((b) => b.dataset.filter === "problems");
problems.click();
await sleep(20);
const rowsAfter = doc.querySelectorAll("#events .event").length;
check("筛选「只看问题」会过滤掉正常事件", rowsAfter >= 1 && rowsAfter < 8, `${rowsAfter} 行`);

// ---- 结束 ----
es.emit("end", {
  kind: "end", ts,
  data: { status: "done", result: { ok: false, final_text: "", steps: 3, stop_reason: "max_steps" } },
});
await sleep(30);

check("结束后显示结果面板", doc.querySelector("#answerPanel").hidden === false);
check("没跑完时出现「继续跑」按钮", doc.querySelector("#resume").hidden === false);
check("页面没有报错", errors.length === 0, errors.slice(0, 3).join(" | "));

for (const c of checks) {
  console.log(`  ${c.ok ? "✅" : "❌"} ${c.name}${c.detail ? "  " + c.detail : ""}`);
}
const failed = checks.filter((c) => !c.ok).length;
console.log(failed === 0 ? `\n界面链路正常（${checks.length} 项）` : `\n${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
