import assert from "node:assert";
import test from "node:test";

import type { AgentEvent } from "./format.ts";
import {
  buildTurn,
  buildTurns,
  describeToolCall,
  mergeDeltaEvents,
  groupByDay,
  shortTitle,
  statusTone,
} from "./chat.ts";

function ev(kind: string, data: Record<string, unknown>, ts = 0): AgentEvent {
  return { kind, data, ts };
}

const TOOL_EVENTS: AgentEvent[] = [
  ev("start", { task: "x" }, 100),
  ev("model_call", { step: 1, usage: { input_tokens: 100, output_tokens: 10 } }, 101),
  ev("tool_call", { step: 1, tool: "bash", args: { command: "ls -la\n全部文件" }, exit_code: 0, failed: false }, 102),
  ev("tool_call", { step: 1, tool: "bash", args: { command: "npm test" }, exit_code: 1, failed: true }, 103),
  ev("denied", { step: 2, tool: "write_file", reason: "protected_file" }, 104),
  ev("tool_call", { step: 2, tool: "todo_write", args: { todos: [
    { content: "A", status: "completed" },
    { content: "B", status: "in_progress" },
  ] }, exit_code: 0, failed: false }, 105),
  ev("end", { status: "done", result: { ok: true } }, 106),
];

test("buildTurn 抽出问题、回答、工具、计划", () => {
  const turn = buildTurn({
    id: "r1",
    task: "数文件",
    status: "done",
    result: { ok: true, final_text: "一共 3 个" },
    events: TOOL_EVENTS,
  });
  assert.equal(turn.runId, "r1");
  assert.equal(turn.task, "数文件");
  assert.equal(turn.answer, "一共 3 个");
  assert.equal(turn.ok, true);
  assert.equal(turn.tools.length, 3);
  assert.equal(turn.denied, 1);
  assert.equal(turn.plan?.length, 2);
  assert.equal(turn.usage.inputTokens, 100);
});

test("buildTurn 对没事件的历史 run 也不炸", () => {
  const turn = buildTurn({ id: "x", task: "t", status: "queued" });
  assert.equal(turn.tools.length, 0);
  assert.equal(turn.answer, "");
  assert.equal(turn.plan, null);
});

test("describeToolCall 用命令做摘要并折叠空白", () => {
  const view = describeToolCall(ev("tool_call", { tool: "bash", args: { command: "ls -la\n全部文件" }, step: 3 }));
  assert.equal(view.tool, "bash");
  assert.equal(view.summary, "ls -la 全部文件");
  assert.equal(view.ok, true);
  assert.equal(view.step, 3);
});

test("describeToolCall 长摘要截断", () => {
  const view = describeToolCall(ev("tool_call", { tool: "bash", args: { command: "x".repeat(200) } }));
  assert.ok(view.summary.length <= 81);
  assert.ok(view.summary.endsWith("…"));
});

test("describeToolCall 认 path/taskId/subject/prompt/清单", () => {
  assert.equal(describeToolCall(ev("tool_call", { tool: "read_file", args: { path: "a.py" } })).summary, "a.py");
  assert.equal(describeToolCall(ev("tool_call", { tool: "claim_task", args: { taskId: "task_aa" } })).summary, "task_aa");
  assert.equal(describeToolCall(ev("tool_call", { tool: "create_task", args: { subject: "写测试" } })).summary, "写测试");
  assert.equal(describeToolCall(ev("tool_call", { tool: "task", args: { prompt: "查一下" } })).summary, "查一下");
  assert.equal(
    describeToolCall(ev("tool_call", { tool: "todo_write", args: { todos: [{ content: "a" }, { content: "b" }] } })).summary,
    "2 条清单",
  );
});

test("describeToolCall 失败标记", () => {
  const view = describeToolCall(ev("tool_call", { tool: "bash", args: { command: "x" }, failed: true }));
  assert.equal(view.ok, false);
});

test("buildTurns 按创建时间排序", () => {
  const turns = buildTurns([
    { id: "b", task: "第二", created_at: 20 },
    { id: "a", task: "第一", created_at: 10 },
  ]);
  assert.deepEqual(turns.map((t) => t.runId), ["a", "b"]);
});

