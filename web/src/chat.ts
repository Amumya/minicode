/**
 * 将 run 及事件转换为聊天界面所需的轮次数据。
 *
 * 职责：用户输入、助手输出与工具调用属于纯数据变换，可脱离 DOM 进行单元测试；
 * main.ts 仅负责渲染。
 *
 * 术语：
 *   turn：一个用户轮次（用户输入、助手执行过程与最终回答）
 *   conversation：多个轮次通过 run.parent_id 关联形成的会话
 */

import type { AgentEvent } from "./format.ts";
import { latestPlan, summarizeUsage, type PlanItem, type Usage } from "./derive.ts";

export interface RunLike {
  id: string;
  task?: string;
  status?: string;
  result?: Record<string, unknown> | null;
  error?: string | null;
  parent_id?: string | null;
  created_at?: number;
  /** 历史会话需要带上事件；实时的那一轮先空着，由 SSE 往上追加 */
  events?: AgentEvent[];
}

export interface ToolCallView {
  tool: string;
  ok: boolean;
  /** 摘要（命令/路径原文，空白已折叠） */
  summary: string;
  /** 退出码，界面用来标失败 */
  exitCode?: number;
  /** 输出预览（后端截到 400 字），界面直接显示，不用点开事件流 */
  output?: string;
  step: number;
  /** 工具来源：MCP 工具会标 "MCP"，内置工具为空 */
  source?: string;
  /** MCP 原始信息：server / 工具名，界面上显示成 `server/工具` */
  mcpServer?: string;
  mcpTool?: string;
}

export interface ThoughtView {
  /** 第几步的思考 */
  step: number;
  /** 思考原文（保留换行） */
  text: string;
}

export interface Turn {
  runId: string;
  /** 结束原因（end_turn / max_steps / cancelled / provider_error…） */
  stopReason: string;
  task: string;
  status: string;
  ok: boolean;
  answer: string;
  error: string | null;
  usage: Usage;
  plan: PlanItem[] | null;
  tools: ToolCallView[];
  thoughts: ThoughtView[];
  denied: number;
  createdAt: number;
    /** start 事件里记录的步数上限（历史 run 可能没有） */
    maxSteps?: number | null;
}

function rec(v: unknown): Record<string, unknown> {
  return typeof v === "object" && v !== null ? (v as Record<string, unknown>) : {};
}

/** 从工具调用事件里抽一句人看得懂的摘要。 */
/** 拆 `mcp__<server>__<tool>`。不是 MCP 工具返回 null。 */
function splitMcpName(name: string): { server: string; tool: string } | null {
  if (!name.startsWith("mcp__")) return null;
  const rest = name.slice(5);
  const cut = rest.indexOf("__");
  if (cut <= 0 || cut + 2 >= rest.length) return null;
  return { server: rest.slice(0, cut), tool: rest.slice(cut + 2) };
}

export function describeToolCall(ev: AgentEvent): ToolCallView {
  const d = rec(ev.data);
  const rawTool = String(d.tool ?? "?");
  const mcp = splitMcpName(rawTool);
  const tool = mcp ? `${mcp.server}/${mcp.tool}` : rawTool;
  const args = rec(d.args);
  let summary = "";
  if (typeof args.command === "string") summary = args.command;
  else if (typeof args.path === "string") summary = args.path;
  else if (typeof args.taskId === "string") summary = args.taskId;
  else if (typeof args.subject === "string") summary = args.subject;
  else if (typeof args.prompt === "string") summary = args.prompt;
  else if (typeof args.text === "string") summary = args.text;
  else if (Array.isArray(args.todos)) summary = `${args.todos.length} 条清单`;
  summary = summary.replace(/\s+/g, " ").trim();
  if (summary.length > 80) summary = summary.slice(0, 80) + "…";
  return {
    tool,
    ok: d.failed !== true,
    summary,
    exitCode: typeof d.exit_code === "number" ? d.exit_code : undefined,
    output: typeof d.output === "string" ? d.output : undefined,
    step: typeof d.step === "number" ? d.step : 0,
    source: mcp ? "MCP" : undefined,
    mcpServer: mcp?.server,
    mcpTool: mcp?.tool,
  };
}

