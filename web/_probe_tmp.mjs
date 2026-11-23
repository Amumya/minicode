// 探针：验证 uicheck 覆盖不到的行为（重复事件 / mcp_ready / SSE 断开 / 取消后继续）
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM, VirtualConsole } from "jsdom";

const HERE = dirname(fileURLToPath(import.meta.url));
const WEB = HERE;
const ASSETS = join(HERE, "..", "src", "minicode", "service", "web", "assets");
const jsName = readdirSync(ASSETS).find((f) => f.endsWith(".js"));
const html = readFileSync(join(WEB, "index.html"), "utf8").replace(/<script type="module"[^>]*><\/script>/, "");
const bundle = readFileSync(join(ASSETS, jsName), "utf8");

const errors = [];
const vc = new VirtualConsole();
vc.on("jsdomError", (e) => errors.push("jsdom: " + e.message));
const dom = new JSDOM(html, { runScripts: "outside-only", virtualConsole: vc, url: "http://127.0.0.1:8765/", pretendToBeVisual: true });
const { window } = dom;
const nowSec = () => Math.floor(Date.now() / 1000);
const calls = [];

window.fetch = async (url, opts = {}) => {
  const method = opts.method ?? "GET";
  calls.push(`${method} ${url}`);
  const reply = (obj) => ({ ok: true, status: 200, json: async () => obj, text: async () => JSON.stringify(obj) });
  if (url === "/healthz") return reply({ ok: true, store: "S", runs: 0 });
  if (url.startsWith("/workspaces")) return reply({ default: "/w", workspaces: [{ path: "/w", name: "w", exists: true, is_default: true }] });
  if (url.startsWith("/conversations")) return reply({ conversations: [] });
  if (url === "/runs" && method === "POST") return reply({ id: "run-new" });
  if (url.includes("/followup")) return reply({ id: "run-follow" });
  return reply({ ok: true });
};

const sources = [];
class StubEventSource {
  constructor(url) { this.url = url; this.readyState = 1; this.listeners = new Map(); sources.push(this); }
  addEventListener(k, fn) { if (!this.listeners.has(k)) this.listeners.set(k, []); this.listeners.get(k).push(fn); }
  close() { this.readyState = 2; }
  emit(k, data) { for (const fn of this.listeners.get(k) ?? []) fn({ data: JSON.stringify(data) }); }
}
window.EventSource = StubEventSource;
window.confirm = () => true;
console.error("EventSource.CLOSED 在 stub 上是:", String(StubEventSource.CLOSED));
window.eval(bundle);
const doc = window.document;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
await sleep(200);

const out = (label, v) => console.log(`\n### ${label}\n${v}`);
const ts = nowSec();

// 提交一轮
doc.querySelector("#task").value = "探针任务";
doc.querySelector("#submit").click();
await sleep(150);
const es = sources.at(-1);
out("订阅 URL", es.url);

// 1. 同一事件重复送达（模拟重连重放）
es.emit("model_call", { kind: "model_call", ts, data: { step: 1, usage: { input_tokens: 100, output_tokens: 1 } } });
await sleep(80);
out("一次 model_call 后 #usage", doc.querySelector("#usage").textContent);
es.emit("model_call", { kind: "model_call", ts, data: { step: 1, usage: { input_tokens: 100, output_tokens: 1 } } });
await sleep(80);
out("同一事件再来一次后 #usage（期望去重，实际翻倍）", doc.querySelector("#usage").textContent);

// 2. 后端会发 mcp_ready，前端 EVENT_KINDS 里没有 → 事件被静默丢弃
es.emit("mcp_ready", { kind: "mcp_ready", ts, data: { server_count: 2, tool_count: 5, servers: ["fs", "git"], warnings: [] } });
await sleep(120);
doc.querySelector("#viewTrajectory").click();
await sleep(200);
const traj = doc.querySelector("#chat")?.textContent ?? "";
out("轨迹里是否出现 MCP 连接 / 2 个 server", traj.includes("MCP 连接") + " / " + traj.includes("2 个 server"));
doc.querySelector("#viewChat").click();
await sleep(100);

// 3. SSE 永久关闭：readyState=CLOSED，前端 error 分支
es.readyState = 2;
es.emit("error", {});
await sleep(150);
out("#status 文本（期望提示断流）", JSON.stringify(doc.querySelector("#status").textContent));
out("#runStatus 文本 / hidden", JSON.stringify(doc.querySelector("#runStatus").textContent) + " / " + doc.querySelector("#runStatus").hidden);
out("#submit.disabled（断流后是否卡死）", String(doc.querySelector("#submit").disabled));
await sleep(1200);
out("1.2s 后 #runStatus（计时器还在跑？）", JSON.stringify(doc.querySelector("#runStatus").textContent));
out("1.2s 后 #status", JSON.stringify(doc.querySelector("#status").textContent));

// 4. 取消后点「继续对话」：应走 followup，实际新建 run 且丢掉历史轮次
es.emit("end", { kind: "end", ts, data: { status: "done", result: { ok: false, stop_reason: "cancelled", steps: 1 } } });
await sleep(200);
const beforeTurns = doc.querySelectorAll("#chat .turn").length;
const beforePosts = calls.filter((c) => c === "POST /runs").length;
doc.querySelector("#task").value = "接着来";
doc.querySelector("#submit").click();
await sleep(200);
out("取消后继续：POST /runs 次数变化", `${beforePosts} -> ${calls.filter((c) => c === "POST /runs").length}`);
out("取消后继续：是否走 followup", String(calls.includes("POST /runs/run-new/followup")));
out("取消后继续：轮次数变化（期望保留 1 轮）", `${beforeTurns} -> ${doc.querySelectorAll("#chat .turn").length}`);
out("取消后继续：当前订阅 run", sources.at(-1)?.url);

// 5. 编队页「运行中」写死 0
doc.querySelector("#viewTeam").click();
await sleep(200);
const stats = [...doc.querySelectorAll("#chat .team-hero-stat")].map((n) => n.textContent);
out("编队页 hero 统计项", JSON.stringify(stats));
out("页面报错", JSON.stringify(errors));
process.exit(0);
