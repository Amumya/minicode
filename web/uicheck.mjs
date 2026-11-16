#!/usr/bin/env node
/**
 * 用 jsdom 把构建出来的 bundle 真跑一遍。
 *
 * 覆盖：左栏（工作区/历史会话）→ 点会话 → 新会话 → 提交 → 流式（右侧面板增量更新）
 *      → 任务看板 → 结束（Markdown 渲染、结果在最后、思考收起）→ 接着说 → 右栏收起/展开。
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
const requests = [];      // 每条带上 method/url/headers，用来验令牌
const nowSec = () => Math.floor(Date.now() / 1000);

const HISTORY_RUN = {
  id: "run-a",
  task: "数一下文件数量",
  status: "done",
  result: { ok: true, final_text: "一共 **12** 个文件" },
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
  requests.push({ method, url, headers: { ...(opts.headers ?? {}) } });
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
  if (url.startsWith("/memory")) {
    return reply({
      workspace: "D:\\work\\demo",
      total: 2,
      entries: [
        { id: "m1", text: "测试要用 .venv 里的 python", tags: ["环境"], created_at: nowSec() - 100 },
        { id: "m2", text: "提交信息用中文写", tags: [], created_at: nowSec() - 200 },
      ],
    });
  }
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
window.confirm = () => true;   // jsdom 没实现 confirm，删除会用到

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
check("左栏工作区下拉有选项", doc.querySelectorAll("#workspace option").length >= 2);
check("工作区默认选中默认目录", doc.querySelector("#workspace").value === "D:\\work\\demo");
check("左栏渲染了历史会话", doc.querySelectorAll("#conversations .conv").length === 1);
check("底部显示存储信息", doc.querySelector("#storeInfo").textContent.includes("StubStore"));

// 令牌：填了会进 Authorization 头；清掉就没了
const tokenInput = doc.querySelector("#token");
check("有令牌输入框", !!tokenInput);
tokenInput.value = "s3cret";
tokenInput.dispatchEvent(new window.Event("change"));
await sleep(150);
const lastConvReq = requests.filter((r) => r.url.startsWith("/conversations")).at(-1);
check("令牌进 Authorization 头",
  lastConvReq?.headers?.Authorization === "Bearer s3cret",
  JSON.stringify(lastConvReq?.headers ?? {}));
tokenInput.value = "";
tokenInput.dispatchEvent(new window.Event("change"));
await sleep(150);
const afterClear = requests.filter((r) => r.url.startsWith("/conversations")).at(-1);
check("清空令牌后不再带 Authorization",
  afterClear && !("Authorization" in afterClear.headers),
  JSON.stringify(afterClear?.headers ?? {}));

// 左栏：会话行带删除按钮
check("每个会话行都有删除按钮", doc.querySelectorAll("#conversations .conv-del").length === 1,
  `${doc.querySelectorAll("#conversations .conv-del").length} 个`);

// 默认值：五个开关全选、步数 20（实测 8 步不够跑多步任务）
const defaultSwitches = ["#optFiles", "#optPlan", "#optSkills", "#optCompact", "#optTasks", "#optMemory"];
check("五个开关默认全选", defaultSwitches.every((sel) => doc.querySelector(sel).checked),
  defaultSwitches.map((sel) => `${sel}=${doc.querySelector(sel).checked}`).join(" "));
check("默认步数是 20", doc.querySelector("#maxSteps").value === "20",
  doc.querySelector("#maxSteps").value);

// ---------------------------------------------------------------- 右栏存在
check("右侧详情面板存在", !!doc.querySelector("#inspector"));
check("右栏有三个分区（工具调用/任务看板/原始事件）",
  !!doc.querySelector("#thinkSection") && !!doc.querySelector("#boardSection") && !!doc.querySelector("#rawSection"));

// ---------------------------------------------------------------- 打开历史会话
doc.querySelector("#conversations .conv").click();
await sleep(150);
check("点会话后加载出对话", doc.querySelectorAll("#chat .turn").length === 1);
check("历史回答走 Markdown 渲染", doc.querySelector("#chat .answer strong")?.textContent === "12",
  doc.querySelector("#chat .answer")?.textContent ?? "");
check("右栏显示这一轮的工具调用", doc.querySelectorAll("#thinkList .tool").length === 1,
  `${doc.querySelectorAll("#thinkList .tool").length} 行`);
check("右栏原始事件有条目", doc.querySelectorAll("#rawList .event").length >= 1);

// ---------------------------------------------------------------- 新会话
doc.querySelector("#newChat").click();
await sleep(80);
check("新会话回到空状态", !doc.querySelector("#empty").hidden);
check("新会话清空右栏工具", doc.querySelectorAll("#thinkList .tool").length === 0);

// ---------------------------------------------------------------- 提交 + 流式
doc.querySelector("#task").value = "列出 tests 目录";
doc.querySelector("#optPlan").checked = true;
doc.querySelector("#submit").click();
await sleep(120);
const es = sources.at(-1);
check("提交后订阅了事件流", es?.url === "/runs/run-new/events", es?.url ?? "(无)");
const sentBody = bodies.get("POST /runs") ?? {};
check("提交 body 带上了任务和开关",
  sentBody.task === "列出 tests 目录" && sentBody.plan === true &&
  sentBody.include_files === true && sentBody.tasks === true &&
  sentBody.memory === true);
check("提交时带上默认步数 20", sentBody.max_steps === 20, String(sentBody.max_steps));

const before = calls.filter((c) => c === "POST /runs").length;
doc.querySelector("#task").value = "再开一个";
doc.querySelector("#submit").click();
await sleep(80);
check("运行中不允许重复提交",
  calls.filter((c) => c === "POST /runs").length === before);
doc.querySelector("#task").value = "";

// 停止按钮：运行中可见，点了会打 cancel 接口
check("运行中显示「停止」按钮", doc.querySelector("#cancel").hidden === false);
doc.querySelector("#cancel").click();
await sleep(150);
check("点停止会调 cancel 接口",
  calls.some((c) => c === "POST /runs/run-new/cancel"), calls.slice(-2).join(" | "));

const ts = nowSec();
es.emit("start", { kind: "start", ts, data: { task: "列出 tests 目录" } });
es.emit("model_call", { kind: "model_call", ts,
  data: { step: 1, stop_reason: "tool_use", usage: { input_tokens: 120, output_tokens: 30 }, tool_calls: 1 } });
es.emit("tool_call", { kind: "tool_call", ts,
  data: { step: 1, tool: "bash", args: { command: "Get-ChildItem tests" },
          exit_code: 0, failed: false, output: "test_a.py\ntest_b.py\ntest_c.py" } });
await sleep(120);

check("工具调用出现在右栏（不在气泡里）",
  doc.querySelectorAll("#thinkList .tool").length === 1 &&
  !doc.querySelector("#chat details.process"),
  `面板 ${doc.querySelectorAll("#thinkList .tool").length} 行`);
check("右栏显示完整命令", doc.querySelector("#thinkList .tool-command")?.textContent?.includes("Get-ChildItem tests"));
check("右栏显示命令输出预览", doc.querySelector("#thinkList .tool-output")?.textContent?.includes("test_b.py"));
check("右栏工具调用计数更新", doc.querySelector("#thinkMeta").textContent.includes("共 1 次"),
  doc.querySelector("#thinkMeta").textContent);
check("顶栏花费在累加", doc.querySelector("#usage").textContent.includes("入 120"));

// ---- 增量渲染：新事件不重建已有节点 ----
const thinkSectionNode = doc.querySelector("#thinkSection");
const firstToolNode = doc.querySelector("#thinkList .tool");
const rawCountBefore = doc.querySelector("#rawList").children.length;
const processCountBefore = doc.querySelectorAll("#thinkList .tool").length;
es.emit("tool_call", { kind: "tool_call", ts,
  data: { step: 2, tool: "create_task", args: { subject: "跑测试" }, exit_code: 0, failed: false } });
await sleep(120);
check("新事件后右栏还是同一个 DOM 节点", doc.querySelector("#thinkSection") === thinkSectionNode);
check("已有工具行没被重建", doc.querySelector("#thinkList .tool") === firstToolNode);
check("工具行只追加了一个",
  doc.querySelectorAll("#thinkList .tool").length === processCountBefore + 1,
  `${processCountBefore} → ${doc.querySelectorAll("#thinkList .tool").length}`);
check("原始事件只追加了一行",
  doc.querySelector("#rawList").children.length === rawCountBefore + 1,
  `${rawCountBefore} → ${doc.querySelector("#rawList").children.length}`);

// ---- 流式文字 ----
for (const piece of ["正在", "统计文件", "数量…"]) {
  es.emit("assistant_delta", { kind: "assistant_delta", ts, data: { text: piece } });
}
await sleep(120);
check("流式文字边收边显示",
  doc.querySelector("#chat .answer.streaming")?.textContent?.includes("正在统计文件数量"));
check("流式时有光标", !!doc.querySelector("#chat .answer.streaming .cursor"));

// ---------------------------------------------------------------- 结束
const FINAL_MD = [
  "tests 下有 **5** 个文件",
  "",
  "| 目录 | 数量 |",
  "| --- | --- |",
  "| tests | 5 |",
  "",
  "用 `Get-ChildItem` 数：",
  "",
  "- 含子目录",
  "- 不含缓存",
  "",
  "```powershell",
  "Get-ChildItem tests -Recurse",
  "```",
  "",
  "危险内容：<img src=x onerror=alert(1)> [坏链接](javascript:alert(1))",
].join("\n");
es.emit("end", { kind: "end", ts,
  data: { status: "done", result: { ok: true, final_text: FINAL_MD, steps: 2 } } });
await sleep(250);

const answerEl = doc.querySelector("#chat .answer");
check("结束后显示回答", answerEl.textContent.includes("5 个文件"));
check("Markdown 粗体被渲染", answerEl.querySelector("strong")?.textContent === "5");
check("Markdown 表格被渲染", !!answerEl.querySelector("table"));
check("Markdown 列表被渲染", answerEl.querySelectorAll("ul li").length === 2);
check("Markdown 代码块被渲染", !!answerEl.querySelector("pre code"));
check("不再显示原始 Markdown 符号", !answerEl.textContent.includes("**"));
check("HTML 标签按纯文本显示（无注入）",
  !answerEl.querySelector("img") && answerEl.textContent.includes("<img src=x"));
check("危险链接不生成 <a>",
  ![...answerEl.querySelectorAll("a")].some((a) => (a.getAttribute("href") ?? "").startsWith("javascript:")));
check("结果在气泡最后一块",
  doc.querySelector("#chat .msg.assistant .bubble")?.lastElementChild?.classList.contains("answer") === true);
check("气泡里没有工具调用（都在右栏）", !doc.querySelector("#chat .tool-list"));
check("右栏记忆面板拿到条目", doc.querySelectorAll("#memoryList .memory-item").length === 2,
  `${doc.querySelectorAll("#memoryList .memory-item").length} 条`);
check("右栏记忆计数正确", doc.querySelector("#memoryMeta").textContent.includes("2 条"),
  doc.querySelector("#memoryMeta").textContent);
check("结束后右栏任务看板渲染出卡片",
  doc.querySelectorAll("#boardBody .task-card").length === 2,
  `${doc.querySelectorAll("#boardBody .task-card").length} 张`);
check("结束后按钮变成「继续对话」", doc.querySelector("#submit").textContent === "继续对话");
check("结束后状态徽章复位", doc.querySelector("#runStatus").hidden === true);

// ---------------------------------------------------------------- 右栏收起/展开
doc.querySelector("#toggleInspector").click();
await sleep(50);
check("点顶栏按钮能收起右栏",
  doc.querySelector("#appRoot").classList.contains("inspector-hidden"));
doc.querySelector("#toggleInspector").click();
await sleep(50);
check("再点一次能展开右栏",
  !doc.querySelector("#appRoot").classList.contains("inspector-hidden"));

// ---------------------------------------------------------------- 继续对话 + 出错
// 先设令牌：验证 SSE（EventSource 不能带头，只能走 ?token=）
doc.querySelector("#token").value = "s3cret";
doc.querySelector("#token").dispatchEvent(new window.Event("change"));
await sleep(120);
doc.querySelector("#task").value = "再数一下 src";
doc.querySelector("#submit").click();
await sleep(150);
check("继续对话走 followup", calls.includes("POST /runs/run-new/followup"));
check("继续对话订阅新的 run（SSE 带令牌）",
  sources.at(-1)?.url === "/runs/run-followup/events?token=s3cret",
  sources.at(-1)?.url ?? "(无)");

const es2 = sources.at(-1);
es2.emit("end", { kind: "end", ts, data: { status: "error", error: "模型端点挂了" } });
await sleep(200);
check("出错时显示错误文案",
  doc.querySelector("#chat .answer.error")?.textContent.includes("模型端点挂了"));
check("出错后出现「继续跑」", doc.querySelector("#resume").hidden === false);

// ---- 删除会话 ----
doc.querySelector("#conversations .conv-del").click();
await sleep(200);
check("点删除会调删除会话接口",
  calls.some((c) => c === "DELETE /conversations/run-a"), calls.slice(-3).join(" | "));
check("删除后有状态提示", doc.querySelector("#status").textContent.includes("已删除"));

// ---- 取消后的展示 ----
const es3 = sources.at(-1);
es3.emit("end", { kind: "end", ts, data: { status: "done", result: { ok: false, stop_reason: "cancelled", steps: 1 } } });
await sleep(150);
const lastTurn = [...doc.querySelectorAll("#chat .turn")].at(-1);
check("取消后显示「已取消」", lastTurn?.querySelector(".answer")?.textContent?.includes("已取消"),
  lastTurn?.querySelector(".answer")?.textContent ?? "(无)");
check("结束/取消后隐藏「停止」按钮", doc.querySelector("#cancel").hidden === true);

check("页面没有报错", errors.length === 0, errors.slice(0, 3).join(" | "));

for (const c of checks) {
  console.log(`  ${c.ok ? "✅" : "❌"} ${c.name}${c.detail ? "  " + c.detail : ""}`);
}
const failed = checks.filter((c) => !c.ok).length;
console.log(failed === 0 ? `\n界面链路正常（${checks.length} 项）` : `\n${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
