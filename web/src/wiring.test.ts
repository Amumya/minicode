/**
 * 界面接线的静态检查。
 *
 * 为什么不跑浏览器：jsdom 不支持 `type="module"` 的脚本，真正的浏览器自动化又要下载
 * 整个 Chromium。所以这里退一步，检查最容易出错、又能静态检查的两件事：
 *
 *   1. main.ts 里取的每个元素 id，index.html 里都真的有
 *   2. main.ts 里请求的每个接口路径，后端真的提供
 *
 * 这两条错了，页面一打开就是"找不到元素 #xxx"或者一片 404。
 */

import assert from "node:assert";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const MAIN = readFileSync(join(HERE, "main.ts"), "utf8");
const HTML = readFileSync(join(HERE, "..", "index.html"), "utf8");

/** 后端实际提供的接口（和 service/app.py 里保持一致）。 */
const API_PATHS = ["/runs", "/healthz", "/api"];

function idsUsedInMain(): string[] {
  const ids = new Set<string>();
  // el<HTMLInputElement>("task") 这种写法
  for (const m of MAIN.matchAll(/\bel<[^>]+>\("([^"]+)"\)/g)) ids.add(m[1]);
  // document.getElementById("x")
  for (const m of MAIN.matchAll(/getElementById\("([^"]+)"\)/g)) ids.add(m[1]);
  return [...ids];
}

function idsInHtml(): Set<string> {
  const ids = new Set<string>();
  for (const m of HTML.matchAll(/\bid="([^"]+)"/g)) ids.add(m[1]);
  return ids;
}

test("main.ts 取的每个元素 id 都在 index.html 里", () => {
  const used = idsUsedInMain();
  assert.ok(used.length >= 5, `只解析到 ${used.length} 个 id，正则可能失效了`);
  const available = idsInHtml();
  const missing = used.filter((id) => !available.has(id));
  assert.deepEqual(missing, [], `HTML 里缺这些 id: ${missing.join(", ")}`);
});

test("main.ts 请求的接口路径都是后端有的", () => {
  const paths = new Set<string>();
  for (const m of MAIN.matchAll(/fetch\(\s*[`"']([^`"'$?]+)/g)) paths.add(m[1]);
  const unknown = [...paths].filter(
    (p) => !API_PATHS.includes(p) && !p.startsWith("/runs/"),
  );
  assert.deepEqual(unknown, [], `用了不认识的接口路径: ${unknown.join(", ")}`);
  assert.ok(paths.size >= 2, "应该至少请求 /runs 和 /healthz");
});

test("index.html 引用了构建产物入口", () => {
  assert.match(HTML, /src\/main\.ts/);
});

test("页面标题和主要区域都在", () => {
  for (const needle of ["minicode", "事件", "结果", "历史"]) {
    assert.ok(HTML.includes(needle), `index.html 里没有「${needle}」`);
  }
});
