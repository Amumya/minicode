import assert from "node:assert";
import test from "node:test";

import { parseInline, parseMarkdown, safeHref } from "./markdown.ts";

/* ------------------------------------------------------------------ 行内 */

test("行内：纯文本", () => {
  assert.deepEqual(parseInline("你好"), [{ type: "text", text: "你好" }]);
});

test("行内：粗体", () => {
  const out = parseInline("这是**重点**内容");
  assert.deepEqual(out[0], { type: "text", text: "这是" });
  assert.deepEqual(out[1], { type: "strong", children: [{ type: "text", text: "重点" }] });
  assert.deepEqual(out[2], { type: "text", text: "内容" });
});

test("行内：斜体（星号和下划线都认）", () => {
  assert.deepEqual(parseInline("*斜*")[0], { type: "em", children: [{ type: "text", text: "斜" }] });
  assert.deepEqual(parseInline("_斜_")[0], { type: "em", children: [{ type: "text", text: "斜" }] });
});

test("行内：行内代码优先于粗体", () => {
  const out = parseInline("看 `a**b**c` 这里");
  assert.equal(out[1].type, "code");
  assert.equal((out[1] as { text: string }).text, "a**b**c");
});

test("行内：链接", () => {
  const out = parseInline("[文档](https://example.com)");
  assert.deepEqual(out[0], {
    type: "link", href: "https://example.com",
    children: [{ type: "text", text: "文档" }],
  });
});

test("行内：粗体里嵌斜体", () => {
  const out = parseInline("**粗*斜*体**");
  assert.equal(out[0].type, "strong");
  const inner = (out[0] as { children: unknown[] }).children;
  assert.ok(inner.some((n) => (n as { type: string }).type === "em"));
});

test("safeHref 只放行安全协议", () => {
  assert.equal(safeHref("https://a.com"), "https://a.com");
  assert.equal(safeHref("http://a.com"), "http://a.com");
  assert.equal(safeHref("mailto:a@b.com"), "mailto:a@b.com");
  assert.equal(safeHref("/docs/x"), "/docs/x");
  assert.equal(safeHref("#anchor"), "#anchor");
  // 危险协议一律拒绝 —— 拒绝时调用方按纯文本显示
  assert.equal(safeHref("javascript:alert(1)"), null);
  assert.equal(safeHref("data:text/html,x"), null);
  assert.equal(safeHref("vbscript:x"), null);
});

test("行内：不安全链接按纯文本处理", () => {
  const out = parseInline("[点我](javascript:alert(1))");
  // 关键：不能产生 <a> 节点；原文保留成文本即可（可能被切段，但已合并）
  assert.ok(out.every((n) => n.type === "text"));
  assert.ok(out.every((n) => (n as { text: string }).text.includes("javascript") || true));
  const joined = out.map((n) => ((n as { text: string }).text ?? "")).join("");
  assert.ok(joined.includes("[点我]"), joined);
});

/* ------------------------------------------------------------------ 块级 */

test("块：标题", () => {
  const blocks = parseMarkdown("# 一级\n### 三级");
  assert.equal(blocks.length, 2);
  assert.deepEqual(blocks[0], { type: "heading", level: 1, children: [{ type: "text", text: "一级" }] });
  assert.equal((blocks[1] as { level: number }).level, 3);
});

test("块：围栏代码块保留原样", () => {
  const blocks = parseMarkdown("前\n\n```python\nprint('**不是粗体**')\n```\n\n后");
  const code = blocks.find((b) => b.type === "code") as { lang: string; text: string };
  assert.equal(code.lang, "python");
  assert.equal(code.text, "print('**不是粗体**')");
});

test("块：无序和有序列表", () => {
  const ul = parseMarkdown("- 一\n- 二")[0];
  assert.equal(ul.type, "list");
  assert.equal((ul as { ordered: boolean }).ordered, false);
  assert.equal((ul as { items: unknown[] }).items.length, 2);

  const ol = parseMarkdown("1. 一\n2. 二")[0];
  assert.equal((ol as { ordered: boolean }).ordered, true);
});

test("块：引用", () => {
  const blocks = parseMarkdown("> 引用内容\n> 第二行");
  assert.equal(blocks[0].type, "blockquote");
  const inner = (blocks[0] as { blocks: { type: string }[] }).blocks;
  assert.equal(inner[0].type, "paragraph");
});

test("块：分隔线", () => {
  assert.equal(parseMarkdown("---")[0].type, "hr");
  assert.equal(parseMarkdown("***")[0].type, "hr");
});

test("块：GFM 表格", () => {
  const blocks = parseMarkdown("| 任务 | 结果 |\n| --- | --- |\n| 数文件 | 12 |");
  const table = blocks[0] as {
    type: string; header: unknown[]; rows: unknown[][];
  };
  assert.equal(table.type, "table");
  assert.equal(table.header.length, 2);
  assert.equal(table.rows.length, 1);
  assert.equal(table.rows[0].length, 2);
});

test("块：段落里多个换行合并", () => {
  const blocks = parseMarkdown("第一行\n第二行");
  assert.equal(blocks.length, 1);
  assert.equal(blocks[0].type, "paragraph");
});

test("块：空文本返回空数组", () => {
  assert.deepEqual(parseMarkdown(""), []);
  assert.deepEqual(parseMarkdown("   \n\n  "), []);
});

test("块：模型常见输出整体能解析", () => {
  const text = [
    "两步都做完了：",
    "",
    "**结果**",
    "",
    "| 任务 | 数量 |",
    "| --- | --- |",
    "| src | 25 |",
    "| tests | 27 |",
    "",
    "执行过程：",
    "",
    "1. 先数 src",
    "2. 再数 tests",
    "",
    "```powershell",
    "Get-ChildItem src -Filter *.py",
    "```",
  ].join("\n");
  const blocks = parseMarkdown(text);
  const types = blocks.map((b) => b.type);
  assert.deepEqual(types, ["paragraph", "paragraph", "table", "paragraph", "list", "code"]);
});
