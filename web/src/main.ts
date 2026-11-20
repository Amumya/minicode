/**
 * 界面接线：左栏（工作区与历史会话）与主区（对话 / 轨迹视图）。
 *
 * 主区通过导航栏切换视图，不使用常驻右侧栏。
 * - 对话：按轮次展示计划、用量与结果（流式增量更新）
 * - 轨迹：按轮次与步骤展示完整事件记录（工具参数、完整输出、事件 JSON）
 *
 * 分层：纯逻辑位于 chat.ts / derive.ts / format.ts（均有单元测试），
 * 接口位于 api.ts；本文件负责 DOM 渲染与用户操作到请求的转换。
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
  formatElapsed,
  groupByDay,
  runOutcome,
  shortTitle,
  statusTone,
  type Turn,
} from "./chat.ts";
import { describeEvent, type AgentEvent } from "./format.ts";
import { describeToolCall } from "./chat.ts";
import {
  formatUsage,
  latestPlan,
  summarizeUsage,
  type PlanItem,
} from "./derive.ts";
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

function lazyDetailsBlock(
  cls: string,
  summaryText: string,
  build: () => HTMLElement | null,
  defaultOpen = false,
): HTMLElement {
  // 轨迹里的参数/输出/原始 JSON 可能很大，先不建 DOM，展开时才渲染。
  const node = h("details", { class: cls });
  let built = false;
  const ensure = () => {
    if (built || !node.open) return;
    built = true;
    const body = build();
    if (body) node.append(body);
  };
  node.append(h("summary", {}, summaryText));
  node.addEventListener("toggle", ensure);
  node.open = defaultOpen;
  if (defaultOpen) ensure();
  return node;
}

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
let liveStatusText = "";

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

  if (turn.usage.steps > 0) {
    bubble.append(h("div", { class: "turn-usage muted" }, formatUsage(turn.usage)));
  }

  const outcome = runOutcome(turn);
  if (turn.error) {
    bubble.append(h("div", { class: "answer error" }, `出错：${turn.error}`));
  } else if (turn.status === "error") {
    bubble.append(h("div", { class: "answer error" }, "执行失败（切换到「轨迹」查看原始信息）"));
  } else {
    if (outcome === "interrupted") {
      const label = turn.stopReason === "max_steps"
        ? "步数用尽，任务中断"
        : turn.stopReason === "cancelled"
          ? "已取消"
          : "任务未完成";
      bubble.append(h("div", { class: "answer-warning" }, `${label}（这不是最终结果）`));
    } else if (outcome === "budget_boundary") {
      bubble.append(h("div", { class: "answer-hint" },
        `本轮到第 ${turn.usage.steps} 步结束（等于步数上限 ${turn.maxSteps}）。如果任务没做完，点「继续执行」。`));
    }
    if (turn.answer) {
      bubble.append(h("div", { class: "answer-label" },
        outcome === "interrupted" ? "中断前模型的最后输出" : "结果"));
      const answerNode = h("div", { class: "answer markdown" });
      answerNode.append(renderMarkdown(turn.answer));
      bubble.append(answerNode);
    } else if (outcome === "interrupted") {
      bubble.append(h("div", { class: "answer muted" }, "没有产出最终结果"));
    }
  }

  assistant.append(bubble);
  wrap.append(assistant);
  return wrap;
}

/* ------------------------------------------------------------------ 轨迹视图（追加式全事件日志）

参考 DSH 的 Trajectory：不是把 thinking 单独拎出来美化，而是把一次 run 的
**全部事件**按发生顺序追加成日志：用户任务、系统提示、模型调用、thinking 原文、
工具调用（完整参数/输出）、压缩、拒绝、结束……每一步之间插一条步骤分隔。
新事件直接 append 到日志末尾，不重建整页。
*/

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

function rawPre(text: string, cls = ""): HTMLElement {
  return h("pre", { class: `traj-pre ${cls}`.trim() }, text);
}

function eventRawJson(ev: AgentEvent): HTMLElement {
  return lazyDetailsBlock(
    "traj-event-json",
    "原始事件 JSON",
    () => rawPre(prettyJson(ev), "json"),
    false,
  );
}

function trajectoryLabel(ev: AgentEvent): string {
  switch (ev.kind) {
    case "start": return "开始";
    case "resume": return "从检查点恢复";
    case "mcp_ready": return "MCP 连接";
    case "thinking": return "thinking（模型推理原文）";
    case "model_call": return "模型调用";
    case "tool_call": return "工具调用";
    case "denied": return "权限拒绝";
    case "hook_error": return "钩子异常";
    case "compact": return "上下文压缩";
    case "finish": return "结束";
    case "unknown_tool": return "未知工具";
    default: return describeEvent(ev).label;
  }
}