test("buildTurns 没有时间戳时保持稳定", () => {
  const turns = buildTurns([{ id: "a" }, { id: "b" }]);
  assert.equal(turns.length, 2);
});

test("groupByDay 分今天/昨天/更早", () => {
  const now = new Date("2026-11-15T12:00:00+08:00").getTime();
  const sec = (iso: string) => new Date(iso).getTime() / 1000;
  const groups = groupByDay(
    [
      { id: "a", updated_at: sec("2026-11-15T09:00:00+08:00") },
      { id: "b", updated_at: sec("2026-11-14T22:00:00+08:00") },
      { id: "c", updated_at: sec("2026-11-10T09:00:00+08:00") },
    ],
    now,
  );
  assert.deepEqual(groups.map((g) => g.label), ["今天", "昨天", "更早"]);
  assert.deepEqual(groups[0].items.map((x) => x.id), ["a"]);
  assert.deepEqual(groups[1].items.map((x) => x.id), ["b"]);
  assert.deepEqual(groups[2].items.map((x) => x.id), ["c"]);
});

test("groupByDay 空分组不显示", () => {
  const now = new Date("2026-11-15T12:00:00+08:00").getTime();
  const groups = groupByDay([{ id: "a", updated_at: now / 1000 - 60 }], now);
  assert.deepEqual(groups.map((g) => g.label), ["今天"]);
});

test("statusTone 映射", () => {
  assert.equal(statusTone("running"), "warn");
  assert.equal(statusTone("queued"), "warn");
  assert.equal(statusTone("done"), "ok");
  assert.equal(statusTone("error"), "error");
  assert.equal(statusTone("其它"), "muted");
});

test("shortTitle 截断和空值", () => {
  assert.equal(shortTitle("  多   个   空格  "), "多 个 空格");
  assert.equal(shortTitle(""), "(空任务)");
  assert.equal(shortTitle("x".repeat(40)).length, 29);
});


test("describeToolCall 带上退出码和输出预览", () => {
  const view = describeToolCall(ev("tool_call", {
    tool: "bash",
    args: { command: "Get-ChildItem tests" },
    exit_code: 0,
    failed: false,
    output: "test_a.py\ntest_b.py",
  }));
  assert.equal(view.exitCode, 0);
  assert.equal(view.output, "test_a.py\ntest_b.py");
});

test("describeToolCall 没输出时 output 为 undefined", () => {
  const view = describeToolCall(ev("tool_call", { tool: "bash", args: { command: "x" } }));
  assert.equal(view.output, undefined);
});

test("describeToolCall 失败时带非零退出码", () => {
  const view = describeToolCall(ev("tool_call", {
    tool: "bash", args: { command: "exit 1" }, exit_code: 1, failed: true, output: "boom",
  }));
  assert.equal(view.ok, false);
  assert.equal(view.exitCode, 1);
});


test("mergeDeltaEvents 把连续增量合并成一条", () => {
  const events = [
    ev("model_call", { step: 1 }),
    ev("assistant_delta", { text: "你" }),
    ev("assistant_delta", { text: "好" }),
    ev("assistant_delta", { text: "！" }),
    ev("end", { status: "done" }),
  ];
  const merged = mergeDeltaEvents(events);
  assert.equal(merged.length, 3);
  assert.equal(merged[1].kind, "assistant_delta");
  assert.equal((merged[1].data as Record<string, unknown>).text, "你好！");
});

test("mergeDeltaEvents 不相邻的增量分开合并", () => {
  const events = [
    ev("assistant_delta", { text: "A" }),
    ev("model_call", { step: 1 }),
    ev("assistant_delta", { text: "B" }),
  ];
  const merged = mergeDeltaEvents(events);
  assert.equal(merged.length, 3);
});

test("mergeDeltaEvents 没有增量时原样返回", () => {
  const events = [ev("start", {}), ev("end", { status: "done" })];
  assert.deepEqual(mergeDeltaEvents(events).map((e) => e.kind), ["start", "end"]);
});
