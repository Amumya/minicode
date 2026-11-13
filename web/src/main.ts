import "./styles.css";
import { describeEvent, formatTime, summarizeResult, type AgentEvent } from "./format.ts";

const KINDS = [
  "start",
  "resume",
  "model_call",
  "tool_call",
  "denied",
  "unknown_tool",
  "finish",
  "end",
] as const;

interface RunRow {
  id: string;
  task: string;
  status: string;
  event_count: number;
}

function el<T extends HTMLElement>(id: string): T {
  const node = document.getElementById(id);
  if (!node) throw new Error(`找不到元素 #${id}`);
  return node as T;
}

const taskEl = el<HTMLTextAreaElement>("task");
const maxStepsEl = el<HTMLInputElement>("maxSteps");
const submitEl = el<HTMLButtonElement>("submit");
const statusEl = el<HTMLSpanElement>("status");
const eventsEl = el<HTMLDivElement>("events");
const answerPanel = el<HTMLElement>("answerPanel");
const answerEl = el<HTMLElement>("answer");
const historyEl = el<HTMLUListElement>("history");
const storeEl = el<HTMLSpanElement>("store");

let source: EventSource | null = null;

function setStatus(text: string, tone: "muted" | "ok" | "error" = "muted"): void {
  statusEl.textContent = text;
  statusEl.className = tone === "muted" ? "muted" : tone;
}

function clearEvents(): void {
  eventsEl.replaceChildren();
}

function addEvent(ev: AgentEvent): void {
  const d = describeEvent(ev);
  const row = document.createElement("div");
  row.className = `event tone-${d.tone}`;

  const time = document.createElement("span");
  time.className = "time";
  time.textContent = formatTime(ev.ts);

  const label = document.createElement("span");
  label.className = "label";
  label.textContent = d.label;

  const detail = document.createElement("span");
  detail.className = "detail";
  detail.textContent = d.detail;

  row.append(time, label, detail);
  eventsEl.append(row);
  // 滚到底，而不是对每一行 scrollIntoView —— 后者会把用户手动往上翻的位置顶掉
  eventsEl.scrollTop = eventsEl.scrollHeight;
}

function showAnswer(payload: Record<string, unknown>): void {
  const result = payload.result;
  answerEl.textContent =
    payload.status === "error"
      ? `出错：${String(payload.error ?? "未知错误")}`
      : summarizeResult(result);
  answerPanel.hidden = false;
  // 结果面板就在提交框下面，一般不用滚；但用户如果滚到事件区去了，这里把他拉回来。
  // jsdom 里没有 scrollIntoView，所以先判一下类型，免得测试环境直接抛异常。
  if (typeof answerPanel.scrollIntoView === "function") {
    answerPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function subscribe(runId: string): void {
  source?.close();
  clearEvents();
  answerPanel.hidden = true;
  setStatus(`订阅 run ${runId} …`);

  const es = new EventSource(`/runs/${runId}/events`);
  source = es;

  for (const kind of KINDS) {
    es.addEventListener(kind, (raw) => {
      const message = raw as MessageEvent<string>;
      let ev: AgentEvent;
      try {
        ev = JSON.parse(message.data) as AgentEvent;
      } catch {
        return;
      }
      addEvent(ev);

      if (kind === "end") {
        es.close();
        showAnswer(ev.data);
        setStatus(`run ${runId} 结束`, "ok");
        void loadHistory();
      }
    });
  }

  es.addEventListener("error", () => {
    // readyState 是 CLOSED 说明连接彻底断了，不是浏览器在自动重连
    if (es.readyState === EventSource.CLOSED) {
      setStatus("事件流断了", "error");
    }
  });
}

async function loadHistory(): Promise<void> {
  try {
    const resp = await fetch("/runs");
    const data = (await resp.json()) as { runs: RunRow[] };
    if (data.runs.length === 0) {
      historyEl.innerHTML = '<li class="muted">（空）</li>';
      return;
    }
    historyEl.replaceChildren();
    for (const run of data.runs.slice(0, 12)) {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.className = "link";
      btn.textContent = `${run.status} · ${run.event_count} 条事件 · ${run.task.slice(0, 40)}`;
      btn.addEventListener("click", () => subscribe(run.id));
      li.append(btn);
      historyEl.append(li);
    }
  } catch {
    historyEl.innerHTML = '<li class="muted">拉取历史失败</li>';
  }
}

async function loadHealth(): Promise<void> {
  try {
    const resp = await fetch("/healthz");
    const data = (await resp.json()) as { store?: string; runs?: number };
    storeEl.textContent = `store: ${data.store ?? "?"} · ${data.runs ?? 0} 个 run`;
  } catch {
    storeEl.textContent = "store: 连不上";
  }
}

async function submit(): Promise<void> {
  const task = taskEl.value.trim();
  if (!task) {
    setStatus("先写个任务", "error");
    return;
  }
  submitEl.disabled = true;
  setStatus("提交中…");
  try {
    const resp = await fetch("/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task, max_steps: Number(maxStepsEl.value) || 8 }),
    });
    if (!resp.ok) {
      const text = await resp.text();
      setStatus(`提交失败 ${resp.status}：${text.slice(0, 120)}`, "error");
      return;
    }
    const { id } = (await resp.json()) as { id: string };
    subscribe(id);
  } catch (err) {
    setStatus(`提交出错：${String(err)}`, "error");
  } finally {
    submitEl.disabled = false;
  }
}

submitEl.addEventListener("click", () => void submit());
taskEl.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") void submit();
});

void loadHealth();
void loadHistory();