function renderTrajectoryEntry(ev: AgentEvent): HTMLElement {
  const data = (ev.data ?? {}) as Record<string, unknown>;
  const step = stepOf(ev);
  const entry = h("div", { class: `traj-entry kind-${ev.kind}` });
  const head = h("div", { class: "traj-entry-head" });
  head.append(h("span", { class: "traj-kind" }, trajectoryLabel(ev)));
  if (step !== null) head.append(h("span", { class: "traj-step-tag" }, `第 ${step} 步`));
  entry.append(head);

  switch (ev.kind) {
    case "start": {
      entry.append(h("div", { class: "traj-label" }, "用户任务"));
      entry.append(rawPre(String(data.task ?? ""), "text"));
      entry.append(h("div", { class: "traj-line" },
        `model=${String(data.model ?? "?")} · tools=${Array.isArray(data.tools) ? data.tools.join(", ") : ""} · hooks=${Array.isArray(data.hooks) ? data.hooks.join(", ") : ""}`));
      if (typeof data.system === "string" && data.system) {
        entry.append(lazyDetailsBlock(
          "traj-system",
          "系统提示（完整，点击展开）",
          () => rawPre(data.system as string, "text"),
          false,
        ));
      }
      break;
    }
    case "resume": {
      entry.append(h("div", { class: "traj-line" },
        data.max_steps === 0
    ? `从第 ${data.from_step ?? "?"} 步恢复 · 不设步数上限`
    : `从第 ${data.from_step ?? "?"} 步恢复 · 再给 ${data.max_steps ?? "?"} 步 · 目标到第 ${data.until_step ?? "?"} 步`));
      break;
    }
    case "mcp_ready": {
      const servers = Array.isArray(data.servers) ? data.servers.join(", ") : "";
      const warnings = Array.isArray(data.warnings) ? data.warnings.length : 0;
      entry.append(h("div", { class: "traj-line" },
        `${data.server_count ?? 0} 个 server · ${data.tool_count ?? 0} 个工具${servers ? ` · ${servers}` : ""}${warnings ? ` · ${warnings} 条警告` : ""}`));
      break;
    }
    case "thinking": {
      entry.append(rawPre(`${textOf(ev, "text")}\n`, "thinking-text"));
      break;
    }
    case "model_call": {
      const usage = (data.usage ?? {}) as Record<string, unknown>;
      entry.append(h("div", { class: "traj-line" },
        `stop_reason=${String(data.stop_reason ?? "?")} · 入 ${usage.input_tokens ?? 0} / 出 ${usage.output_tokens ?? 0} · 工具调用 ${data.tool_calls ?? 0} 个 · 文本 ${data.text_len ?? 0} 字`));
      break;
    }
    case "tool_call": {
      const toolHead = h("div", { class: "traj-tool-head" });
      toolHead.append(h("span", { class: "traj-tool-name" }, String(data.tool ?? "?")));
      if (typeof data.exit_code === "number") {
        toolHead.append(h("span", { class: `traj-exit ${data.failed === true ? "bad" : "ok"}` },
          data.failed === true ? `exit ${data.exit_code}` : "exit 0"));
      }
      entry.append(toolHead);
      entry.append(lazyDetailsBlock(
        "traj-tool-section",
        `参数（完整）`,
        () => rawPre(prettyJson(data.args ?? {}), "json"),
        false,
      ));
      entry.append(lazyDetailsBlock(
        "traj-tool-section",
        "输出（完整，不截断）",
        () => rawPre(typeof data.output === "string" ? data.output : "(无输出)", "output"),
        false,
      ));
      if (data.meta && Object.keys(data.meta as Record<string, unknown>).length > 0) {
        entry.append(lazyDetailsBlock(
          "traj-tool-section",
          "meta",
          () => rawPre(prettyJson(data.meta), "json"),
          false,
        ));
      }
      break;
    }
    case "denied": {
      entry.append(h("div", { class: "traj-line" },
        `${String(data.reason ?? "?")}${data.detail ? ` · ${String(data.detail)}` : ""} · by ${String(data.by ?? "")}`));
      if (data.args !== undefined) entry.append(rawPre(prettyJson(data.args), "json"));
      break;
    }
    case "hook_error": {
      entry.append(rawPre(prettyJson(data), "json"));
      break;
    }
    case "compact": {
      entry.append(h("div", { class: "traj-line" },
        `${data.chars_before ?? "?"} → ${data.chars_after ?? "?"} 字符（省 ${Math.round(Number(data.saved_ratio ?? 0) * 100)}%）；落盘 ${data.persisted ?? 0}、截断 ${data.trimmed ?? 0}、删整对 ${data.snipped_messages ?? 0}`));
      break;
    }
    case "finish": {
      entry.append(h("div", { class: "traj-line" },
        `ok=${String(data.ok ?? "?")} · reason=${String(data.reason ?? "?")}`));
      break;
    }
    case "unknown_tool": {
      entry.append(h("div", { class: "traj-line" }, String(data.tool ?? "?")));
      break;
    }
    default: {
      const described = describeEvent(ev);
      entry.append(h("div", { class: "traj-line" }, described.detail));
      entry.append(rawPre(prettyJson(data), "json"));
      break;
    }
  }

  entry.append(eventRawJson(ev));
  return entry;
}

