/**
 * 从事件流里"算"出界面要显示的东西。
 *
 * 为什么不改后端：这些数据本来就都在事件里 —— 花费、计划、压缩效果全是
 * 工具调用和模型调用的副产物。把它们算在纯函数里，既不用动接口，
 * 也能用 `node --test` 直接测（界面渲染那一层测不了）。
 */

import type { AgentEvent } from "./format.ts";

export interface Usage {
  steps: number;
  inputTokens: number;
  outputTokens: number;
  compactions: number;
  savedChars: number;
  elapsedMs: number;
  toolCalls: number;
  denied: number;
  failures: number;
}

export interface PlanItem {
  content: string;
  status: "pending" | "in_progress" | "completed";
  blockedBy?: number[];
}

export interface TaskItem {
  id: string;
  subject: string;
  status: "pending" | "in_progress" | "completed";
  owner?: string | null;
  blockedBy?: string[];
  description?: string;
}

function rec(v: unknown): Record<string, unknown> {
  return typeof v === "object" && v !== null ? (v as Record<string, unknown>) : {};
}

function num(v: unknown): number {
  return typeof v === "number" && Number.isFinite(v) ? v : 0;
}

/** 累加花费。事件里已经有 usage、exit_code、compact 的省字符数，直接汇总即可。 */
export function summarizeUsage(events: AgentEvent[]): Usage {
  const u: Usage = {
    steps: 0,
    inputTokens: 0,
    outputTokens: 0,
    compactions: 0,
    savedChars: 0,
    elapsedMs: 0,
    toolCalls: 0,
    denied: 0,
    failures: 0,
  };
  let first = 0;
  let last = 0;
  for (const ev of events) {
    const d = rec(ev.data);
    if (typeof ev.ts === "number") {
      if (!first) first = ev.ts;
      last = ev.ts;
    }
    if (ev.kind === "model_call") {
      u.steps += 1;
      const usage = rec(d.usage);
      u.inputTokens += num(usage.input_tokens);
      u.outputTokens += num(usage.output_tokens);
    } else if (ev.kind === "tool_call") {
      u.toolCalls += 1;
      if (d.failed === true) u.failures += 1;
    } else if (ev.kind === "denied") {
      u.denied += 1;
    } else if (ev.kind === "compact") {
      u.compactions += 1;
      u.savedChars += num(d.saved_chars);
    }
  }
  if (first && last >= first) u.elapsedMs = Math.round((last - first) * 1000);
  return u;
}

/** 取最后一次 todo_write 提交的清单 —— 模型每次都传完整清单，所以"最后一次"就是当前状态。 */
export function latestPlan(events: AgentEvent[]): PlanItem[] | null {
  let latest: PlanItem[] | null = null;
  for (const ev of events) {
    if (ev.kind !== "tool_call") continue;
    const d = rec(ev.data);
    if (d.tool !== "todo_write") continue;
    const args = rec(d.args);
    const todos = args.todos;
    if (!Array.isArray(todos)) continue;
    latest = todos.map((raw) => {
      const item = rec(raw);
      const status = item.status === "in_progress" || item.status === "completed"
        ? item.status
        : "pending";
      const blockedBy = Array.isArray(item.blockedBy)
        ? item.blockedBy.map((x) => num(x)).filter((x) => x > 0)
        : [];
      return { content: String(item.content ?? ""), status, blockedBy };
    });
  }
  return latest;
}

export function planProgress(items: PlanItem[] | null): { done: number; total: number } {
  if (!items) return { done: 0, total: 0 };
  return {
    done: items.filter((i) => i.status === "completed").length,
    total: items.length,
  };
}

export type EventFilter = "all" | "tools" | "problems" | "model";

/** 事件筛选。"problems" 只留失败/被拒/钩子报错 —— 排查时最常用。 */
export function filterEvents(events: AgentEvent[], filter: EventFilter): AgentEvent[] {
  if (filter === "all") return events;
  return events.filter((ev) => {
    const d = rec(ev.data);
    if (filter === "tools") return ev.kind === "tool_call" || ev.kind === "denied";
    if (filter === "model") return ev.kind === "model_call" || ev.kind === "compact";
    return (
      ev.kind === "denied" ||
      ev.kind === "hook_error" ||
      ev.kind === "unknown_tool" ||
      (ev.kind === "tool_call" && d.failed === true) ||
      ev.kind === "end" && d.status === "error"
    );
  });
}

export interface RunSummary {
  id: string;
  task?: string;
  status?: string;
  result?: Record<string, unknown> | null;
}

/** 什么时候该显示"继续跑"按钮：没跑完、或者出错了，而且有检查点（后端会校验）。 */
export function canResume(run: RunSummary | null): boolean {
  if (!run) return false;
  const status = run.status ?? "";
  if (status === "error") return true;
  if (status !== "done") return false;
  const result = rec(run.result);
  return result.ok === false;
}

/** 把毫秒说成人话。 */
export function formatDuration(ms: number): string {
  if (!ms) return "0s";
  const s = ms / 1000;
  if (s < 60) return `${s < 10 ? s.toFixed(1) : Math.round(s)}s`;
  const m = Math.floor(s / 60);
  const rest = Math.round(s - m * 60);
  return `${m}m${rest.toString().padStart(2, "0")}s`;
}

/** 花费行：给界面顶部那一行用。 */
export function formatUsage(u: Usage): string {
  const parts = [
    `${u.steps} 步`,
    `入 ${u.inputTokens}`,
    `出 ${u.outputTokens}`,
    `工具 ${u.toolCalls}`,
    formatDuration(u.elapsedMs),
  ];
  if (u.denied) parts.push(`被拒 ${u.denied}`);
  if (u.failures) parts.push(`失败 ${u.failures}`);
  if (u.compactions) parts.push(`压缩 ${u.compactions} 次省 ${u.savedChars} 字符`);
  return parts.join(" · ");
}

/* ------------------------------------------------------------------ 任务看板 */

export interface TaskBoard {
  done: number;
  total: number;
  /** 现在可以开工的（pending 且前置全完成） */
  ready: string[];
  /** 被前置阻塞的 */
  blocked: string[];
  inProgress: string[];
}

/** 从后端给的 tasks 算出看板上的分组。依赖判定和 python 那边保持一致：读不到的前置算阻塞。 */
export function buildTaskBoard(tasks: TaskItem[]): TaskBoard {
  const byId = new Map(tasks.map((t) => [t.id, t]));
  const board: TaskBoard = { done: 0, total: tasks.length, ready: [], blocked: [], inProgress: [] };

  const blockedBy = (t: TaskItem): string[] =>
    (t.blockedBy ?? []).filter((dep) => {
      const d = byId.get(dep);
      return !d || d.status !== "completed";
    });

  for (const t of tasks) {
    if (t.status === "completed") {
      board.done += 1;
      continue;
    }
    if (t.status === "in_progress") {
      board.inProgress.push(t.id);
      continue;
    }
    if (blockedBy(t).length > 0) board.blocked.push(t.id);
    else board.ready.push(t.id);
  }
  return board;
}
