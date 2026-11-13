#!/usr/bin/env node
/**
 * 用 jsdom 把构建出来的 bundle 真跑一遍，看界面逻辑能不能把事件渲染出来。
 *
 * 为什么需要它：`node --test src/*.test.ts` 测的是纯逻辑和接线（id、路径对不对），
 * 但"提交 → 收事件 → 渲染 → 显示结果"这条链路是 DOM 操作，静态检查测不到。
 * 这个脚本用假的 fetch 和 EventSource 把整条链路跑一遍。
 *
 * 用法（在 web/ 下）：
 *     npm install --no-save jsdom
 *     node uicheck.mjs
 *
 * jsdom 故意没写进 devDependencies：它只在这一个检查里用得上，
 * 不想为了它把 `npm ci` 变重。用到的时候临时装。
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

window.fetch = async (url, opts) => {
  const reply = (obj) => ({
    ok: true,
    status: 200,
    json: async () => obj,
    text: async () => JSON.stringify(obj),
  });
  if (url === "/healthz") return reply({ ok: true, store: "StubStore", runs: 0 });
  if (url === "/runs" && (opts?.method ?? "GET") === "GET") return reply({ runs: [] });
  if (url === "/runs" && opts?.method === "POST") return reply({ id: "stub-run" });
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
await sleep(50);

const checks = [];
const check = (name, ok, detail = "") => checks.push({ name, ok, detail });

check("页面加载会拉后端状态", doc.querySelector("#store").textContent.startsWith("store:"),
  doc.querySelector("#store").textContent);

doc.querySelector("#task").value = "stub 任务";
doc.querySelector("#submit").click();
await sleep(50);

const es = sources.at(-1);
check("提交后会订阅事件流", !!es, es?.url ?? "(没有 EventSource)");

const ts = Math.floor(Date.now() / 1000);
es?.emit("start", { kind: "start", ts, data: { task: "stub 任务", model: "m", tools: ["bash"] } });
es?.emit("tool_call", { kind: "tool_call", ts, data: { tool: "bash", args: { command: "echo hi" }, exit_code: 0 } });
await sleep(20);
check("事件渲染成行", doc.querySelectorAll("#events .event").length === 2,
  `${doc.querySelectorAll("#events .event").length} 行`);

es?.emit("end", { kind: "end", ts, data: { status: "done", result: { ok: true, final_text: "ok" } } });
await sleep(20);
check("结束后显示结果面板", doc.querySelector("#answerPanel").hidden === false);
check("结果内容非空", doc.querySelector("#answer").textContent.length > 0);
check("页面没有报错", errors.length === 0, errors.slice(0, 3).join(" | "));

for (const c of checks) {
  console.log(`  ${c.ok ? "✅" : "❌"} ${c.name}${c.detail ? "  " + c.detail : ""}`);
}
const failed = checks.filter((c) => !c.ok).length;
console.log(failed === 0 ? "\n界面链路正常" : `\n${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