interface TrajLog {
  runId: string;
  log: HTMLElement;
  lastStep: number | null | undefined;
  pendingDelta: string;
  pendingStep: number | null;
}

let trajLive: TrajLog | null = null;
let trajFollowTail = true;
let pendingTrajEvents: AgentEvent[] = [];
let trajFlushScheduled = false;

function flushTrajDelta(state: TrajLog): void {
  if (!state.pendingDelta) return;
  const text = state.pendingDelta;
  state.pendingDelta = "";
  const entry = h("div", { class: "traj-entry kind-assistant" });
  const head = h("div", { class: "traj-entry-head" });
  head.append(h("span", { class: "traj-kind" }, "助手输出"));
  if (state.pendingStep !== null) {
    head.append(h("span", { class: "traj-step-tag" }, `第 ${state.pendingStep} 步`));
  }
  entry.append(head);
  entry.append(rawPre(`${text}\n`, "answer-text"));
  state.log.append(entry);
}

function appendTrajectoryEvent(state: TrajLog, ev: AgentEvent): void {
  const step = stepOf(ev);
  if (step !== state.lastStep) {
    flushTrajDelta(state);
    state.lastStep = step;
    if (step !== null) {
      state.log.append(h("div", { class: "traj-step-divider" }, `第 ${step} 步`));
    }
  }
  if (ev.kind === "assistant_delta") {
    state.pendingDelta += textOf(ev, "text");
    state.pendingStep = step;
    return;
  }
  flushTrajDelta(state);
  state.log.append(renderTrajectoryEntry(ev));
}

function buildTrajectoryRun(run: RunRow, index: number): { section: HTMLElement; state: TrajLog } {
  const events = run.events ?? [];
  const usage = summarizeUsage(events);
  const result = (run.result ?? {}) as Record<string, unknown>;
  const runStatus = run.status ?? "queued";
  const status = runStatus === "done" && result.ok === false ? "未完成" : runStatus;

  const section = h("section", { class: "traj-run" });
  const head = h("div", { class: "traj-run-head" });
  head.append(h("span", { class: "traj-run-index" }, `第 ${index + 1} 轮`));
  head.append(h("span", { class: "traj-run-task" }, run.task));
  head.append(h("span", { class: `traj-run-status tone-${statusTone(runStatus)}` }, status));
  head.append(h("span", { class: "traj-run-meta muted" }, formatUsage(usage)));
  section.append(head);

  const log = h("div", { class: "traj-log" });
  section.append(log);
  const state: TrajLog = {
    runId: run.id,
    log,
    lastStep: undefined,
    pendingDelta: "",
    pendingStep: null,
  };
  for (const ev of events) appendTrajectoryEvent(state, ev);
  flushTrajDelta(state);

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
  return { section, state };
}

function renderTrajectory(): void {
  main.chat.className = "chat trajectory";
  pendingTrajEvents = [];
  trajFlushScheduled = false;
  const scrollTop = main.chat.scrollTop;
  main.chat.replaceChildren();
  trajLive = null;
  if (runs.length === 0) {
    main.chat.append(h("div", { class: "empty" },
      h("p", { class: "empty-title" }, "还没有运行记录"),
      h("p", { class: "muted" }, "在下方输入任务，或从左侧选择历史会话。")));
    return;
  }
  runs.forEach((run, index) => {
    const { section, state } = buildTrajectoryRun(run, index);
    main.chat.append(section);
    if (liveRun?.id === run.id) trajLive = state;
  });
  // 跟随尾部时直接定位到最后一行；用户往上翻看时保持原位置
  main.chat.scrollTop = trajFollowTail ? main.chat.scrollHeight : scrollTop;
}

