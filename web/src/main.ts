/**
 * 界面接线：左栏（工作区 + 历史会话）+ 主区（对话 / 轨迹两个视图）。
 *
 * 参考 DSH Web GUI 的结构：主区通过导航栏切换视图，而不是常驻一个右栏。
 * - 对话：按轮次展示问题、计划、思考、结果（流式增量更新）
 * - 轨迹：按轮次/步骤展示完整原始记录（thinking 全文、工具参数、完整输出、事件 JSON），不省略
 *
 * 分层：纯逻辑在 chat.ts / derive.ts / format.ts（都有单测），接口在 api.ts，
 * 这里只负责"把状态画到 DOM 上"和"把用户操作变成请求"。
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
  groupByDay,
  shortTitle,
  statusTone,
  type Turn,
} from "./chat.ts";
import { describeEvent, type AgentEvent } from "./format.ts";
import {
  formatUsage,
  latestPlan,
  summarizeUsage,
  type PlanItem,
} from "./derive.ts";
import type { ThoughtView } from "./chat.ts";
import { renderMarkdown } from "./markdown.ts";

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
const VIEW_KEY = "minicode:view";

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
  viewChat: el<HTMLButtonElement>("viewChat"),
  viewTrajectory: el<HTMLButtonElement>("viewTrajectory"),
};

/* ------------------------------------------------------------------ 展开状态 */

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
  node.addEventListener("toggle", () => {
    if (node.open !== initialOpen) detailsState.set(key, node.open);
  });
  node.append(h("summary", {}, summaryText));
  const body = build();
  if (body) node.append(body);
  return node;
}

/* ------------------------------------------------------------------ 状态 */

type ViewMode = "chat" | "trajectory";

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
let viewMode: ViewMode = "chat";

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
      const row = h("div", { class: "conv-row" });
      const item = h("button", { class: "conv", dataset: { root: conv.root_id } });
      if (conv.root_id === conversationRoot) {
        item.classList.add("active");
        item.setAttribute("aria-current", "true");
      }
      const tone = statusTone(conv.status);
      item.append(h("span", { class: `dot tone-${tone}` }));
      item.append(h("span", { class: "conv-title" }, shortTitle(conv.title, 30)));
      if (conv.turns && conv.turns > 1) {
        item.append(h("span", { class: "conv-turns" }, `${conv.turns} 轮`));
      }
      item.addEventListener("click", () => void openConversation(conv.root_id));
      row.append(item);

      const del = h("button", { class: "conv-del", title: "删除会话" }, "×");
        del.setAttribute("aria-label", "删除会话");
      del.addEventListener("click", (event) => {
        event.stopPropagation();
        void deleteConversation(conv);
      });
      row.append(del);
      sidebar.conversations.append(row);
    }
  }
}

/* ------------------------------------------------------------------ 对话视图 */

function thoughtBlock(thought: ThoughtView): HTMLElement {
  const block = h("div", { class: "thought-step" });
  block.append(h("div", { class: "thought-step-label" }, `第 ${thought.step} 步`));
  block.append(h("pre", { class: "thought-text" }, `${thought.text}\n`));
  return block;
}