/** 单个 run → 一轮对话。 */
export function buildTurn(run: RunLike): Turn {
  const events = run.events ?? [];
  const usage = summarizeUsage(events);
  const result = rec(run.result);
  const answer = typeof result.final_text === "string" ? result.final_text : "";
  const tools: ToolCallView[] = [];
  const thoughts: ThoughtView[] = [];
  let denied = 0;
  let maxSteps: number | null = null;
  for (const ev of events) {
    if (ev.kind === "start") {
      const d = rec(ev.data);
      if (typeof d.max_steps === "number") maxSteps = d.max_steps;
    } else if (ev.kind === "tool_call") tools.push(describeToolCall(ev));
    else if (ev.kind === "denied") denied += 1;
    else if (ev.kind === "thinking") {
      const d = rec(ev.data);
      const text = typeof d.text === "string" ? d.text : "";
      if (text.trim()) {
        thoughts.push({
          step: typeof d.step === "number" ? d.step : 0,
          text,
        });
      }
    }
  }
  return {
    runId: run.id,
    stopReason: typeof result.stop_reason === "string" ? result.stop_reason : "",
    task: run.task ?? "",
    status: run.status ?? "queued",
    ok: result.ok === true,
    answer,
    error: run.error ?? null,
    usage,
    plan: latestPlan(events),
    tools,
    thoughts,
    denied,
    createdAt: typeof run.created_at === "number" ? run.created_at : 0,
    maxSteps,
  };
}

export type RunOutcome = "ok" | "interrupted" | "budget_boundary";

/**
 * 这一轮该怎么展示结尾。
 *
 * - interrupted：后端明确说没完成（max_steps / cancelled），哪怕模型最后还吐了段文字，
 *   它也不是最终结果，界面必须先给中断提示，再标成"中断前的最后输出"。
 * - budget_boundary：模型自己在步数上限那一步 end_turn 了。任务不一定没做完，
 *   但正好卡在上限上，给用户一个"要确认/可继续"的提示。
 */
export function runOutcome(turn: Turn): RunOutcome {
  if (!turn.ok && turn.status === "done") return "interrupted";
  const limit = turn.maxSteps ?? 0;
  if (
    turn.ok &&
    turn.stopReason === "end_turn" &&
    limit > 0 &&
    turn.usage.steps >= limit
  ) {
    return "budget_boundary";
  }
  return "ok";
}

/** 一个会话的若干 run → 按时间排好的轮次。 */
export function buildTurns(runs: RunLike[]): Turn[] {
  return [...runs]
    .sort((a, b) => (a.created_at ?? 0) - (b.created_at ?? 0))
    .map(buildTurn);
}

/**
 * 把连续的 assistant_delta 合并成一条事件。
 *
 * 合并原因：流式输出会将一次回答拆分为数十条增量事件；原始事件列表
 * 若逐条展示会形成大量行，合并为一条完整文本更便于阅读。
 */
export function mergeDeltaEvents(events: AgentEvent[]): AgentEvent[] {
  const out: AgentEvent[] = [];
  let buffer: string[] = [];
  let firstTs = 0;

  const flush = () => {
    if (buffer.length === 0) return;
    out.push({ kind: "assistant_delta", ts: firstTs, data: { text: buffer.join("") } });
    buffer = [];
  };

  for (const ev of events) {
    if (ev.kind === "assistant_delta") {
      if (buffer.length === 0) firstTs = ev.ts ?? 0;
      buffer.push(String(rec(ev.data).text ?? ""));
      continue;
    }
    flush();
    out.push(ev);
  }
  flush();
  return out;
}

export interface ConversationLike {
  root_id: string;
  title: string;
  updated_at: number;
  status: string;
  turns?: number;
  workspace?: string;
}

export interface DateGroup<T> {
  label: string;
  items: T[];
}

function startOfDay(ts: number): number {
  const d = new Date(ts * 1000);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

/** 按"今天 / 昨天 / 更早"分组。左栏要用。 */
export function groupByDay<T extends { updated_at: number }>(
  items: T[],
  now: number = Date.now(),
): DateGroup<T>[] {
  const today = startOfDay(now / 1000);
  const yesterday = today - 24 * 3600 * 1000;
  const groups: DateGroup<T>[] = [
    { label: "今天", items: [] },
    { label: "昨天", items: [] },
    { label: "更早", items: [] },
  ];
  for (const item of items) {
    const day = startOfDay(item.updated_at);
    if (day >= today) groups[0].items.push(item);
    else if (day >= yesterday) groups[1].items.push(item);
    else groups[2].items.push(item);
  }
  return groups.filter((g) => g.items.length > 0);
}

/** 会话状态对应的一个小标记（左栏用圆点表示）。 */
export function statusTone(status: string): "ok" | "warn" | "error" | "muted" {
  if (status === "running" || status === "queued") return "warn";
  if (status === "error") return "error";
  if (status === "done") return "ok";
  return "muted";
}

/** 会话标题太长时截断。 */
export function shortTitle(text: string, max = 28): string {
  const clean = (text || "").replace(/\s+/g, " ").trim();
  if (!clean) return "(空任务)";
  return clean.length > max ? clean.slice(0, max) + "…" : clean;
}

/** 运行计时器的显示格式：小于 1 小时用 mm:ss，否则 h:mm:ss。 */
export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(sec).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}