/* ------------------------------------------------------------------ 实时增量渲染（对话视图） */

interface LiveView {
  runId: string;
  planHost: HTMLElement;
  usageEl: HTMLElement;
  answerEl: HTMLElement;
  deltaText: string;
  answerStep: number | null;
}

let liveView: LiveView | null = null;

function buildLiveTurn(turn: Turn): { el: HTMLElement; view: LiveView } {
  const wrap = renderTurn(turn, true);
  // 一轮里有两个 .bubble（用户 + 助手），必须取助手那个；
  // 一轮包含用户与助手两个 .bubble；取第一个会将流式内容写入用户气泡。
  const bubble = wrap.querySelector(".msg.assistant .bubble") as HTMLElement;
  const planHost = h("div", { class: "plan-host" });
  const usageEl = h("div", { class: "turn-usage muted" });
  const answerEl = h("div", { class: "answer streaming markdown" });
  bubble.replaceChildren(planHost, usageEl, answerEl);

  const view: LiveView = {
    runId: turn.runId,
    planHost,
    usageEl,
    answerEl,
    deltaText: "",
    answerStep: null,
  };
  return { el: wrap, view };
}

function activityText(ev: AgentEvent): string {
  const step = numOf(ev, "step");
  const prefix = step === undefined ? "" : `第 ${step} 步 · `;
  switch (ev.kind) {
    case "assistant_delta": return `${prefix}模型输出中…`;
    case "thinking": return `${prefix}模型在思考…`;
    case "tool_call": {
      const call = describeToolCall(ev);
      return `${prefix}正在执行 ${call.tool}${call.summary ? `：${call.summary}` : ""}`;
    }
    case "denied": return `${prefix}工具调用被权限拒绝`;
    case "compact": return `${prefix}正在压缩上下文…`;
    case "model_call": return `${prefix}模型已返回，继续下一步…`;
    default: return "";
  }
}

let liveFooterEl: HTMLElement | null = null;
let liveFooterTextEl: HTMLElement | null = null;
let liveTimerEl: HTMLElement | null = null;
let liveStartedAt = 0;
let liveTimerHandle: number | null = null;

function updateLiveBadge(text: string): void {
  if (!text) return;
  liveStatusText = text;
  main.status.hidden = false;
  main.status.textContent = liveTimerEl
    ? `${text} · ${formatElapsed(Date.now() - liveStartedAt)}`
    : text;
}

function setLiveActivity(text: string): void {
  if (!text) return;
  liveStatusText = text;
  if (liveFooterTextEl) liveFooterTextEl.textContent = `运行中 · ${text}`;
  main.status.hidden = false;
  main.status.textContent = `${text} · ${formatElapsed(Date.now() - liveStartedAt)}`;
}

function tickLiveTimer(): void {
  if (!liveRun) return;
  const elapsed = formatElapsed(Date.now() - liveStartedAt);
  if (liveTimerEl) liveTimerEl.textContent = elapsed;
  if (liveStatusText) main.status.textContent = `${liveStatusText} · ${elapsed}`;
  main.status.title = main.status.textContent;

}

function ensureLiveFooter(): void {
  if (liveFooterEl) return;
  liveFooterEl = h("div", { class: "live-footer" });
  liveFooterTextEl = h("span", { class: "live-footer-text" },
    liveStatusText ? `运行中 · ${liveStatusText}` : "运行中 · 等待模型回复…");
  liveTimerEl = h("span", { class: "live-timer" }, "00:00");
  liveFooterEl.append(liveFooterTextEl, liveTimerEl);
}

function startLiveTimer(): void {
  if (liveTimerHandle !== null) return;
  tickLiveTimer();
  liveTimerHandle = window.setInterval(tickLiveTimer, 1000);
}

