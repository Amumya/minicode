#!/usr/bin/env node
/**
 * 使用 jsdom 执行构建产物并验证界面链路。
 *
 * 覆盖：左栏（工作区/历史会话）→ 打开会话 → 对话/轨迹视图切换 → 轨迹事件展示
 *      → 新建会话 → 提交 → 流式输出 → 结束（Markdown 渲染）→ followup → 删除 → 取消。
 *
 * 用法（在 web/ 目录下）：npm install --no-save jsdom && node uicheck.mjs
 */

import { readFileSync, readdirSync, statSync } from "node:fs";
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
  result: { ok: true, final_text: "一共 **12** 个文件", steps: 1, stop_reason: "end_turn" },
  created_at: nowSec() - 3600,
  event_count: 2,
  events: [
    { kind: "start", ts: nowSec(), data: { task: "数一下文件数量", model: "m", tools: ["bash"], hooks: ["policy"], system: "你是 minicode 助手。\n规则一：先读文件。", max_steps: 1 } },
    { kind: "model_call", ts: nowSec(), data: { step: 1, stop_reason: "tool_use", usage: { input_tokens: 100, output_tokens: 20 }, tool_calls: 1 } },
    { kind: "thinking", ts: nowSec(), data: { step: 1, text: "先看看目录结构\n再数文件" } },
    { kind: "tool_call", ts: nowSec(), data: {
        step: 1, tool: "bash", args: { command: "Get-ChildItem" },
        exit_code: 0, failed: false, output: "aaa.txt\nbbb.txt\nccc.txt" } },
      { kind: "tool_call", ts: nowSec(), data: {
          step: 2, tool: "task", args: { prompt: "数一下 tests 目录" },
          exit_code: 0, failed: false, output: "19 个测试文件",
          meta: { subagent_ok: true, subagent_steps: 2, subagent_tokens: 40, subagent_tool_calls: 3 } } },
      { kind: "tool_call", ts: nowSec(), data: {
          step: 3, tool: "team", args: { task: "审查登录模块" },
          exit_code: 0, failed: false, output: "成员结论",
          meta: { team: { task: "审查登录模块", members: [
            { name: "alice", role: "安全", ok: true, steps: 4, tokens: 88, tool_calls: 5, output: "无高危问题", error: "" },
            { name: "bob", role: "性能", ok: false, steps: 2, tokens: 40, tool_calls: 1, output: "", error: "超时" },
          ] } } } },
      { kind: "tool_call", ts: nowSec(), data: {
          step: 11, tool: "team", args: { task: "审查支付模块" },
          exit_code: 0, failed: false, output: "成员结论",
          meta: { team: { task: "审查支付模块", members: [
            { name: "carol", role: "合规", ok: true, steps: 3, tokens: 60, tool_calls: 3, output: "合规通过", error: "" },
            { name: "dave", role: "风控", ok: true, steps: 4, tokens: 70, tool_calls: 4, output: "无风险", error: "" },
          ] } } } },
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

  // 默认值：六个开关全选；步数固定由后端默认 20，前端不提供输入。
const defaultSwitches = ["#optFiles", "#optPlan", "#optSkills", "#optCompact", "#optTasks", "#optMemory"];
check("五个开关默认全选", defaultSwitches.every((sel) => doc.querySelector(sel).checked),
  defaultSwitches.map((sel) => `${sel}=${doc.querySelector(sel).checked}`).join(" "));
  check("页面不再提供步数输入框", doc.querySelector("#maxSteps") === null);

// ---------------------------------------------------------------- 视图切换（没有右侧栏）
check("没有右侧详情栏", !doc.querySelector("#inspector"));
check("导航栏有「对话 / 日志」切换",
  !!doc.querySelector("#viewChat") && !!doc.querySelector("#viewTrajectory"));
check("默认停在对话视图", doc.querySelector("#viewChat").classList.contains("active"));
check("导航栏有「编队」切换", !!doc.querySelector("#viewTeam"));
check("子代理/编队开关默认关闭",
  doc.querySelector("#optSubagent").checked === false && doc.querySelector("#optTeam").checked === false);
doc.querySelector("#viewTeam").click();
await sleep(200);
check("切到编队视图", doc.querySelector("#viewTeam").classList.contains("active") &&
  doc.querySelector("#chat").classList.contains("team") &&
  (doc.querySelector("#chat")?.textContent ?? "").includes("编队"));
doc.querySelector("#viewChat").click();
await sleep(100);

// ---------------------------------------------------------------- 打开历史会话
doc.querySelector("#conversations .conv").click();
await sleep(150);
check("点会话后加载出对话", doc.querySelectorAll("#chat .turn").length === 1);
check("历史回答走 Markdown 渲染", doc.querySelector("#chat .answer strong")?.textContent === "12",
  doc.querySelector("#chat .answer")?.textContent ?? "");
  check("子代理不再用下拉框", doc.querySelector("#subagentPanel") === null &&
    doc.querySelector("#subagentList") === null);

  // 编队：多 Agent 仪表盘（Agent 网络 + 协作 + 任务板 + 事件）
  doc.querySelector("#viewTeam").click();
  await sleep(200);
  check("编队是多 Agent 仪表盘", !!doc.querySelector("#chat .team-dashboard") &&
    !!doc.querySelector("#chat .agent-network") && !!doc.querySelector("#chat .team-hero"));
  const agentCards = [...doc.querySelectorAll("#chat .agent-card")];
  check("Agent 网络包含主控/成员/子代理", agentCards.length >= 4, `${agentCards.length} 个 Agent`);
  const netText = doc.querySelector("#chat .agent-network")?.textContent ?? "";
  check("Agent 卡片显示任务与 Token 指标",
    netText.includes("主控") && netText.includes("alice") && netText.includes("Token"));
  const teamGroups = [...doc.querySelectorAll("#chat .team-group")]
    .filter((group) => (group.textContent ?? "").includes("编队 "));
  check("多个编队按组渲染", teamGroups.length === 2, `${teamGroups.length} 组`);
  check("每个编队只有一个 Lead 且不与成员混排",
    teamGroups.every((group) =>
      group.querySelectorAll(".agent-card.lead").length === 1 &&
      group.querySelectorAll(".agent-lead-row .agent-card.lead").length === 1 &&
      group.querySelectorAll(".agent-grid .agent-card").length === 2));
  const taskRows = [...doc.querySelectorAll("#chat .task-row")];
  check("协作任务板列出成员", taskRows.length >= 4, `${taskRows.length} 行`);
  const eventRows = [...doc.querySelectorAll("#chat .event-row:not(.event-head)")];
  check("事件检查列出调用记录", eventRows.length >= 2, `${eventRows.length} 条`);
  const aliceCard = agentCards.find((card) => card.textContent?.includes("alice"));
  aliceCard?.click();
  await sleep(120);
  check("点击 Agent 卡片联动协作详情",
    (doc.querySelector("#chat .agent-detail")?.textContent ?? "").includes("alice"));
  doc.querySelector("#viewChat").click();
  await sleep(100);
check("对话视图不显示工具原始信息（在日志里）",
  !doc.querySelector("#chat .tool-list"));
check("对话视图不输出 thinking（没有思考块）",
  !doc.querySelector("#chat details.thoughts") &&
  !(doc.querySelector("#chat")?.textContent ?? "").includes("先看看目录结构"));

// 日志视图：追加式全事件日志
doc.querySelector("#viewTrajectory").click();
await sleep(200);
check("切到轨迹视图", doc.querySelector("#viewTrajectory").classList.contains("active") &&
  doc.querySelector("#chat").classList.contains("trajectory"));
const entries = [...doc.querySelectorAll("#chat .traj-entry")];
check("轨迹是追加式事件日志", entries.length >= 3, `${entries.length} 条事件`);
  const trajText = doc.querySelector("#chat")?.textContent ?? "";
  check("轨迹列出系统提示入口", trajText.includes("系统提示（完整，点击展开）"));
  check("轨迹展示 thinking 原文", trajText.includes("先看看目录结构") && trajText.includes("再数文件"));
  check("轨迹按步骤分隔", !!doc.querySelector("#chat .traj-step-divider"));
  const toolSections = [...doc.querySelectorAll("#chat details.traj-tool-section")];
  check("工具参数/输出/META 默认收起",
    toolSections.length >= 2 && toolSections.every((d) => !d.open),
    `${toolSections.length} 个，展开 ${toolSections.filter((d) => d.open).length} 个`);
  check("收起时不为工具内容建 DOM",
    !doc.querySelector("#chat details.traj-tool-section pre"));
  const rawJsonDetails = [...doc.querySelectorAll("#chat details.traj-event-json")];
  check("每条事件都有原始事件 JSON 入口", rawJsonDetails.length >= 3, `${rawJsonDetails.length} 个`);
  check("收起时不为原始 JSON 建 DOM",
    !doc.querySelector("#chat details.traj-event-json pre"));

  const openDetails = (d) => { d.open = true; d.dispatchEvent(new window.Event("toggle")); };
  const systemDetails = doc.querySelector("#chat details.traj-system");
  if (systemDetails) openDetails(systemDetails);
  for (const d of toolSections) openDetails(d);
  for (const d of rawJsonDetails) openDetails(d);
  await sleep(120);
  check("展开后能看到系统提示全文",
    (doc.querySelector("#chat details.traj-system")?.textContent ?? "").includes("规则一"));
  const toolText = toolSections.map((d) => d.textContent ?? "").join(" ");
  check("展开后能看到完整工具参数", toolText.includes("Get-ChildItem"));
  check("展开后能看到完整工具输出", toolText.includes("aaa.txt") && toolText.includes("ccc.txt"));
  const rawText = rawJsonDetails.map((d) => d.textContent ?? "").join(" ");
  check("原始事件 JSON 展开后是完整内容",
    rawText.includes('"kind": "tool_call"'));

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
doc.querySelector("#optSubagent").checked = true;
doc.querySelector("#optTeam").checked = true;
doc.querySelector("#submit").click();
await sleep(120);
const es = sources.at(-1);
check("提交后订阅了事件流", es?.url === "/runs/run-new/events", es?.url ?? "(无)");
  check("提交后立刻显示运行中活动行（在会话最后）",
    (doc.querySelector("#chat .live-footer-text")?.textContent ?? "").includes("等待模型回复") &&
    doc.querySelector("#chat").lastElementChild?.classList.contains("live-footer") === true,
    doc.querySelector("#chat .live-footer-text")?.textContent ?? "(无)");
  check("计时器立刻开始",
    /^\d{2}:\d{2}$/.test(doc.querySelector("#chat .live-timer")?.textContent ?? ""),
    doc.querySelector("#chat .live-timer")?.textContent ?? "(无)");
const sentBody = bodies.get("POST /runs") ?? {};
check("提交 body 带上了任务和开关",
  sentBody.task === "列出 tests 目录" && sentBody.plan === true &&
  sentBody.include_files === true && sentBody.tasks === true &&
  sentBody.memory === true && sentBody.mcp === true);
check("提交 body 带上了子代理/编队开关",
  sentBody.subagent === true && sentBody.team === true,
  `subagent=${sentBody.subagent} team=${sentBody.team}`);
check("提交时不携带 max_steps（由后端默认 0 = 不限）", sentBody.max_steps === undefined,
  String(sentBody.max_steps));

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
  check("运行中显示当前步骤和工具",
    (doc.querySelector("#chat .live-footer-text")?.textContent ?? "").includes("第 2 步") &&
    (doc.querySelector("#chat .live-footer-text")?.textContent ?? "").includes("create_task"),
    doc.querySelector("#chat .live-footer-text")?.textContent ?? "(无)");

  es.emit("tool_call", { kind: "tool_call", ts,
    data: { step: 2, tool: "todo_write", exit_code: 0, failed: false,
            args: { todos: [{ content: "跑测试", status: "in_progress" }, { content: "出报告", status: "pending" }] } } });
  await sleep(150);
  check("运行中计划列表自动展开",
    doc.querySelector("#chat details.plan")?.open === true &&
    (doc.querySelector("#chat details.plan")?.textContent ?? "").includes("跑测试"),
    doc.querySelector("#chat details.plan")?.textContent ?? "(无)");
  check("运行中活动行跟着切换工具",
    (doc.querySelector("#chat .live-footer-text")?.textContent ?? "").includes("todo_write"),
    doc.querySelector("#chat .live-footer-text")?.textContent ?? "(无)");
  check("顶栏也显示当前活动（轨迹/对话都能看到）",
    (doc.querySelector("#runStatus")?.textContent ?? "").includes("第 2 步"),
    doc.querySelector("#runStatus")?.textContent ?? "(无)");

doc.querySelector("#viewTrajectory").click();
await sleep(250);
const firstTrajEntry = doc.querySelector("#chat .traj-entry");
const entriesBefore = doc.querySelectorAll("#chat .traj-entry").length;
check("流式期间轨迹能看到第 2 步",
  doc.querySelector("#chat")?.textContent?.includes("第 2 步"),
  `当前 ${entriesBefore} 条事件`);

es.emit("tool_call", { kind: "tool_call", ts,
  data: { step: 3, tool: "bash", args: { command: "echo live" }, exit_code: 0, failed: false, output: "live" } });
await sleep(250);
check("轨迹实时追加新事件",
  doc.querySelectorAll("#chat .traj-entry").length > entriesBefore,
  `${entriesBefore} → ${doc.querySelectorAll("#chat .traj-entry").length}`);
check("追加式：已有日志节点没被重建",
  doc.querySelector("#chat .traj-entry") === firstTrajEntry);

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
const liveUserBubble = [...doc.querySelectorAll("#chat .msg.user .bubble")].at(-1);
const liveAssistantBubble = [...doc.querySelectorAll("#chat .msg.assistant .bubble")].at(-1);
check("流式输出写在助手气泡（不是「你」的气泡）",
  !(liveUserBubble?.textContent ?? "").includes("正在统计") &&
  (liveAssistantBubble?.textContent ?? "").includes("正在统计文件数量"),
  `user=${(liveUserBubble?.textContent ?? "").slice(0, 30)}`);

  // ---- 不同步的正文要分段（空一行），不能粘在一起 ----
  es.emit("assistant_delta", { kind: "assistant_delta", ts, data: { step: 2, text: "第一段话" } });
  await sleep(80);
  es.emit("assistant_delta", { kind: "assistant_delta", ts, data: { step: 3, text: "第二段话" } });
  await sleep(150);
  const liveAnswerEl = doc.querySelector("#chat .answer.streaming");
  const liveParagraphs = [...(liveAnswerEl?.querySelectorAll("p") ?? [])].map((p) => p.textContent ?? "");
  check("不同步的正文之间分段（空一行）",
    liveParagraphs.includes("第一段话") && liveParagraphs.includes("第二段话"),
    `p=${liveParagraphs.join(" | ")}`);

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

// ---- thinking 只进轨迹，不进对话气泡 ----
es.emit("thinking", { kind: "thinking", ts, data: { step: 2, text: "第一行思考\n第二行思考" } });
await sleep(150);
check("对话气泡不渲染 thinking",
  !doc.querySelector("#chat details.thoughts") &&
  !(doc.querySelector("#chat")?.textContent ?? "").includes("第一行思考"));

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
doc.querySelector("#viewTrajectory").click();
await sleep(200);
check("结束后轨迹里保留 thinking 原文",
  (doc.querySelector("#chat")?.textContent ?? "").includes("第一行思考"));
doc.querySelector("#viewChat").click();
await sleep(150);
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
check("取消后显示「已取消」提示",
    lastTurn?.querySelector(".answer-warning")?.textContent?.includes("已取消"),
    lastTurn?.querySelector(".answer-warning")?.textContent ?? "(无)");
check("结束/取消后隐藏「停止」按钮", doc.querySelector("#cancel").hidden === true);

check("页面没有报错", errors.length === 0, errors.slice(0, 3).join(" | "));

for (const c of checks) {
  console.log(`  ${c.ok ? "✅" : "❌"} ${c.name}${c.detail ? "  " + c.detail : ""}`);
}
const failed = checks.filter((c) => !c.ok).length;
console.log(failed === 0 ? `\n界面链路正常（${checks.length} 项）` : `\n${failed} 项失败`);
process.exit(failed === 0 ? 0 : 1);