function renderThoughts(turn: Turn): HTMLElement | null {
  if (!turn.thoughts || turn.thoughts.length === 0) return null;
  const steps = turn.thoughts.map((t) => t.step).join("、");
  return detailsBlock(`${turn.runId}:thoughts`, "thoughts", `思考：第 ${steps} 步`, () => {
    const list = h("div", { class: "thought-list" });
    for (const thought of turn.thoughts) list.append(thoughtBlock(thought));
    return list;
  }, true);
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

function renderTurn(turn: Turn, live = false): HTMLElement {
  const wrap = h("div", { class: `turn${live ? " live" : ""}` });

  const user = h("div", { class: "msg user" });
  user.append(h("div", { class: "avatar" }, "你"));
  user.append(h("div", { class: "bubble" }, turn.task));
  wrap.append(user);

  const assistant = h("div", { class: "msg assistant" });
  assistant.append(h("div", { class: "avatar" }, "m"));
  const bubble = h("div", { class: "bubble" });

  const plan = renderPlan(turn, false);
  if (plan) bubble.append(plan);

  const thoughts = renderThoughts(turn);
  if (thoughts) bubble.append(thoughts);

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
    bubble.append(h("div", { class: "answer error" }, "执行失败（切换到「轨迹」查看原始信息）"));
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

/* ------------------------------------------------------------------ 轨迹视图（原始信息不省略） */

function prettyJson(value: unknown): string {
  try {
    const text = JSON.stringify(value, null, 2);
    return text ?? String(value);
  } catch {
    return String(value);
  }
}

function textOf(ev: AgentEvent, key: string): string {
  const data = (ev.data ?? {}) as Record<string, unknown>;
  return typeof data[key] === "string" ? (data[key] as string) : "";
}

function numOf(ev: AgentEvent, key: string): number | undefined {
  const data = (ev.data ?? {}) as Record<string, unknown>;
  return typeof data[key] === "number" ? (data[key] as number) : undefined;
}

function stepOf(ev: AgentEvent): number | null {
  const step = numOf(ev, "step");
  return step === undefined ? null : step;
}

interface StepGroup {
  step: number | null;
  events: AgentEvent[];
}

/** 按 data.step 分组，保持事件原始顺序。没有 step 的（start/mcp_ready）单独一组。 */
function groupEventsByStep(events: AgentEvent[]): StepGroup[] {
  const groups: StepGroup[] = [];
  const index = new Map<number | null, StepGroup>();
  for (const ev of events) {
    const step = stepOf(ev);
    let group = index.get(step);
    if (!group) {
      group = { step, events: [] };
      index.set(step, group);
      groups.push(group);
    }
    group.events.push(ev);
  }
  return groups;
}

function rawPre(text: string, cls = ""): HTMLElement {
  return h("pre", { class: `traj-pre ${cls}`.trim() }, text);
}

function renderTrajectoryItem(ev: AgentEvent): HTMLElement | null {
  const data = (ev.data ?? {}) as Record<string, unknown>;
  switch (ev.kind) {
    case "assistant_delta":
      return null; // 由调用方合并成一段"模型输出"
    case "thinking": {
      const item = h("div", { class: "traj-item thought" });
      item.append(h("div", { class: "traj-label" }, `thinking · 第 ${numOf(ev, "step") ?? "?"} 步`));
      item.append(rawPre(textOf(ev, "text"), "thinking-text"));
      return item;
    }
    case "model_call": {
      const usage = (data.usage ?? {}) as Record<string, unknown>;
      const item = h("div", { class: "traj-item model" });
      item.append(h("div", { class: "traj-label" }, "模型调用"));
      item.append(h("div", { class: "traj-line" },
        `stop_reason=${String(data.stop_reason ?? "?")} · 入 ${usage.input_tokens ?? 0} / 出 ${usage.output_tokens ?? 0} · 工具调用 ${data.tool_calls ?? 0} 个`));
      return item;
    }
    case "tool_call": {
      const item = h("div", { class: "traj-item tool" });
      const head = h("div", { class: "traj-tool-head" });
      head.append(h("span", { class: "traj-tool-name" }, String(data.tool ?? "?")));
      if (typeof data.exit_code === "number") {
        head.append(h("span", { class: `traj-exit ${data.failed === true ? "bad" : "ok"}` },
          data.failed === true ? `exit ${data.exit_code}` : "exit 0"));
      }
      item.append(head);
      if (data.args !== undefined) {
        item.append(h("div", { class: "traj-label" }, "参数"));
        item.append(rawPre(prettyJson(data.args), "json"));
      }
      item.append(h("div", { class: "traj-label" }, "输出（完整）"));
      item.append(rawPre(typeof data.output === "string" ? data.output : "(无输出)", "output"));
      if (data.meta && Object.keys(data.meta as Record<string, unknown>).length > 0) {
        item.append(h("div", { class: "traj-label" }, "meta"));
        item.append(rawPre(prettyJson(data.meta), "json"));
      }
      return item;
    }
    case "denied": {
      const item = h("div", { class: "traj-item denied" });
      item.append(h("div", { class: "traj-label" }, "权限拒绝"));
      item.append(h("div", { class: "traj-line" },
        `${String(data.reason ?? "?")}${data.detail ? ` · ${String(data.detail)}` : ""} · ${String(data.by ?? "")}`));
      if (data.args !== undefined) item.append(rawPre(prettyJson(data.args), "json"));
      return item;
    }
    case "hook_error": {
      const item = h("div", { class: "traj-item hook-error" });
      item.append(h("div", { class: "traj-label" }, "钩子异常"));
      item.append(rawPre(prettyJson(data), "json"));
      return item;
    }
    case "compact": {
      const item = h("div", { class: "traj-item compact" });
      item.append(h("div", { class: "traj-label" }, "上下文压缩"));
      item.append(h("div", { class: "traj-line" },
        `${data.chars_before ?? "?"} → ${data.chars_after ?? "?"} 字符（省 ${Math.round(Number(data.saved_ratio ?? 0) * 100)}%）；落盘 ${data.persisted ?? 0}、截断 ${data.trimmed ?? 0}、删整对 ${data.snipped_messages ?? 0}`));
      return item;
    }
    case "finish": {
      const item = h("div", { class: "traj-item finish" });
      item.append(h("div", { class: "traj-label" }, "结束"));
      item.append(h("div", { class: "traj-line" },
        `ok=${String(data.ok ?? "?")} · reason=${String(data.reason ?? "?")}`));
      return item;
    }
    case "unknown_tool": {
      const item = h("div", { class: "traj-item denied" });
      item.append(h("div", { class: "traj-label" }, "未知工具"));
      item.append(h("div", { class: "traj-line" }, String(data.tool ?? "?")));
      return item;
    }
    default: {
      const described = describeEvent(ev);
      const item = h("div", { class: "traj-item other" });
      item.append(h("div", { class: "traj-label" }, described.label));
      item.append(h("div", { class: "traj-line" }, described.detail));
      item.append(rawPre(prettyJson(data), "json"));
      return item;
    }
  }
}

function renderTrajectoryStep(runId: string, group: StepGroup): HTMLElement {
  const title = group.step === null ? "轮次开始" : `第 ${group.step} 步`;
  const tools = group.events.filter((e) => e.kind === "tool_call").length;
  const thoughts = group.events.filter((e) => e.kind === "thinking").length;
  const pieces = [
    `${group.events.length} 条事件`,
    thoughts ? `${thoughts} 段思考` : "",
    tools ? `${tools} 次工具` : "",
  ].filter(Boolean).join(" · ");

  return detailsBlock(
    `traj:${runId}:${group.step ?? "start"}`,
    "traj-step",
    `${title} · ${pieces}`,
    () => {
      const body = h("div", { class: "traj-step-body" });
      let answerText = "";
      const flushAnswer = () => {
        if (!answerText) return;
        const item = h("div", { class: "traj-item answer-raw" });
        item.append(h("div", { class: "traj-label" }, "模型输出（流式片段合并）"));
        item.append(rawPre(answerText, "answer-text"));
        body.append(item);
        answerText = "";
      };
      for (const ev of group.events) {
        if (ev.kind === "assistant_delta") {
          answerText += textOf(ev, "text");
          continue;
        }
        const item = renderTrajectoryItem(ev);
        if (item) body.append(item);
      }
      flushAnswer();
      body.append(detailsBlock(
        `traj:${runId}:${group.step ?? "start"}:json`,
        "traj-raw-json",
        `原始事件 JSON（${group.events.length} 条，不省略）`,
        () => rawPre(prettyJson(group.events), "json"),
        false,
      ));
      return body;
    },
    true,
  );
}

function renderTrajectoryRun(run: RunRow, index: number): HTMLElement {
  const events = run.events ?? [];
  const usage = summarizeUsage(events);
  const section = h("section", { class: "traj-run" });

  const head = h("div", { class: "traj-run-head" });
  head.append(h("span", { class: "traj-run-index" }, `第 ${index + 1} 轮`));
  head.append(h("span", { class: "traj-run-task" }, run.task));
  const result = (run.result ?? {}) as Record<string, unknown>;
  const runStatus = run.status ?? "queued";
  const status = runStatus === "done" && result.ok === false ? "未完成" : runStatus;
  head.append(h("span", { class: `traj-run-status tone-${statusTone(runStatus)}` }, status));
  head.append(h("span", { class: "traj-run-meta muted" }, formatUsage(usage)));
  section.append(head);

  for (const group of groupEventsByStep(events)) {
    section.append(renderTrajectoryStep(run.id, group));
  }

  if (run.error) {
    section.append(h("div", { class: "traj-result error" }, `错误：${run.error}`));
  } else if (typeof result.final_text === "string" && result.final_text) {
    const box = h("div", { class: "traj-result" });
    box.append(h("div", { class: "traj-label" }, "最终结果"));
    const markdown = h("div", { class: "answer markdown" });
    markdown.append(renderMarkdown(result.final_text));
    box.append(markdown);
    section.append(box);
  }
  return section;
}

function renderTrajectory(): void {
  main.chat.className = "chat trajectory";
  const scrollTop = main.chat.scrollTop;
  main.chat.replaceChildren();
  if (runs.length === 0) {
    main.chat.append(h("div", { class: "empty" },
      h("p", { class: "empty-title" }, "还没有运行记录"),
      h("p", { class: "muted" }, "在下方输入任务，或从左侧选择历史会话。")));
    return;
  }
  runs.forEach((run, index) => main.chat.append(renderTrajectoryRun(run, index)));
  main.chat.scrollTop = scrollTop;
}

/* ------------------------------------------------------------------ 实时增量渲染（对话视图） */

interface LiveView {
  runId: string;
  planHost: HTMLElement;
  thoughtsHost: HTMLElement;
  thoughts: ThoughtView[];
  usageEl: HTMLElement;
  answerEl: HTMLElement;
  deltaText: string;
}

let liveView: LiveView | null = null;

function buildLiveTurn(turn: Turn): { el: HTMLElement; view: LiveView } {
  const wrap = renderTurn(turn, true);
  const bubble = wrap.querySelector(".bubble") as HTMLElement;
  const planHost = h("div", { class: "plan-host" });
  const thoughtsHost = h("div", { class: "thoughts-host" });
  const usageEl = h("div", { class: "turn-usage muted" });
  const answerEl = h("div", { class: "answer streaming markdown" });
  bubble.replaceChildren(planHost, thoughtsHost, usageEl, answerEl);

  const view: LiveView = {
    runId: turn.runId,
    planHost,
    thoughtsHost,
    thoughts: [],
    usageEl,
    answerEl,
    deltaText: "",
  };
  return { el: wrap, view };
}

function renderLiveAnswer(view: LiveView): void {
  const frag = renderMarkdown(view.deltaText);
  frag.append(h("span", { class: "cursor" }));
  view.answerEl.replaceChildren(frag);
}

function renderLivePlan(view: LiveView, plan: PlanItem[] | null): void {
  if (!plan || plan.length === 0) return;
  const done = plan.filter((x) => x.status === "completed").length;
  let detail = view.planHost.querySelector("details.plan") as HTMLDetailsElement | null;
  let list: HTMLOListElement;
  if (!detail) {
    detail = h("details", { class: "plan" });
    list = h("ol", { class: "plan-list" });
    detail.append(h("summary"), list);
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
    list.append(li);
  }
}

function renderLiveThoughts(view: LiveView): void {
  if (view.thoughts.length === 0) return;
  const steps = view.thoughts.map((t) => t.step).join("、");
  const node = detailsBlock(`${view.runId}:thoughts`, "thoughts", `思考：第 ${steps} 步`, () => {
    const list = h("div", { class: "thought-list" });
    for (const thought of view.thoughts) list.append(thoughtBlock(thought));
    return list;
  }, true);
  view.thoughtsHost.replaceChildren(node);
}

function applyLiveEvent(ev: AgentEvent): void {
  const view = liveView;
  if (!view) return;
  if (ev.kind === "assistant_delta") {
    view.deltaText += textOf(ev, "text");
    renderLiveAnswer(view);
  } else if (ev.kind === "thinking") {
    const text = textOf(ev, "text");
    if (text.trim()) {
      view.thoughts.push({ step: numOf(ev, "step") ?? 0, text });
      renderLiveThoughts(view);
    }
  } else if (ev.kind === "tool_call") {
    renderLivePlan(view, latestPlan(liveEvents));
  }
  view.usageEl.textContent = formatUsage(summarizeUsage(liveEvents));
  updateTopbarUsage();
  scrollToBottomIfNearBottom();
}

function replayIntoLiveView(events: AgentEvent[], plan: PlanItem[] | null): void {
  const live = liveView as LiveView | null;
  if (!live) return;
  live.thoughts = [];
  live.thoughtsHost.replaceChildren();
  for (const ev of events) applyLiveEvent(ev);
  if (plan) renderLivePlan(live, plan);
}

/* ------------------------------------------------------------------ 渲染入口 */

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

function renderViewTabs(): void {
  main.viewChat.classList.toggle("active", viewMode === "chat");
  main.viewTrajectory.classList.toggle("active", viewMode === "trajectory");
  main.viewChat.setAttribute("aria-selected", String(viewMode === "chat"));
  main.viewTrajectory.setAttribute("aria-selected", String(viewMode === "trajectory"));
}

function setView(mode: ViewMode): void {
  viewMode = mode;
  remember(VIEW_KEY, mode);
  render();
}

function renderChrome(): void {
  const conv = currentConversation();
  main.title.textContent = conv ? shortTitle(conv.title, 40) : "新会话";
  const busy = isBusy();
  main.submit.disabled = busy;
  main.submit.textContent = busy ? "执行中…" : (runs.length > 0 ? "继续对话" : "发送");
  main.resume.hidden = !latestRunResumable();
  main.cancel.hidden = !busy;
  main.status.hidden = !busy;
  if (busy) main.status.textContent = "执行中…";
}

function renderChat(): void {
  main.chat.className = "chat";
  const turns = buildTurns(runs);
  main.chat.replaceChildren();
  liveView = null;
  const hasContent = turns.length > 0 || liveRun !== null;
  if (!hasContent) {
    main.empty.hidden = false;
    main.chat.append(main.empty);
    return;
  }
  main.empty.hidden = true;
  for (const turn of turns) {
    const isLive = liveRun?.id === turn.runId;
    if (isLive) {
      const { el, view } = buildLiveTurn(turn);
      liveView = view;
      replayIntoLiveView(liveEvents, turn.plan);
      main.chat.append(el);
      continue;
    }
    main.chat.append(renderTurn(turn));
  }
  scrollToBottom();
}

function latestRunResumable(): boolean {
  const last = runs[runs.length - 1];
  if (!last) return false;
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
    renderViewTabs();
    renderChrome();
    updateTopbarUsage();
    renderConversations();
    if (viewMode === "chat") renderChat();
    else renderTrajectory();
  });
}