function stopLiveTimer(): void {
  if (liveTimerHandle !== null) {
    window.clearInterval(liveTimerHandle);
    liveTimerHandle = null;
  }
  liveStartedAt = 0;
  liveFooterEl = null;
  liveFooterTextEl = null;
  liveTimerEl = null;
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
    detail.open = true; // 运行中直接展开，能看见 todo 进度
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

function applyLiveEvent(ev: AgentEvent): void {
  const view = liveView;
  if (!view) return;
  if (ev.kind === "assistant_delta") {
    // 一次回复可能跨很多步；不同步的正文之间空一行，避免"上一句+最终回答"粘在一起。
    const stepNo = numOf(ev, "step") ?? null;
    if (view.deltaText && stepNo !== view.answerStep) view.deltaText += "\n\n";
    view.answerStep = stepNo;
    view.deltaText += textOf(ev, "text");
    renderLiveAnswer(view);
  } else if (ev.kind === "tool_call") {
    renderLivePlan(view, latestPlan(liveEvents));
  }
  const activity = activityText(ev);
  if (activity) setLiveActivity(activity);
  view.usageEl.textContent = formatUsage(summarizeUsage(liveEvents));
  updateTopbarUsage();
  scrollToBottomIfNearBottom();
}

function replayIntoLiveView(events: AgentEvent[], plan: PlanItem[] | null): void {
  const live = liveView as LiveView | null;
  if (!live) return;
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
  if (mode === "trajectory") trajFollowTail = true;
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
  if (busy) {
    const elapsed = liveStartedAt ? ` · ${formatElapsed(Date.now() - liveStartedAt)}` : "";
    main.status.textContent = liveStatusText ? `${liveStatusText}${elapsed}` : "执行中…";
    main.status.title = main.status.textContent;
  } else {
    liveStatusText = "";
  }
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
  if (liveRun) {
    if (!liveStartedAt) {
      liveStartedAt = typeof liveRun.created_at === "number"
        ? liveRun.created_at * 1000
        : Date.now();
    }
    ensureLiveFooter();
    if (liveFooterEl) main.chat.append(liveFooterEl);
    startLiveTimer();
  } else {
    stopLiveTimer();
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
  trajFollowTail = true;
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
  stopLiveTimer();
  liveStartedAt = typeof run.created_at === "number" ? run.created_at * 1000 : Date.now();
  const index = runs.findIndex((r) => r.id === run.id);
  if (index >= 0) runs[index] = { ...run, events: liveEvents };
  else runs = [...runs, { ...run, events: liveEvents }];

  closeStream = subscribeRun(
    run.id,
    (ev) => {
      liveEvents = [...liveEvents, ev];
      const current = runs.find((r) => r.id === run.id);
      if (current) current.events = liveEvents;
      if (viewMode === "chat") {
        applyLiveEvent(ev);
      } else if (trajLive && trajLive.runId === run.id) {
        pendingTrajEvents.push(ev);
        if (!trajFlushScheduled) {
          trajFlushScheduled = true;
          window.requestAnimationFrame(() => {
            trajFlushScheduled = false;
            const batch = pendingTrajEvents;
            pendingTrajEvents = [];
            if (!trajLive) return;
            for (const item of batch) {
              appendTrajectoryEvent(trajLive, item);
              const activity = activityText(item);
              if (activity) updateLiveBadge(activity);
            }
            if (trajFollowTail) main.chat.scrollTop = main.chat.scrollHeight;
          });
        }
      } else {
        render();
      }
    },
    (_kind, ev) => {
      const data = (ev.data ?? {}) as Record<string, unknown>;
      liveRun = null;
      stopLiveTimer();
      pendingTrajEvents = [];
      trajFlushScheduled = false;
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
  trajFollowTail = true;
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
      const resp = await api.followup(lastRun.id, task);
      runId = resp.id;
    } else {
      const resp = await api.createRun({
        task,
        workspace: workspace || undefined,
        model: main.model.value || undefined,
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
    // 流式/活动行都在对话视图里；用户上次停在轨迹时，提交后自动切回对话
    if (viewMode !== "chat") {
      viewMode = "chat";
      remember(VIEW_KEY, "chat");
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
    await api.resume(last.id);
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
  const runningConv = conversations.find((c) => c.status === "running" || c.status === "queued");
  const target = [fromHash, runningConv?.root_id, saved].find(
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

// 轨迹视图的"自动追踪最后一行"：跟丢是因为用户自己往上翻了，回到底部再恢复。
main.chat.addEventListener("scroll", () => {
  if (viewMode !== "trajectory") return;
  const elNode = main.chat;
  trajFollowTail = elNode.scrollHeight - elNode.scrollTop - elNode.clientHeight < 80;
});

window.addEventListener("hashchange", () => {
  const id = (window.location.hash.match(/^#\/c\/([0-9a-zA-Z_-]+)$/) ?? [])[1] ?? "";
  if (id && id !== conversationRoot) void openConversation(id, { silent: true });
});

void init();
