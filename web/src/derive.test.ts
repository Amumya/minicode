import assert from "node:assert";
import test from "node:test";

import type { AgentEvent } from "./format.ts";
import {
  buildTaskBoard,
  canResume,
  filterEvents,
  formatDuration,
  formatUsage,
  latestPlan,
  planProgress,
  summarizeUsage,
} from "./derive.ts";

function ev(kind: string, data: Record<string, unknown>, ts = 0): AgentEvent {
  return { kind, data, ts };
}

const SAMPLE: AgentEvent[] = [
  ev("start", { task: "x", model: "m", tools: ["bash"] }, 100),
  ev("model_call", { step: 1, stop_reason: "tool_use", usage: { input_tokens: 100, output_tokens: 20 } }, 101),
  ev("tool_call", { step: 1, tool: "bash", args: { command: "echo hi" }, exit_code: 0, failed: false }, 102),
  ev("compact", { saved_chars: 500, persisted: 1, trimmed: 0, snipped_messages: 0 }, 103),
  ev("tool_call", { step: 2, tool: "bash", args: { command: "nope" }, exit_code: 1, failed: true }, 105),
  ev("denied", { step: 2, tool: "bash", reason: "protected_file", detail: "不许改测试" }, 106),
  ev("model_call", { step: 3, stop_reason: "end_turn", usage: { input_tokens: 50, output_tokens: 5 } }, 110),
];

test("summarizeUsage 汇总花费", () => {
  const u = summarizeUsage(SAMPLE);
  assert.equal(u.steps, 2);
  assert.equal(u.inputTokens, 150);
  assert.equal(u.outputTokens, 25);
  assert.equal(u.toolCalls, 2);
  assert.equal(u.failures, 1);
  assert.equal(u.denied, 1);
  assert.equal(u.compactions, 1);
  assert.equal(u.savedChars, 500);
  // 100 → 110 秒
  assert.equal(u.elapsedMs, 10_000);
});

test("summarizeUsage 对空事件是零", () => {
  const u = summarizeUsage([]);
  assert.equal(u.steps, 0);
  assert.equal(u.elapsedMs, 0);
});

test("formatUsage 把关键项拼成一行", () => {
  const line = formatUsage(summarizeUsage(SAMPLE));
  assert.match(line, /2 步/);
  assert.match(line, /入 150/);
  assert.match(line, /被拒 1/);
  assert.match(line, /压缩 1 次省 500 字符/);
});

test("formatDuration 分级", () => {
  assert.equal(formatDuration(0), "0s");
  assert.equal(formatDuration(3_400), "3.4s");
  assert.equal(formatDuration(45_000), "45s");
  assert.equal(formatDuration(95_000), "1m35s");
});

test("latestPlan 取最后一次清单", () => {
  const events = [
    ev("tool_call", { tool: "todo_write", args: { todos: [{ content: "A", status: "in_progress" }] } }),
    ev("tool_call", { tool: "todo_write", args: { todos: [
      { content: "A", status: "completed" },
      { content: "B", status: "in_progress", blockedBy: [1] },
    ] } }),
  ];
  const plan = latestPlan(events);
  assert.ok(plan);
  assert.equal(plan.length, 2);
  assert.equal(plan[1].status, "in_progress");
  assert.deepEqual(plan[1].blockedBy, [1]);
});

test("latestPlan 没有清单时返回 null", () => {
  assert.equal(latestPlan(SAMPLE), null);
});

test("latestPlan 忽略坏数据", () => {
  const plan = latestPlan([ev("tool_call", { tool: "todo_write", args: { todos: "oops" } })]);
  assert.equal(plan, null);
});

test("planProgress 统计完成数", () => {
  assert.deepEqual(planProgress(null), { done: 0, total: 0 });
  assert.deepEqual(
    planProgress([
      { content: "A", status: "completed" },
      { content: "B", status: "pending" },
    ]),
    { done: 1, total: 2 },
  );
});

test("filterEvents: all 原样返回", () => {
  assert.equal(filterEvents(SAMPLE, "all").length, SAMPLE.length);
});

test("filterEvents: tools 只要工具和被拒", () => {
  const out = filterEvents(SAMPLE, "tools");
  assert.deepEqual(out.map((e) => e.kind), ["tool_call", "tool_call", "denied"]);
});

test("filterEvents: problems 只要失败和被拒", () => {
  const out = filterEvents(SAMPLE, "problems");
  assert.deepEqual(out.map((e) => e.kind), ["tool_call", "denied"]);
});

test("filterEvents: model 要模型调用和压缩", () => {
  const out = filterEvents(SAMPLE, "model");
  assert.deepEqual(out.map((e) => e.kind), ["model_call", "compact", "model_call"]);
});

test("canResume 只在没完成或出错时为真", () => {
  assert.equal(canResume(null), false);
  assert.equal(canResume({ id: "a", status: "running" }), false);
  assert.equal(canResume({ id: "a", status: "done", result: { ok: true } }), false);
  assert.equal(canResume({ id: "a", status: "done", result: { ok: false } }), true);
  assert.equal(canResume({ id: "a", status: "error" }), true);
});

test("buildTaskBoard 分出可开工/阻塞/进行中", () => {
  const board = buildTaskBoard([
    { id: "t1", subject: "A", status: "completed" },
    { id: "t2", subject: "B", status: "pending", blockedBy: ["t1"] },
    { id: "t3", subject: "C", status: "pending", blockedBy: ["t9"] },
    { id: "t4", subject: "D", status: "in_progress" },
  ]);
  assert.equal(board.total, 4);
  assert.equal(board.done, 1);
  assert.deepEqual(board.inProgress, ["t4"]);
  // t2 的前置已完成 → 可开工；t3 的前置不存在 → 算阻塞（和 python 侧一致）
  assert.deepEqual(board.ready, ["t2"]);
  assert.deepEqual(board.blocked, ["t3"]);
});

test("buildTaskBoard 空看板", () => {
  const board = buildTaskBoard([]);
  assert.equal(board.total, 0);
  assert.deepEqual(board.ready, []);
});