function scrollToBottom(): void {
  main.chat.scrollTop = main.chat.scrollHeight;
}

function scrollToBottomIfNearBottom(): void {
  const elNode = main.chat;
  const near = elNode.scrollHeight - elNode.scrollTop - elNode.clientHeight < 120;
  if (near) elNode.scrollTop = elNode.scrollHeight;
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
    runs = [];
    render();
    return;
  }

  if (!opts.silent) setStatus("正在加载会话…");
  const settled = await Promise.all(
    conv.run_ids.map((runId) => api.runWithEvents(runId).catch(() => null)),
  );
  if (gen !== conversationGeneration) return;
  runs = settled.filter((r): r is RunRow => r !== null);
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
  liveEvents = [];
  const index = runs.findIndex((r) => r.id === run.id);
  if (index >= 0) runs[index] = { ...run, events: liveEvents };
  else runs = [...runs, { ...run, events: liveEvents }];

  closeStream = subscribeRun(
    run.id,
    (ev) => {
      liveEvents = [...liveEvents, ev];
      const current = runs.find((r) => r.id === run.id);
      if (current) current.events = liveEvents;
      if (viewMode === "chat") applyLiveEvent(ev);
      else render();
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
  setStatus("");
  render();
  main.task.focus();
}

let submitting = false;

async function submit(): Promise<void> {
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
      memory: canFollow ? lastRun?.memory : main.optMemory.checked,
    };
    if (canFollow) runs = [...runs, row];
    else {
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

function canContinue(run: RunRow): boolean {
  return run.status === "done" || run.status === "error";
}

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
  const savedToken = recall(TOKEN_KEY);
  if (savedToken) {
    sidebar.token.value = savedToken;
    setAccessToken(savedToken);
  }

  // 视图：URL 参数优先（方便分享/截图），其次本地记忆，默认对话
  const fromQuery = new URLSearchParams(window.location.search).get("view");
  const savedView = recall(VIEW_KEY);
  viewMode = fromQuery === "trajectory" || fromQuery === "raw"
    ? "trajectory"
    : fromQuery === "chat"
      ? "chat"
      : savedView === "trajectory"
        ? "trajectory"
        : "chat";

  try {
    const health = await api.health();
    sidebar.store.textContent = `${health.store ?? "?"} · ${health.runs ?? 0} 个 run`;
  } catch {
    sidebar.store.textContent = "后端连接失败";
  }

  await loadWorkspaces();
  await loadConversations();

  const fromHash = (window.location.hash.match(/^#\/c\/([0-9a-zA-Z_-]+)$/) ?? [])[1] ?? "";
  const saved = recall(LAST_CONVERSATION_KEY);
  const target = [fromHash, saved].find(
    (id) => id && conversations.some((c) => c.root_id === id),
  );
  if (target) await openConversation(target, { silent: true });
  else render();
}

main.submit.addEventListener("click", () => void submit());
main.task.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") void submit();
});
sidebar.token.addEventListener("change", () => {
  applyToken(sidebar.token.value);
  setStatus(sidebar.token.value ? "令牌已保存" : "已清除令牌");
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
main.viewChat.addEventListener("click", () => setView("chat"));
main.viewTrajectory.addEventListener("click", () => setView("trajectory"));

window.addEventListener("hashchange", () => {
  const id = (window.location.hash.match(/^#\/c\/([0-9a-zA-Z_-]+)$/) ?? [])[1] ?? "";
  if (id && id !== conversationRoot) void openConversation(id, { silent: true });
});

void init();
