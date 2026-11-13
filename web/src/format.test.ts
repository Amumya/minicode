import assert from "node:assert";
import test from "node:test";

import { clip, describeEvent, formatTime, summarizeResult } from "./format.ts";

test("start 显示模型和工具", () => {
  const d = describeEvent({
    kind: "start",
    data: { model: "deepseek-flash", tools: ["bash"], task: "数文件" },
  });
  assert.equal(d.label, "开始");
  assert.match(d.detail, /deepseek-flash/);
  assert.match(d.detail, /bash/);
});

test("tool_call 成功和失败的语气不一样", () => {
  const ok = describeEvent({
    kind: "tool_call",
    data: { tool: "bash", args: { command: "echo hi" }, exit_code: 0, failed: false },
  });
  assert.equal(ok.tone, "ok");
  assert.match(ok.detail, /exit=0/);

  const bad = describeEvent({
    kind: "tool_call",
    data: { tool: "bash", args: { command: "nope" }, exit_code: 1, failed: true },
  });
  assert.equal(bad.tone, "warn");
  assert.match(bad.detail, /exit=1/);
});

test("tool_call 认得 path 参数（文件工具）", () => {
  const d = describeEvent({
    kind: "tool_call",
    data: { tool: "write_file", args: { path: "a/b.txt" }, exit_code: 0 },
  });
  assert.match(d.detail, /a\/b\.txt/);
});

test("denied 带上原因", () => {
  const d = describeEvent({
    kind: "denied",
    data: { reason: "protected_file", detail: "不能改测试文件: tests/x.py" },
  });
  assert.equal(d.tone, "warn");
  assert.match(d.detail, /protected_file/);
});

test("finish 按 ok 区分语气", () => {
  assert.equal(describeEvent({ kind: "finish", data: { ok: true, reason: "end_turn" } }).tone, "ok");
  assert.equal(
    describeEvent({ kind: "finish", data: { ok: false, reason: "max_steps" } }).tone,
    "error",
  );
});

test("end 状态是 error 时语气为 error", () => {
  const d = describeEvent({ kind: "end", data: { status: "error", error: "端点挂了" } });
  assert.equal(d.tone, "error");
  assert.match(d.detail, /端点挂了/);
});

test("未知事件不会炸", () => {
  const d = describeEvent({ kind: "whatever", data: { a: 1 } });
  assert.equal(d.label, "whatever");
  assert.match(d.detail, /"a":1/);
});

test("clip 会把多行压成一行并截断", () => {
  const out = clip("a\nb\nc" + "x".repeat(100), 10);
  assert.equal(out.includes("\n"), false);
  assert.ok(out.endsWith("…"));
});

test("summarizeResult 带出文本和花费", () => {
  const s = summarizeResult({
    ok: true,
    final_text: "15",
    steps: 2,
    usage: { input_tokens: 381, output_tokens: 171 },
  });
  assert.match(s, /完成/);
  assert.match(s, /步数 2/);
  assert.match(s, /15/);
});

test("summarizeResult 对空结果有兜底", () => {
  assert.equal(summarizeResult(null), "（没有结果）");
  assert.equal(summarizeResult({}), "（没有结果）");
});

test("formatTime 输出 HH:MM:SS", () => {
  assert.match(formatTime(1794560284.5), /^\d{2}:\d{2}:\d{2}$/);
  assert.equal(formatTime(undefined), "");
});
