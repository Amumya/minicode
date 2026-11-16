/**
 * 界面接线：左栏（工作区 + 历史会话）+ 右侧对话流。
 *
 * 分层：纯逻辑在 chat.ts / derive.ts / format.ts（都有单测），
 * 接口在 api.ts，这里只负责"把状态画到 DOM 上"和"把用户操作变成请求"。
 */

import "./styles.css";
import {
  api,
  setAccessToken,
  subscribeRun,
  type Conversation,
  type RunRow,
  type Workspace,
} from "./api.ts";
import {
  buildTurns,
  mergeDeltaEvents,
  groupByDay,
  shortTitle,
  statusTone,
  type Turn,
} from "./chat.ts";
import { describeEvent } from "./format.ts";
import {
  buildTaskBoard,
  formatUsage,
  latestPlan,
  summarizeUsage,
  type PlanItem,
  type TaskItem,
} from "./derive.ts";
import { describeToolCall, type ToolCallView } from "./chat.ts";
import { renderMarkdown } from "./markdown.ts";
import type { AgentEvent } from "./format.ts";

/* ------------------------------------------------------------------ 小工具 */

type Child = Node | string | null | undefined | false;

function h<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  props: Partial<HTMLElementTagNameMap[K]> & { class?: string; dataset?: Record<string, string> } = {},
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null) continue;
    if (key === "class") node.className = String(value);
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key === "hidden") node.hidden = Boolean(value);
    else (node as unknown as Record<string, unknown>)[key] = value;
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.append(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

function el<T extends HTMLElement>(id: string): T {
  const node = document.getElementById(id);
  if (!node) throw new Error(`找不到元素 #${id}`);
  return node as T;
}

const LAST_CONVERSATION_KEY = "minicode:conversation";
const LAST_WORKSPACE_KEY = "minicode:workspace";

function remember(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* 隐私模式不可用，忽略 */
  }
}
function recall(key: string): string {
  try {
    return window.localStorage.getItem(key) ?? "";
  } catch {
    return "";
  }
}
function forget(key: string): void {
  try {
    window.localStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}

/* ------------------------------------------------------------------ DOM */

const sidebar = {
  workspace: el<HTMLSelectElement>("workspace"),
  custom: el<HTMLInputElement>("workspaceCustom"),
  refresh: el<HTMLButtonElement>("refreshWs"),
  newChat: el<HTMLButtonElement>("newChat"),
  conversations: el<HTMLElement>("conversations"),
  token: el<HTMLInputElement>("token"),
  store: el<HTMLElement>("storeInfo"),
};

const main = {
  title: el<HTMLElement>("title"),
  status: el<HTMLElement>("runStatus"),
  usage: el<HTMLElement>("usage"),
  chat: el<HTMLElement>("chat"),
  empty: el<HTMLElement>("empty"),
  task: el<HTMLTextAreaElement>("task"),
  submit: el<HTMLButtonElement>("submit"),
  resume: el<HTMLButtonElement>("resume"),
  cancel: el<HTMLButtonElement>("cancel"),
  statusLine: el<HTMLElement>("status"),
  model: el<HTMLSelectElement>("model"),
  maxSteps: el<HTMLInputElement>("maxSteps"),
  optFiles: el<HTMLInputElement>("optFiles"),
  optPlan: el<HTMLInputElement>("optPlan"),
  optSkills: el<HTMLInputElement>("optSkills"),
  optCompact: el<HTMLInputElement>("optCompact"),
  optTasks: el<HTMLInputElement>("optTasks"),
  optMemory: el<HTMLInputElement>("optMemory"),
  optMcp: el<HTMLInputElement>("optMcp"),
};

/* ------------------------------------------------------------------ 展开状态

为什么需要：每次事件都会重画整个对话区，<details> 的 open 状态会被重建掉。
用户展开"过程"想看细节，来了一条新事件就被关回去 —— 很烦。
把展开状态按"轮次+区块"记在内存里，重画后恢复。
*/

const detailsState = new Map<string, boolean>();

function detailsBlock(
  key: string,
  cls: string,
  summaryText: string,
  build: () => HTMLElement | null,
  defaultOpen = false,
): HTMLElement {
  const node = h("details", { class: cls });
  const initialOpen = detailsState.has(key) ? detailsState.get(key)! : defaultOpen;
  node.open = initialOpen;
  // 只记录"和初始状态不同"的切换。
  // 直接监听 toggle 会把它自己的初始设置也当成用户操作（代码设 open=true 同样触发 toggle），
  // 结果"结束后自动收起"永远不生效 —— 这是实测踩到的。
  node.addEventListener("toggle", () => {
    if (node.open !== initialOpen) detailsState.set(key, node.open);
  });
  node.append(h("summary", {}, summaryText));
  const body = build();
  if (body) node.append(body);
  return node;
}

/* ------------------------------------------------------------------ 状态 */

let workspaces: Workspace[] = [];
let conversations: Conversation[] = [];
/** 当前会话的所有 run（按时间排好），live run 也在里面 */
let runs: RunRow[] = [];
let conversationRoot = "";
/** 正在流式接收的那个 run */
let liveRun: RunRow | null = null;
let liveEvents: AgentEvent[] = [];
let closeStream: (() => void) | null = null;
let renderQueued = false;

function currentConversation(): Conversation | null {
  return conversations.find((c) => c.root_id === conversationRoot) ?? null;
}

function selectedWorkspace(): string {
  const custom = sidebar.custom.value.trim();
  if (custom) return custom;
  return sidebar.workspace.value;
}

function setStatus(text: string, tone: "muted" | "ok" | "error" = "muted"): void {
  main.statusLine.textContent = text;
  main.statusLine.className = tone === "muted" ? "muted" : tone;
}

function isBusy(): boolean {
  return runs.some((r) => r.status === "running" || r.status === "queued");
}

/* ------------------------------------------------------------------ 左栏 */

function renderWorkspaces(): void {
  sidebar.workspace.replaceChildren();
  for (const ws of workspaces) {
    const option = h("option", { value: ws.path });
    option.textContent = `${ws.name}${ws.is_default ? "（默认）" : ""}${ws.exists ? "" : "（不存在）"}`;
    option.title = ws.path;
    sidebar.workspace.append(option);
  }
  const saved = recall(LAST_WORKSPACE_KEY);
  const pick = workspaces.find((w) => w.path === saved) ?? workspaces.find((w) => w.is_default) ?? workspaces[0];
  if (pick) sidebar.workspace.value = pick.path;
}

function renderConversations(): void {
  sidebar.conversations.replaceChildren();
  if (conversations.length === 0) {
    sidebar.conversations.append(h("p", { class: "muted hint" }, "暂无会话"));
    return;
  }
  for (const group of groupByDay(conversations)) {
    sidebar.conversations.append(h("div", { class: "group-label" }, group.label));
    for (const conv of group.items) {
      // 多包一层：会话按钮和删除按钮是兄弟节点（button 里不能再嵌 button）
      const row = h("div", { class: "conv-row" });
      const item = h("button", { class: "conv", dataset: { root: conv.root_id } });
      if (conv.root_id === conversationRoot) {
        item.classList.add("active");
        item.setAttribute("aria-current", "true");
      }
      const tone = statusTone(conv.status);
      const dot = h("span", { class: `dot tone-${tone}` });
      dot.setAttribute("aria-label", `状态：${conv.status}`);
      const title = h("span", { class: "conv-title" }, shortTitle(conv.title));
      const meta = h("span", { class: "conv-meta" }, conv.turns && conv.turns > 1 ? `${conv.turns} 轮` : "");
      item.append(dot, title, meta);
      item.title = conv.title;
      item.addEventListener("click", () => void openConversation(conv.root_id));

      const remove = h("button", { class: "conv-del", title: "删除这个会话" }, "×");
      remove.setAttribute("aria-label", `删除会话：${conv.title}`);
      remove.addEventListener("click", (event) => {
        event.stopPropagation();
        void deleteConversation(conv);
      });
      row.append(item, remove);
      sidebar.conversations.append(row);
    }
  }
}

/* ------------------------------------------------------------------ 对话区 */

/** 一条工具调用对应的 DOM。实时面板和历史回看共用。 */
function toolRow(tool: ToolCallView): HTMLElement {
  const li = h("li", { class: tool.ok ? "tool" : "tool failed" });
  const head = h("div", { class: "tool-head" });
  head.append(h("code", { class: "tool-name" }, tool.tool));
  if (tool.source) {
    head.append(h("span", { class: "tool-source", title: `来自 MCP server：${tool.mcpServer}` },
      tool.source));
  }
  if (tool.exitCode !== undefined) {
    head.append(
      h("span", { class: `tool-exit ${tool.ok ? "ok" : "error"}` },
        tool.exitCode === 0 ? "exit 0" : `exit ${tool.exitCode}`),
    );
  }
  li.append(head);
  if (tool.summary) {
    const cmd = h("pre", { class: "tool-command" }, tool.summary);
    cmd.title = tool.summary;
    li.append(cmd);
  }
  if (tool.output) li.append(h("pre", { class: "tool-output" }, tool.output));
  return li;
}

/** 一条原始事件对应的 DOM。 */
function eventRow(ev: AgentEvent): HTMLElement {
  const d = describeEvent(ev);
  const row = h("div", { class: `event tone-${d.tone}` });
  row.append(h("span", { class: "event-label" }, d.label));
  row.append(h("span", { class: "event-detail" }, d.detail));
  return row;
}

function renderPlan(turn: Turn, defaultOpen: boolean): HTMLElement | null {
  if (!turn.plan || turn.plan.length === 0) return null;
  const done = turn.plan.filter((p) => p.status === "completed").length;
  return detailsBlock(`${turn.runId}:plan`, "plan", `计划：${done}/${turn.plan.length} 完成`, () => {
    const ol = h("ol", { class: "plan-list" });
    for (const item of turn.plan ?? []) {
      const li = h("li", { class: `plan-item status-${item.status}` });
      li.textContent = item.content;
      if (item.blockedBy?.length) {
        li.append(h("span", { class: "blocked-by" }, `等待 ${item.blockedBy.join(",")}`));
      }
      ol.append(li);
    }
    return ol;
  }, defaultOpen);
}

/** 气泡：用户消息 + 计划 + 用量 + 结果。思考/看板/原始事件都在右栏。 */
function renderTurn(
  turn: Turn,
  live: boolean,
  defaultOpen = false,
): HTMLElement {
  const wrap = h("div", { class: `turn${live ? " live" : ""}` });

  const user = h("div", { class: "msg user" });
  user.append(h("div", { class: "avatar" }, "你"));
  user.append(h("div", { class: "bubble" }, turn.task));
  wrap.append(user);

  const assistant = h("div", { class: "msg assistant" });
  assistant.append(h("div", { class: "avatar" }, "m"));
  const bubble = h("div", { class: "bubble" });

  const plan = renderPlan(turn, defaultOpen);
  if (plan) bubble.append(plan);

  if (turn.usage.steps > 0) {
    bubble.append(h("div", { class: "turn-usage muted" }, formatUsage(turn.usage)));
  }

  if (turn.error) {
    bubble.append(h("div", { class: "answer error" }, `出错：${turn.error}`));
  } else if (turn.answer) {
    bubble.append(h("div", { class: "answer-label" }, "结果"));
    const answerNode = h("div", { class: "answer markdown" });
    answerNode.append(renderMarkdown(turn.answer));
    bubble.append(answerNode);
  } else if (!turn.ok && turn.status === "error") {
    bubble.append(h("div", { class: "answer error" }, "执行失败（详情见右侧面板）"));
  } else if (!turn.ok && turn.status === "done") {
    const text = turn.stopReason === "cancelled"
      ? "已取消"
      : "未完成（步数用尽或已中断）";
    bubble.append(h("div", { class: "answer muted" }, text));
  }

  assistant.append(bubble);
  wrap.append(assistant);
  return wrap;
}

/* ------------------------------------------------------------------ 右侧详情面板 */

const panel = {
  root: el<HTMLElement>("inspector"),
  appRoot: el<HTMLElement>("appRoot"),
  toggle: el<HTMLButtonElement>("toggleInspector"),
  close: el<HTMLButtonElement>("closeInspector"),
  thinkSection: el<HTMLDetailsElement>("thinkSection"),
  thinkList: el<HTMLUListElement>("thinkList"),
  thinkMeta: el<HTMLElement>("thinkMeta"),
  boardSection: el<HTMLDetailsElement>("boardSection"),
  boardBody: el<HTMLElement>("boardBody"),
  boardMeta: el<HTMLElement>("boardMeta"),
  memorySection: el<HTMLDetailsElement>("memorySection"),
  memoryList: el<HTMLElement>("memoryList"),
  memoryMeta: el<HTMLElement>("memoryMeta"),
  rawSection: el<HTMLDetailsElement>("rawSection"),
  rawList: el<HTMLElement>("rawList"),
  rawMeta: el<HTMLElement>("rawMeta"),
};

/** 每个 run 的任务看板只拉一次（历史回看时反复切会话不必重复请求）。 */
const boardCache = new Map<string, { enabled: boolean; tasks: TaskItem[]; progress: { done: number; total: number } } | null>();

function renderBoardInto(
  data: { enabled: boolean; tasks: TaskItem[]; progress: { done: number; total: number } } | null,
): void {
  const body = panel.boardBody;
  body.replaceChildren();
  panel.boardMeta.textContent = "";
  if (!data) {
    body.append(h("p", { class: "muted" }, "任务看板加载失败"));
    return;
  }
  if (!data.enabled || data.tasks.length === 0) {
    body.append(h("p", { class: "muted" }, "本轮未启用任务图"));
    return;
  }
  const board = buildTaskBoard(data.tasks);
  panel.boardMeta.textContent =
    `${data.progress.done}/${data.progress.total} 完成 · 可开工 ${board.ready.length}`;
  body.append(
    h("div", { class: "board-meta muted" },
      `阻塞 ${board.blocked.length} · 进行中 ${board.inProgress.length}`),
  );
  const grid = h("div", { class: "board-grid" });
  for (const task of data.tasks) {
    const card = h("div", { class: `task-card status-${task.status}` });
    card.append(h("code", { class: "task-id" }, task.id.replace("task_", "")));
    card.append(h("span", { class: "task-subject" }, task.subject));
    const waiting = (task.blockedBy ?? []).filter((dep) => {
      const found = data.tasks.find((x) => x.id === dep);
      return !found || found.status !== "completed";
    });
    card.append(
      h("span", { class: "task-meta muted" },
        [task.status, task.owner ? `owner ${task.owner}` : "",
         waiting.length ? `等 ${waiting.map((w) => w.replace("task_", "")).join(",")}` : ""]
          .filter(Boolean).join(" · ")),
    );
    grid.append(card);
  }
  body.append(grid);
}

async function loadBoard(runId: string): Promise<void> {
  if (boardCache.has(runId)) {
    renderBoardInto(boardCache.get(runId) ?? null);
    return;
  }
  panel.boardBody.replaceChildren(h("p", { class: "muted" }, "加载中…"));
  try {
    const data = await api.runTasks(runId);
    boardCache.set(runId, data);
    renderBoardInto(data);
  } catch {
    boardCache.set(runId, null);
    renderBoardInto(null);
  }
}

function panelTools(turn: Turn): ToolCallView[] {
  return turn.tools;
}

/** 把右栏刷新成"某一轮"的详情。run 结束、切会话、新会话都会调它。 */
function renderInspector(turn: Turn | null, events: AgentEvent[]): void {
  if (!turn) {
    panel.thinkList.replaceChildren(h("li", { class: "muted" }, "暂无工具调用"));
    panel.thinkMeta.textContent = "";
    panel.rawList.replaceChildren();
    panel.rawMeta.textContent = "";
    renderBoardInto(null);
    panel.boardBody.replaceChildren(h("p", { class: "muted" }, "本轮未启用任务图"));
    void renderMemory(undefined, false);
    return;
  }

  const tools = panelTools(turn);
  panel.thinkList.replaceChildren();
  if (tools.length === 0) {
    panel.thinkList.append(h("li", { class: "muted" }, "本轮无工具调用"));
  } else {
    for (const tool of tools) panel.thinkList.append(toolRow(tool));
  }
  const failed = tools.filter((t) => !t.ok).length;
  panel.thinkMeta.textContent =
    `共 ${tools.length} 次` +
    (failed ? ` · 失败 ${failed}` : "") +
    (turn.denied ? ` · 已拒绝 ${turn.denied}` : "");

  const merged = mergeDeltaEvents(events);
  panel.rawList.replaceChildren();
  for (const ev of merged) panel.rawList.append(eventRow(ev));
  panel.rawMeta.textContent = `${merged.length} 条`;

  void loadBoard(turn.runId);
  const run = runs.find((r) => r.id === turn.runId);
  void renderMemory(run?.workspace, run?.memory === true);
}

/** 右栏的"记忆"分区：显示这个工作区里存下的长期笔记。 */
async function renderMemory(workspace: string | undefined, enabled: boolean): Promise<void> {
  const box = panel.memoryList;
  panel.memoryMeta.textContent = "";
  if (!enabled) {
    box.replaceChildren(h("p", { class: "muted" }, "这一轮没有启用记忆"));
    return;
  }
  if (!workspace) {
    box.replaceChildren(h("p", { class: "muted" }, "没有工作区信息"));
    return;
  }
  box.replaceChildren(h("p", { class: "muted" }, "加载中…"));
  try {
    const data = await api.memory(workspace);
    if (data.entries.length === 0) {
      box.replaceChildren(h("p", { class: "muted" }, "还没有记忆（让它在对话里记住点什么）"));
      panel.memoryMeta.textContent = "0 条";
      return;
    }
    panel.memoryMeta.textContent = `${data.total} 条`;
    const list = h("ul", { class: "memory-items" });
    for (const entry of data.entries) {
      const li = h("li", { class: "memory-item" });
      li.append(h("span", { class: "memory-text" }, entry.text));
      if (entry.tags.length) {
        li.append(h("span", { class: "memory-tags muted" }, entry.tags.join(" · ")));
      }
      list.append(li);
    }
    box.replaceChildren(list);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    box.replaceChildren(h("p", { class: "muted" }, `记忆加载失败：${message}`));
  }
}

/* ------------------------------------------------------------------ 实时增量渲染

事件来了只往右栏和结果区追加，不重建整个对话区。
*/

interface LiveView {
  runId: string;
  planHost: HTMLElement;
  usageEl: HTMLElement;
  answerEl: HTMLElement;
  toolCount: number;
  denied: number;
  rawRows: number;
  deltaText: string;
  deltaRow: HTMLElement | null;
  deltaDetail: HTMLElement | null;
}

let liveView: LiveView | null = null;

function buildLiveTurn(turn: Turn): { el: HTMLElement; view: LiveView } {
  const wrap = h("div", { class: "turn live" });

  const user = h("div", { class: "msg user" });
  user.append(h("div", { class: "avatar" }, "你"));
  user.append(h("div", { class: "bubble" }, turn.task));
  wrap.append(user);

  const assistant = h("div", { class: "msg assistant" });
  assistant.append(h("div", { class: "avatar" }, "m"));
  const bubble = h("div", { class: "bubble" });

  const planHost = h("div", { class: "plan-host" });
  const usageEl = h("div", { class: "turn-usage muted" });
  const answerEl = h("div", { class: "answer streaming markdown" });
  bubble.append(planHost, usageEl, answerEl);

  assistant.append(bubble);
  wrap.append(assistant);

  const view: LiveView = {
    runId: turn.runId,
    planHost,
    usageEl,
    answerEl,
    toolCount: 0,
    denied: 0,
    rawRows: 0,
    deltaText: "",
    deltaRow: null,
    deltaDetail: null,
  };
  return { el: wrap, view };
}

function renderLiveAnswer(view: LiveView): void {
  const frag = renderMarkdown(view.deltaText);
  frag.append(h("span", { class: "cursor" }));
  view.answerEl.replaceChildren(frag);
}

function updateThinkMeta(view: LiveView): void {
  panel.thinkMeta.textContent =
    `共 ${view.toolCount} 次` + (view.denied ? ` · 已拒绝 ${view.denied}` : "");
}

function appendRawRow(view: LiveView, ev: AgentEvent): void {
  panel.rawList.append(eventRow(ev));
  view.rawRows += 1;
  panel.rawMeta.textContent = `${view.rawRows} 条`;
}

function renderLivePlan(view: LiveView, plan: PlanItem[] | null): void {
  if (!plan || plan.length === 0) return;
  const done = plan.filter((x) => x.status === "completed").length;
  let detail = view.planHost.querySelector("details.plan") as HTMLDetailsElement | null;
  let list: HTMLOListElement;
  if (!detail) {
    detail = h("details", { class: "plan" });
    const summary = h("summary");
    list = h("ol", { class: "plan-list" });
    detail.append(summary, list);
    view.planHost.append(detail);
  } else {
    list = detail.querySelector("ol.plan-list") as HTMLOListElement;
  }
  (detail.querySelector("summary") as HTMLElement).textContent =
    `计划：${done}/${plan.length} 完成`;
  list.replaceChildren();
  for (const item of plan) {
    const li = h("li", { class: `plan-item status-${item.status}` });
    li.textContent = item.content;
    if (item.blockedBy?.length) {
      li.append(h("span", { class: "blocked-by" }, `等待 ${item.blockedBy.join(",")}`));
    }
    list.append(li);
  }
}

function applyLiveEvent(ev: AgentEvent, silent = false): void {
  const view = liveView;
  if (!view) return;

  if (ev.kind === "assistant_delta") {
    const chunk = String(((ev.data ?? {}) as Record<string, unknown>).text ?? "");
    view.deltaText += chunk;
    renderLiveAnswer(view);
    if (!view.deltaRow) {
      view.deltaRow = h("div", { class: "event tone-info" });
      view.deltaRow.append(h("span", { class: "event-label" }, "模型输出"));
      view.deltaDetail = h("span", { class: "event-detail" }, view.deltaText.slice(0, 60));
      view.deltaRow.append(view.deltaDetail);
      panel.rawList.append(view.deltaRow);
      view.rawRows += 1;
      panel.rawMeta.textContent = `${view.rawRows} 条`;
    } else if (view.deltaDetail) {
      view.deltaDetail.textContent = view.deltaText.slice(0, 60);
    }
  } else if (ev.kind === "tool_call") {
    view.toolCount += 1;
    // 第一次有工具调用时把"暂无工具调用"的提示去掉
    if (view.toolCount === 1) panel.thinkList.replaceChildren();
    panel.thinkList.append(toolRow(describeToolCall(ev)));
    updateThinkMeta(view);
    if (!silent) renderLivePlan(view, latestPlan(liveEvents));
    appendRawRow(view, ev);
  } else if (ev.kind === "denied") {
    view.denied += 1;
    updateThinkMeta(view);
    appendRawRow(view, ev);
  } else {
    appendRawRow(view, ev);
  }

  view.usageEl.textContent = formatUsage(summarizeUsage(liveEvents));
  if (!silent) {
    updateTopbarUsage();
    scrollToBottomIfNearBottom();
  }
}

/** 从历史/刷新恢复：把已有事件灌进右栏（不滚屏、不改顶栏）。 */
function replayIntoLiveView(events: AgentEvent[], plan: PlanItem[] | null = null): void {
  panel.thinkList.replaceChildren(h("li", { class: "muted" }, "暂无工具调用"));
  panel.rawList.replaceChildren();
  for (const ev of events) applyLiveEvent(ev, true);
  if (plan) renderLivePlan(liveView as LiveView, plan);
  updateTopbarUsage();
}

/** 顶栏总用量：历史各轮 + 正在跑的这一轮。 */
function updateTopbarUsage(): void {
  const zero = {
    steps: 0, inputTokens: 0, outputTokens: 0, toolCalls: 0,
    denied: 0, failures: 0, compactions: 0, savedChars: 0, elapsedMs: 0,
  };
  const total = { ...zero };
  for (const run of runs) {
    const u = summarizeUsage(run.events ?? []);
    for (const key of Object.keys(zero) as (keyof typeof zero)[]) {
      total[key] += u[key];
    }
  }
  main.usage.textContent = total.steps ? formatUsage(total) : "";
}

function renderChat(): void {
  renderQueued = false;
  const turns = buildTurns(runs);
  main.chat.replaceChildren();
  liveView = null;
  const hasContent = turns.length > 0 || liveRun !== null;
  if (hasContent) {
    main.empty.hidden = true;
    turns.forEach((turn) => {
      const isLive = liveRun?.id === turn.runId;
      if (isLive) {
        const { el, view } = buildLiveTurn(turn);
        liveView = view;
        replayIntoLiveView(liveEvents, turn.plan);
        main.chat.append(el);
        return;
      }
      main.chat.append(renderTurn(turn, false, false));
    });
    // 右栏显示"最后一轮"的详情；正在跑时由增量事件接管
    const last = turns[turns.length - 1];
    if (!liveView && last) {
      const lastRun = runs.find((r) => r.id === last.runId);
      renderInspector(last, lastRun?.events ?? []);
    }
  } else {
    main.empty.hidden = false;
    main.chat.append(main.empty);
    renderInspector(null, []);
  }

  // 顶栏
  const conv = currentConversation();
  main.title.textContent = conv ? shortTitle(conv.title, 40) : "新会话";
  updateTopbarUsage();

  const busy = isBusy();
  main.submit.disabled = busy;
  main.submit.textContent = busy ? "执行中…" : (runs.length > 0 ? "继续对话" : "发送");
  main.resume.hidden = !latestRunResumable();
  main.cancel.hidden = !busy;
  // 徽章必须跟着 busy 一起复位，否则跑完之后还挂着"运行中…"
  main.status.hidden = !busy;
  if (busy) main.status.textContent = "执行中…";

  scrollToBottom();
}

function latestRunResumable(): boolean {
  const last = runs[runs.length - 1];
  if (!last) return false;
  // 出错（provider 挂了等）同样可以从检查点续跑
  if (last.status === "error") return true;
  if (last.status !== "done") return false;
  const ok = (last.result as Record<string, unknown> | null | undefined)?.ok;
  return ok === false;
}

function render(): void {
  if (renderQueued) return;
  renderQueued = true;
  window.requestAnimationFrame(() => {
    renderQueued = false;
    renderChat();
    renderConversations();
  });
}

function scrollToBottom(): void {
  main.chat.scrollTop = main.chat.scrollHeight;
}

/** 只有用户本来就在底部时才自动跟随，免得他往回翻看的时候被拽走。 */
function scrollToBottomIfNearBottom(): void {
  const el = main.chat;
  const near = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
  if (near) el.scrollTop = el.scrollHeight;
}

/* ------------------------------------------------------------------ 数据加载 */

async function loadWorkspaces(): Promise<void> {
  try {
    const data = await api.workspaces();
    workspaces = data.workspaces;
    renderWorkspaces();
  } catch {
    sidebar.workspace.replaceChildren(h("option", {}, "（工作区列表加载失败）"));
  }
}

async function loadConversations(): Promise<void> {
  try {
    const data = await api.conversations();
    conversations = data.conversations;
    renderConversations();
  } catch {
    sidebar.conversations.replaceChildren(h("p", { class: "muted hint" }, "会话列表加载失败"));
  }
}

let conversationGeneration = 0;

function setHash(rootId: string): void {
  const next = rootId ? `#/c/${rootId}` : "#/";
  if (window.location.hash !== next) {
    // replaceState：不往历史里塞一堆条目，但地址栏能复制分享
    window.history.replaceState(null, "", next);
  }
}

async function openConversation(rootId: string, opts: { silent?: boolean } = {}): Promise<void> {
  const gen = ++conversationGeneration;
  conversationRoot = rootId;
  setHash(rootId);
  remember(LAST_CONVERSATION_KEY, rootId);
  closeStream?.();
  closeStream = null;
  liveRun = null;
  liveEvents = [];

  const conv = conversations.find((c) => c.root_id === rootId);
  if (!conv) {
    // 新会话：还没有 run
    runs = [];
    render();
    return;
  }

  if (!opts.silent) setStatus("正在加载会话…");
  // 并发拉：串行 await 在长会话里很慢，而且慢的那个晚到会覆盖后点的那个
  const settled = await Promise.all(
    conv.run_ids.map((runId) => api.runWithEvents(runId).catch(() => null)),
  );
  if (gen !== conversationGeneration) return;   // 用户已经切走了，别覆盖
  const loaded = settled.filter((r): r is RunRow => r !== null);
  runs = loaded;
  render();

  const last = runs[runs.length - 1];
  if (last && (last.status === "running" || last.status === "queued")) {
    attachStream(last);
  } else {
    setStatus("");
  }
  renderConversations();
}

function attachStream(run: RunRow): void {
  closeStream?.();
  liveRun = run;
  // 关键：这里必须清空。后端 SSE 每次连接都从头重放全部事件，
  // 如果先把 ?events=true 取回的塞进来，同一批事件就被渲染两遍
  // （实测：一次 bash 调用显示成两行、usage 翻倍）。
  liveEvents = [];
  const idx = runs.findIndex((r) => r.id === run.id);
  if (idx >= 0) runs[idx] = { ...run, events: liveEvents };
  else runs = [...runs, { ...run, events: liveEvents }];

  closeStream = subscribeRun(
    run.id,
    (ev) => {
      liveEvents = [...liveEvents, ev];
      const current = runs.find((r) => r.id === run.id);
      if (current) current.events = liveEvents;
      // 只更新变化的那一行，不重建整个对话区（rebuild 会让每跳一步都闪一下）
      applyLiveEvent(ev);
    },
    (_kind, ev) => {
      const data = (ev.data ?? {}) as Record<string, unknown>;
      liveRun = null;
      const current = runs.find((r) => r.id === run.id);
      if (current) {
        current.status = String(data.status ?? "done");
        current.result = (data.result ?? null) as Record<string, unknown> | null;
        current.error = typeof data.error === "string" ? data.error : null;
        current.events = liveEvents;
      }
      setStatus(current?.status === "error" ? "执行失败" : "已完成", current?.status === "error" ? "error" : "ok");
      render();
      void loadConversations();
      void loadWorkspaces();
    },
    () => setStatus("事件流连接中断", "error"),
  );
}

/* ------------------------------------------------------------------ 动作 */

function newChat(): void {
  closeStream?.();
  closeStream = null;
  conversationRoot = "";
  runs = [];
  liveRun = null;
  liveEvents = [];
  forget(LAST_CONVERSATION_KEY);
  setHash("");
  main.task.value = "";
  main.chat.replaceChildren(main.empty);
  main.empty.hidden = false;
  setStatus("");
  render();
  main.task.focus();
}

let submitting = false;

async function submit(): Promise<void> {
  // 运行中再提交会新开一个 run 并把事件流抢过来，旧 run 还在后台烧钱
  if (submitting || isBusy()) {
    setStatus("上一轮仍在执行，请稍候", "error");
    return;
  }
  const task = main.task.value.trim();
  if (!task) {
    setStatus("请输入任务内容", "error");
    return;
  }
  const workspace = selectedWorkspace();
  if (workspace) remember(LAST_WORKSPACE_KEY, workspace);

  const lastRun = runs[runs.length - 1];
  const canFollow = lastRun && canContinue(lastRun);

  submitting = true;
  main.submit.disabled = true;
  setStatus(canFollow ? "正在继续上一轮…" : "提交中…");

  try {
    let runId: string;
    if (canFollow && lastRun) {
      const resp = await api.followup(lastRun.id, task, Number(main.maxSteps.value) || 20);
      runId = resp.id;
    } else {
      const resp = await api.createRun({
        task,
        workspace: workspace || undefined,
        model: main.model.value || undefined,
        maxSteps: Number(main.maxSteps.value) || 20,
        includeFiles: main.optFiles.checked,
        plan: main.optPlan.checked,
        skills: main.optSkills.checked,
        compact: main.optCompact.checked,
        tasks: main.optTasks.checked,
        memory: main.optMemory.checked,
        mcp: main.optMcp.checked,
      });
      runId = resp.id;
      if (!conversationRoot) conversationRoot = runId;
      remember(LAST_CONVERSATION_KEY, conversationRoot);
    }
    main.task.value = "";

    const row: RunRow = {
      id: runId,
      task,
      status: "running",
      result: null,
      created_at: Date.now() / 1000,
      events: [],
      workspace: workspace || undefined,
      // 续聊沿用上一轮的开关（服务端也是这么继承的）
      memory: canFollow ? lastRun?.memory : main.optMemory.checked,
    };
    if (canFollow) {
      runs = [...runs, row];
    } else {
      runs = [row];
      conversationRoot = runId;
    }
    attachStream(row);
    render();
    void loadConversations();
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    setStatus(`提交失败：${message}`, "error");
  } finally {
    submitting = false;
    main.submit.disabled = false;
  }
}

/** 上一轮能不能接着聊：正常完成或出错都算，还在跑的不算。 */
function canContinue(run: RunRow): boolean {
  return run.status === "done" || run.status === "error";
}

/** 删除一个会话（连同它的全部 run）。删之前问一次，避免点错。 */
async function deleteConversation(conv: Conversation): Promise<void> {
  const label = shortTitle(conv.title, 24);
  const turns = conv.turns && conv.turns > 1 ? `（共 ${conv.turns} 轮）` : "";
  if (!window.confirm(`确定删除会话「${label}」${turns}？删除后不可恢复。`)) return;

  try {
    await api.deleteConversation(conv.root_id);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    setStatus(`删除失败：${message}`, "error");
    return;
  }

  setStatus("已删除会话");
  // 删的正是当前打开的那个，就回到空状态；否则只刷新左栏
  if (conversationRoot === conv.root_id) {
    closeStream?.();
    closeStream = null;
    conversationRoot = "";
    runs = [];
    liveRun = null;
    liveEvents = [];
    setHash("");
    render();
  }
  await loadConversations();
}

async function cancelRun(): Promise<void> {
  const running = [...runs].reverse().find(
    (r) => r.status === "running" || r.status === "queued",
  );
  if (!running) {
    setStatus("当前没有执行中的任务", "error");
    return;
  }
  main.cancel.disabled = true;
  setStatus("正在停止…（等待当前步骤结束）");
  try {
    await api.cancel(running.id);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    setStatus(`取消失败：${message}`, "error");
  } finally {
    main.cancel.disabled = false;
  }
}

async function resumeRun(): Promise<void> {
  const last = runs[runs.length - 1];
  if (!last) return;
  setStatus("正在从检查点恢复…");
  try {
    await api.resume(last.id, Math.max(Number(main.maxSteps.value) || 20, 8));
    const refreshed = await api.run(last.id);
    runs = runs.map((r) => (r.id === last.id ? { ...refreshed, events: r.events } : r));
    attachStream(runs[runs.length - 1]);
    render();
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    setStatus(`继续失败：${message}`, "error");
  }
}

/* ------------------------------------------------------------------ 初始化 */

const TOKEN_KEY = "minicode:token";

function applyToken(value: string): void {
  setAccessToken(value);
  remember(TOKEN_KEY, value);
}

async function init(): Promise<void> {
  // 先把令牌接上，后面所有请求才会带 Authorization
  const savedToken = recall(TOKEN_KEY);
  if (savedToken) {
    sidebar.token.value = savedToken;
    setAccessToken(savedToken);
  }

  try {
    const health = await api.health();
    sidebar.store.textContent = `${health.store ?? "?"} · ${health.runs ?? 0} 个 run`;
  } catch {
    sidebar.store.textContent = "后端连接失败";
  }

  await loadWorkspaces();
  await loadConversations();

  // 地址栏里的会话优先（可分享、可后退），其次才是上次打开的那个
  const fromHash = (window.location.hash.match(/^#\/c\/([0-9a-zA-Z_-]+)$/) ?? [])[1] ?? "";
  const saved = recall(LAST_CONVERSATION_KEY);
  const target = [fromHash, saved].find(
    (id) => id && conversations.some((c) => c.root_id === id),
  );
  if (target) {
    await openConversation(target, { silent: true });
  } else {
    render();
  }
}

main.submit.addEventListener("click", () => void submit());
main.task.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") void submit();
});
sidebar.token.addEventListener("change", () => {
  applyToken(sidebar.token.value);
  setStatus(sidebar.token.value ? "令牌已保存" : "已清除令牌");
  // 令牌变了，历史会话要用新令牌重新拉
  void loadConversations();
});

main.cancel.addEventListener("click", () => void cancelRun());
main.resume.addEventListener("click", () => void resumeRun());
sidebar.newChat.addEventListener("click", () => newChat());
sidebar.refresh.addEventListener("click", () => {
  void loadWorkspaces();
  void loadConversations();
  setStatus("已刷新");
});
sidebar.workspace.addEventListener("change", () => {
  sidebar.custom.value = "";
  remember(LAST_WORKSPACE_KEY, sidebar.workspace.value);
});
sidebar.custom.addEventListener("input", () => {
  if (sidebar.custom.value.trim()) remember(LAST_WORKSPACE_KEY, sidebar.custom.value.trim());
});
for (const example of document.querySelectorAll<HTMLButtonElement>(".example")) {
  example.addEventListener("click", () => {
    main.task.value = example.dataset.task ?? "";
    main.task.focus();
  });
}

/* ------------------------------------------------------------------ 右栏开关 */

const INSPECTOR_KEY = "minicode:inspector";

function setInspector(open: boolean): void {
  panel.appRoot.classList.toggle("inspector-hidden", !open);
  remember(INSPECTOR_KEY, open ? "1" : "0");
}

function inspectorOpen(): boolean {
  return !panel.appRoot.classList.contains("inspector-hidden");
}

panel.toggle.addEventListener("click", () => setInspector(!inspectorOpen()));
panel.close.addEventListener("click", () => setInspector(false));
setInspector(recall(INSPECTOR_KEY) !== "0");   // 默认展开

window.addEventListener("hashchange", () => {
  const id = (window.location.hash.match(/^#\/c\/([0-9a-zA-Z_-]+)$/) ?? [])[1] ?? "";
  if (id && id !== conversationRoot) void openConversation(id, { silent: true });
});

void init();
