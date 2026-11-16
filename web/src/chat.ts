/**
 * 把"一串 run + 它们的事件"整理成聊天界面要显示的轮次。
 *
 * 为什么单独一个模块：这部分逻辑（用户说了什么、助手答了什么、中间调了哪些工具）
 * 是纯数据变换，可以脱离 DOM 测。main.ts 只负责把它画出来。
 *
 * 术语：
 *   turn  = 一轮对话（用户一句话 + 助手做的一堆事 + 最终回答）
 *   一个会话（conversation）= 若干轮串起来，靠 run.parent_id 关联
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
  /** 一句话摘要（命令/路径原文，已折叠空白） */
  summary: string;
  /** 退出码，界面用来标失败 */
  exitCode?: number;
  /** 输出预览（后端截到 400 字），界面直接显示，不用点开事件流 */
  output?: string;
  step: number;
}

export interface Turn {
  runId: string;
  task: string;
  status: string;
  ok: boolean;
  answer: string;
  error: string | null;
  usage: Usage;
  plan: PlanItem[] | null;
  tools: ToolCallView[];
  denied: number;
  createdAt: number;
}

function rec(v: unknown): Record<string, unknown> {
  return typeof v === "object" && v !== null ? (v as Record<string, unknown>) : {};
}

/** 从工具调用事件里抽一句人看得懂的摘要。 */
export function describeToolCall(ev: AgentEvent): ToolCallView {
  const d = rec(ev.data);
  const tool = String(d.tool ?? "?");
  const args = rec(d.args);
  let summary = "";
  if (typeof args.command === "string") summary = args.command;
  else if (typeof args.path === "string") summary = args.path;
  else if (typeof args.taskId === "string") summary = args.taskId;
  else if (typeof args.subject === "string") summary = args.subject;
  else if (typeof args.prompt === "string") summary = args.prompt;
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
  };
}

/** 单个 run → 一轮对话。 */
export function buildTurn(run: RunLike): Turn {
  const events = run.events ?? [];
  const usage = summarizeUsage(events);
  const result = rec(run.result);
  const answer = typeof result.final_text === "string" ? result.final_text : "";
  const tools: ToolCallView[] = [];
  let denied = 0;
  for (const ev of events) {
    if (ev.kind === "tool_call") tools.push(describeToolCall(ev));
    else if (ev.kind === "denied") denied += 1;
  }
  return {
    runId: run.id,
    task: run.task ?? "",
    status: run.status ?? "queued",
    ok: result.ok === true,
    answer,
    error: run.error ?? null,
    usage,
    plan: latestPlan(events),
    tools,
    denied,
    createdAt: typeof run.created_at === "number" ? run.created_at : 0,
  };
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
 * 为什么要合并：流式输出时一次回答会拆成几十条增量事件，原始事件面板里
 * 就会刷出几十行"思考"，看起来像卡住了在滚屏。合并成一条完整文本更可读。
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
