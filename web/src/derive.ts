/**
 * 从事件流派生界面所需指标。
 *
 * 设计原因：用量、计划与压缩统计均来自事件，属于工具调用与模型调用的副产物。
 * 以纯函数实现，无需修改后端接口，可直接使用 node --test 测试。
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

/** “继续跑”按钮显示条件：未完成或出错，且存在检查点（由后端校验）。 */
export function canResume(run: RunSummary | null): boolean {
  if (!run) return false;
  const status = run.status ?? "";
  if (status === "error") return true;
  if (status !== "done") return false;
  const result = rec(run.result);
  return result.ok === false;
}

/**
 * 能不能在上一轮后面接着说。
 *
 * 与 canResume 的区别：resume 表示 run 被中断后从检查点继续；
 * followup 表示当前轮已结束，新增一轮对话；两者均可获得检查点中的消息历史，
 * 但语义不同，界面上要分开显示。
 */
export function canFollowup(run: RunSummary | null): boolean {
  if (!run) return false;
  return ["done", "error", "cancelled", "exhausted"].includes(run.status ?? "");
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

/* ------------------------------------------------------------------ 子代理记录 */

export interface SubagentRecord {
  runId: string;
  step: number;
  prompt: string;
  ok: boolean;
  steps: number;
  tokens: number;
  toolCalls: number;
  output: string;
  failed: boolean;
}

/** 从当前会话的 `task` 工具调用里提取子代理记录；runs 只包含当前会话，天然按会话隔离。 */
export function subagentRecords(runs: Array<{ id: string; events?: AgentEvent[] }>): SubagentRecord[] {
  const out: SubagentRecord[] = [];
  for (const run of runs) {
    for (const ev of run.events ?? []) {
      if (ev.kind !== "tool_call") continue;
      const data = rec(ev.data);
      if (data.tool !== "task") continue;
      const args = rec(data.args);
      const meta = rec(data.meta);
      out.push({
        runId: run.id,
        step: num(data.step),
        prompt: String(args.prompt ?? ""),
        ok: meta.subagent_ok === true,
        steps: num(meta.subagent_steps),
        tokens: num(meta.subagent_tokens),
        toolCalls: num(meta.subagent_tool_calls),
        output: String(data.output ?? ""),
        failed: data.failed === true,
      });
    }
  }
  return out;
}

/* ------------------------------------------------------------------ 编队 */

export interface TeamMemberView {
  name: string;
  role: string;
  ok: boolean;
  steps: number;
  tokens: number;
  output: string;
  error: string;
}

export interface TeamView {
  runId: string;
  task: string;
  step: number;
  members: TeamMemberView[];
}

/** 从 run 的 tool_call 事件里还原 `team` 工具的编队记录（meta.team 由后端写入）。 */
export function teamViews(runs: Array<{ id: string; events?: AgentEvent[] }>): TeamView[] {
  const out: TeamView[] = [];
  for (const run of runs) {
    for (const ev of run.events ?? []) {
      if (ev.kind !== "tool_call") continue;
      const data = rec(ev.data);
      if (data.tool !== "team") continue;
      const team = rec(rec(data.meta).team);
      const raw = team.members;
      if (!Array.isArray(raw)) continue;
      out.push({
        runId: run.id,
        task: String(team.task ?? ""),
        step: num(data.step),
        members: raw.map((m) => {
          const item = rec(m);
          return {
            name: String(item.name ?? "?"),
            role: String(item.role ?? ""),
            ok: item.ok === true,
            steps: num(item.steps),
            tokens: num(item.tokens),
            output: String(item.output ?? ""),
            error: String(item.error ?? ""),
          };
        }),
      });
    }
  }
  return out;
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
