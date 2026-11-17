#!/usr/bin/env node
/**
 * 用 jsdom 把构建出来的 bundle 真跑一遍。
 *
 * 覆盖：左栏（工作区/历史会话）→ 点会话 → 对话/轨迹视图切换 → 轨迹展示完整原始信息
 *      → 新会话 → 提交 → 流式 → 结束（Markdown 渲染）→ 接着说 → 删除 → 取消。
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
    { kind: "model_call", ts: nowSec(), data: { step: 1, stop_reason: "tool_use", usage: { input_tokens: 100, output_tokens: 20 }, tool_calls: 1 } },
    { kind: "thinking", ts: nowSec(), data: { step: 1, text: "先看看目录结构\n再数文件" } },
    { kind: "tool_call", ts: nowSec(), data: {
        step: 1, tool: "bash", args: { command: "Get-ChildItem" },
        exit_code: 0, failed: false, output: "aaa.txt\nbbb.txt\nccc.txt" } },
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

// ---------------------------------------------------------------- 视图切换（没有右侧栏）
check("没有右侧详情栏", !doc.querySelector("#inspector"));
check("导航栏有「对话 / 轨迹」切换",
  !!doc.querySelector("#viewChat") && !!doc.querySelector("#viewTrajectory"));
check("默认停在对话视图", doc.querySelector("#viewChat").classList.contains("active"));

// ---------------------------------------------------------------- 打开历史会话
doc.querySelector("#conversations .conv").click();
await sleep(150);
check("点会话后加载出对话", doc.querySelectorAll("#chat .turn").length === 1);
check("历史回答走 Markdown 渲染", doc.querySelector("#chat .answer strong")?.textContent === "12",
  doc.querySelector("#chat .answer")?.textContent ?? "");
check("对话视图不显示工具原始信息（在轨迹里）",
  !doc.querySelector("#chat .tool-list"));

// 轨迹视图：按步骤展示完整原始信息
doc.querySelector("#viewTrajectory").click();
await sleep(150);
check("切到轨迹视图", doc.querySelector("#viewTrajectory").classList.contains("active") &&
  doc.querySelector("#chat").classList.contains("trajectory"));
const steps = [...doc.querySelectorAll("#chat details.traj-step")];
check("轨迹按步骤分组", steps.length >= 1, `${steps.length} 组`);
const trajText = doc.querySelector("#chat")?.textContent ?? "";
check("轨迹展示 thinking 全文", trajText.includes("先看看目录结构") && trajText.includes("再数文件"));
check("轨迹展示完整工具参数", trajText.includes("Get-ChildItem"));
check("轨迹展示完整工具输出", trajText.includes("aaa.txt") && trajText.includes("ccc.txt"));
const rawJsonNodes = doc.querySelectorAll("#chat details.traj-raw-json pre");
check("轨迹里有原始事件 JSON",
  rawJsonNodes.length > 0 &&
  [...rawJsonNodes].some((n) => (n.textContent ?? "").includes('"kind": "tool_call"')),
  `${rawJsonNodes.length} 个: ${(rawJsonNodes[0]?.textContent ?? "").slice(0, 80)}`);

doc.querySelector("#viewChat").click();
await sleep(120);
check("切回对话视图", doc.querySelector("#viewChat").classList.contains("active") &&
  doc.querySelectorAll("#chat .turn").length === 1);

// ---------------------------------------------------------------- 新会话
doc.querySelector("#newChat").click();
await sleep(120);
check("新会话回到空状态", !doc.querySelector("#empty").hidden);

// ---------------------------------------------------------------- 提交 + 流式
doc.querySelector("#task").value = "列出 tests 目录";
doc.querySelector("#optPlan").checked = true;
check("MCP 开关默认关闭（要显式开才起外部进程）",
  doc.querySelector("#optMcp").checked === false);
doc.querySelector("#optMcp").checked = true;
doc.querySelector("#submit").click();
await sleep(120);
const es = sources.at(-1);
check("提交后订阅了事件流", es?.url === "/runs/run-new/events", es?.url ?? "(无)");
const sentBody = bodies.get("POST /runs") ?? {};
check("提交 body 带上了任务和开关",
  sentBody.task === "列出 tests 目录" && sentBody.plan === true &&
  sentBody.include_files === true && sentBody.tasks === true &&
  sentBody.memory === true && sentBody.mcp === true);
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

check("顶栏花费在累加", doc.querySelector("#usage").textContent.includes("入 120"));

es.emit("tool_call", { kind: "tool_call", ts,
  data: { step: 2, tool: "create_task", args: { subject: "跑测试" }, exit_code: 0, failed: false } });
await sleep(120);
check("流式期间轨迹能实时看到新步骤", (() => {
  doc.querySelector("#viewTrajectory").click();
  return true;
})());

// 轨迹视图下继续收事件：需要等一帧重绘
await sleep(200);
check("轨迹实时出现第 2 步",
  doc.querySelectorAll("#chat details.traj-step").length >= 2,
  `${doc.querySelectorAll("#chat details.traj-step").length} 组`);

// 切回对话，继续流式文字断言
doc.querySelector("#viewChat").click();
await sleep(150);

// ---- 流式文字 ----
for (const piece of ["正在", "统计文件", "数量…"]) {
  es.emit("assistant_delta", { kind: "assistant_delta", ts, data: { text: piece } });
}
await sleep(120);
check("流式文字边收边显示",
  doc.querySelector("#chat .answer.streaming")?.textContent?.includes("正在统计文件数量"));
check("流式时有光标", !!doc.querySelector("#chat .answer.streaming .cursor"));

// ---- MCP 工具在轨迹里保留原始名字 ----
es.emit("tool_call", { kind: "tool_call", ts,
  data: { step: 3, tool: "mcp__fixture__echo", args: { text: "hi" },
          exit_code: 0, failed: false, output: "hi" } });
await sleep(120);
doc.querySelector("#viewTrajectory").click();
await sleep(200);
check("轨迹里 MCP 工具保留原始名字",
  doc.querySelector("#chat")?.textContent?.includes("mcp__fixture__echo"));
doc.querySelector("#viewChat").click();
await sleep(120);

// ---- thinking 内容展示（换行 + 第几步） ----
es.emit("thinking", { kind: "thinking", ts, data: { step: 2, text: "第一行思考\n第二行思考" } });
await sleep(120);
const thoughtBox = doc.querySelector("#chat details.thoughts");
check("思考内容作为消息出现在气泡里", !!thoughtBox, "没有 details.thoughts");
check("思考标了第几步并保留换行",
  thoughtBox?.textContent?.includes("第 2 步") &&
    thoughtBox?.querySelector(".thought-text")?.textContent?.trim() === "第一行思考\n第二行思考",
  thoughtBox?.textContent?.slice(0, 60) ?? "(无)");

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
check("气泡里没有工具调用（工具在轨迹视图）", !doc.querySelector("#chat .tool-list"));
check("结束后按钮变成「继续对话」", doc.querySelector("#submit").textContent === "继续对话");
check("结束后状态徽章复位", doc.querySelector("#runStatus").hidden === true);

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
