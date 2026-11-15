import "./styles.css";
import { describeEvent, formatTime, summarizeResult, type AgentEvent } from "./format.ts";
import {
  buildTaskBoard,
  canFollowup,
  canResume,
  filterEvents,
  formatUsage,
  latestPlan,
  planProgress,
  summarizeUsage,
  type EventFilter,
  type TaskItem,
} from "./derive.ts";

const KINDS = [
  "start",
  "resume",
  "model_call",
  "tool_call",
  "denied",
  "unknown_tool",
  "hook_error",
  "compact",
  "finish",
  "end",
] as const;

interface RunRow {
  id: string;
  task: string;
  status: string;
  event_count: number;
  result?: Record<string, unknown> | null;
}

function el<T extends HTMLElement>(id: string): T {
  const node = document.getElementById(id);
  if (!node) throw new Error(`找不到元素 #${id}`);
  return node as T;
}

const taskEl = el<HTMLTextAreaElement>("task");
const modelEl = el<HTMLSelectElement>("model");
const maxStepsEl = el<HTMLInputElement>("maxSteps");
const optFiles = el<HTMLInputElement>("optFiles");
const optPlan = el<HTMLInputElement>("optPlan");
const optSkills = el<HTMLInputElement>("optSkills");
const optCompact = el<HTMLInputElement>("optCompact");
const optTasks = el<HTMLInputElement>("optTasks");
const submitEl = el<HTMLButtonElement>("submit");
const newChatEl = el<HTMLButtonElement>("newChat");
const statusEl = el<HTMLSpanElement>("status");
const usageEl = el<HTMLSpanElement>("usage");
const eventsEl = el<HTMLDivElement>("events");
const filtersEl = el<HTMLDivElement>("filters");
const answerPanel = el<HTMLElement>("answerPanel");
const answerEl = el<HTMLElement>("answer");
const answerMetaEl = el<HTMLElement>("answerMeta");
const resumeEl = el<HTMLButtonElement>("resume");
const planPanel = el<HTMLElement>("planPanel");
const planEl = el<HTMLOListElement>("plan");
const planProgressEl = el<HTMLSpanElement>("planProgress");
const boardPanel = el<HTMLElement>("boardPanel");
const boardEl = el<HTMLDivElement>("board");
const boardProgressEl = el<HTMLSpanElement>("boardProgress");
const historyEl = el<HTMLUListElement>("history");
const storeEl = el<HTMLSpanElement>("store");

let source: EventSource | null = null;
let events: AgentEvent[] = [];
let currentRunId = "";
let currentRun: RunRow | null = null;

/** 最近一次会话记在 localStorage 里。
 *
 * 为什么需要：界面上的"接着说"依赖内存里的 currentRun，一刷新就丢了 ——
 * 用户看到的却是"我刚聊过，接着问一句"，结果变成新会话，模型失忆。
 * 实测踩过：刷新后问"我叫什么名字"，模型答不上来。
 */
const LAST_RUN_KEY = "minicode:lastRun";

function rememberRun(runId: string): void {
  try {
    window.localStorage.setItem(LAST_RUN_KEY, runId);
  } catch {
    /* 隐私模式下 localStorage 可能不可用，忽略 */
  }
}

function forgetRun(): void {
  try {
    window.localStorage.removeItem(LAST_RUN_KEY);
  } catch {
    /* 同上 */
  }
}

function restoreRunId(): string {
  try {
    return window.localStorage.getItem(LAST_RUN_KEY) ?? "";
  } catch {
    return "";
  }
}
let filter: EventFilter = "all";
/** 任务看板刷新的防抖计时器。
 *
 * 为什么需要：一次 run 里 create/claim/complete 可能各调好几次，每调一次就拉一次
 * /runs/{id}/tasks 太吵（日志里能看到连续十几行）。攒 250ms 只拉最后一次就够了。 */
let boardTimer: ReturnType<typeof setTimeout> | null = null;

function setStatus(text: string, tone: "muted" | "ok" | "error" = "muted"): void {
  statusEl.textContent = text;
  statusEl.className = tone === "muted" ? "muted" : tone;
}

/* ------------------------------------------------------------------ 事件区 */

function renderEvents(): void {
  const visible = filterEvents(events, filter);
  eventsEl.replaceChildren();
  if (visible.length === 0) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = events.length ? "当前筛选下没有事件。" : "还没有任务。";
    eventsEl.append(p);
    return;
  }
  for (const ev of visible) {
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
  }
  eventsEl.scrollTop = eventsEl.scrollHeight;
}

function addEvent(ev: AgentEvent): void {
  events.push(ev);
  // 只对新事件做追加，避免每次重画整屏（长任务里事件可能上百条）
  if (filter === "all" || filterEvents([ev], filter).length > 0) {
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
    if (eventsEl.firstElementChild?.tagName === "P") eventsEl.replaceChildren();
    eventsEl.append(row);
    eventsEl.scrollTop = eventsEl.scrollHeight;
  }
  refreshDerived();
}

/* ------------------------------------------------------------------ 派生面板 */

function refreshDerived(): void {
  const usage = summarizeUsage(events);
  usageEl.textContent = usage.steps ? formatUsage(usage) : "";
  answerMetaEl.textContent = usage.steps ? formatUsage(usage) : "";

  const plan = latestPlan(events);
  if (plan && plan.length) {
    const prog = planProgress(plan);
    planPanel.hidden = false;
    planProgressEl.textContent = `${prog.done}/${prog.total} 完成`;
    planEl.replaceChildren();
    for (const item of plan) {
      const li = document.createElement("li");
      li.className = `plan-item status-${item.status}`;
      li.textContent = item.content;
      if (item.blockedBy?.length) {
        const tag = document.createElement("span");
        tag.className = "blocked-by";
        tag.textContent = `等 ${item.blockedBy.join(",")}`;
        li.append(tag);
      }
      planEl.append(li);
    }
  } else {
    planPanel.hidden = true;
  }
}

/** 250ms 防抖：连续的任务工具调用只触发一次看板刷新。 */
function scheduleBoardRefresh(runId: string): void {
  if (boardTimer) clearTimeout(boardTimer);
  boardTimer = setTimeout(() => {
    boardTimer = null;
    void refreshBoard(runId);
  }, 250);
}

async function refreshBoard(runId: string): Promise<void> {
  try {
    const resp = await fetch(`/runs/${runId}/tasks`);
    if (!resp.ok) return;
    const data = (await resp.json()) as {
      enabled: boolean;
      tasks: TaskItem[];
      progress: { done: number; total: number };
    };
    if (!data.enabled || !data.tasks?.length) {
      boardPanel.hidden = true;
      return;
    }
    const board = buildTaskBoard(data.tasks);
    boardPanel.hidden = false;
    boardProgressEl.textContent =
      `${data.progress.done}/${data.progress.total} 完成 · ` +
      `可开工 ${board.ready.length} · 阻塞 ${board.blocked.length}`;
    boardEl.replaceChildren();
    for (const t of data.tasks) {
      const card = document.createElement("div");
      card.className = `task status-${t.status}`;
      const head = document.createElement("div");
      head.className = "task-head";
      const id = document.createElement("code");
      id.textContent = t.id.replace("task_", "");
      const subj = document.createElement("span");
      subj.textContent = t.subject;
      head.append(id, subj);
      card.append(head);
      const meta = document.createElement("div");
      meta.className = "task-meta";
      const waiting = (t.blockedBy ?? []).filter((dep) => {
        const d = data.tasks.find((x) => x.id === dep);
        return !d || d.status !== "completed";
      });
      meta.textContent = [
        t.status,
        t.owner ? `owner ${t.owner}` : "",
        waiting.length ? `等 ${waiting.map((w) => w.replace("task_", "")).join(",")}` : "",
      ].filter(Boolean).join(" · ");
      card.append(meta);
      boardEl.append(card);
    }
  } catch {
    boardPanel.hidden = true;
  }
}

/* ------------------------------------------------------------------ 订阅 */

function showAnswer(payload: Record<string, unknown> | undefined): void {
  const p = payload ?? {};
  answerEl.textContent =
    p.status === "error"
      ? `出错：${String(p.error ?? "未知错误")}`
      : summarizeResult(p.result);
  answerPanel.hidden = false;
  if (typeof answerPanel.scrollIntoView === "function") {
    answerPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

function updateResumeButton(): void {
  const ok = canResume(currentRun);
  resumeEl.hidden = !ok;
}

/** 当前会话还能不能接着聊；同时控制"新会话"按钮和提交按钮的提示文案。 */
function updateChatMode(): void {
  const chatting = canFollowup(currentRun);
  newChatEl.hidden = !chatting;
  submitEl.textContent = chatting ? "接着说" : "提交";
}

function subscribe(runId: string, knownRun: RunRow | null = null): void {
  source?.close();
  events = [];
  currentRunId = runId;
  currentRun = knownRun;
  rememberRun(runId);
  eventsEl.replaceChildren();
  answerPanel.hidden = true;
  planPanel.hidden = true;
  boardPanel.hidden = true;
  usageEl.textContent = "";
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

      // 任务图变了就去拉一次看板（create/claim/complete 都会改状态）
      const d = (ev.data ?? {}) as Record<string, unknown>;
      if (ev.kind === "tool_call" && typeof d.tool === "string" && d.tool.endsWith("_task")) {
        scheduleBoardRefresh(runId);
      }

      if (kind === "end") {
        es.close();
        currentRun = {
          id: runId,
          task: "",
          status: String(ev.data?.status ?? "done"),
          event_count: events.length,
          result: (ev.data?.result ?? null) as Record<string, unknown> | null,
        };
        showAnswer(ev.data);
        updateResumeButton();
        updateChatMode();
        rememberRun(runId);
        void refreshBoard(runId);
        setStatus(`run ${runId} 结束`, "ok");
        void loadHistory();
      }
    });
  }

  es.addEventListener("error", () => {
    if (es.readyState === EventSource.CLOSED) setStatus("事件流断了", "error");
  });
}

/* ------------------------------------------------------------------ 数据加载 */

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
      const open = document.createElement("button");
      open.className = "link";
      open.textContent = `${run.status} · ${run.event_count} 条 · ${run.task.slice(0, 40)}`;
      open.addEventListener("click", () => {
        currentRun = run;
        subscribe(run.id);
        updateChatMode();
      });
      li.append(open);
      if (canResume(run)) {
        const again = document.createElement("button");
        again.className = "chip";
        again.textContent = "继续";
        again.addEventListener("click", () => void resumeRun(run.id));
        li.append(again);
      }
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

/* ------------------------------------------------------------------ 动作 */

async function submit(): Promise<void> {
  const task = taskEl.value.trim();
  if (!task) {
    setStatus("先写个任务", "error");
    return;
  }

  // 上一轮已经聊完：接着它继续。否则开一个新的 run。
  if (canFollowup(currentRun) && currentRun) {
    await followup(currentRun.id, task);
    return;
  }

  submitEl.disabled = true;
  setStatus("提交中…");
  try {
    const body: Record<string, unknown> = {
      task,
      max_steps: Number(maxStepsEl.value) || 8,
      include_files: optFiles.checked,
      plan: optPlan.checked,
      skills: optSkills.checked,
      compact: optCompact.checked,
      tasks: optTasks.checked,
    };
    if (modelEl.value) body.model = modelEl.value;

    const resp = await fetch("/runs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
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

async function followup(runId: string, text: string): Promise<void> {
  submitEl.disabled = true;
  setStatus("接着上一轮…");
  try {
    const resp = await fetch(`/runs/${runId}/followup`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, max_steps: Number(maxStepsEl.value) || 8 }),
    });
    if (!resp.ok) {
      const detail = await resp.text();
      setStatus(`续聊失败 ${resp.status}：${detail.slice(0, 120)}`, "error");
      return;
    }
    const { id } = (await resp.json()) as { id: string };
    taskEl.value = "";
    subscribe(id);
  } catch (err) {
    setStatus(`续聊出错：${String(err)}`, "error");
  } finally {
    submitEl.disabled = false;
  }
}

/** 开一个新会话：断开当前会话的关联，但不清历史。 */
function newChat(): void {
  source?.close();
  source = null;
  currentRun = null;
  currentRunId = "";
  events = [];
  eventsEl.replaceChildren();
  answerPanel.hidden = true;
  planPanel.hidden = true;
  boardPanel.hidden = true;
  usageEl.textContent = "";
  taskEl.value = "";
  forgetRun();
  updateChatMode();
  setStatus("新会话");
}

async function resumeRun(runId: string): Promise<void> {
  const steps = Number(maxStepsEl.value) || 8;
  setStatus(`正在从检查点继续 ${runId} …`);
  try {
    const resp = await fetch(`/runs/${runId}/resume`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max_steps: Math.max(steps, 8) }),
    });
    if (!resp.ok) {
      const text = await resp.text();
      setStatus(`继续失败 ${resp.status}：${text.slice(0, 120)}`, "error");
      return;
    }
    subscribe(runId);
  } catch (err) {
    setStatus(`继续出错：${String(err)}`, "error");
  }
}

/* ------------------------------------------------------------------ 事件绑定 */

submitEl.addEventListener("click", () => void submit());
taskEl.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") void submit();
});
newChatEl.addEventListener("click", () => newChat());
resumeEl.addEventListener("click", () => {
  if (currentRunId) void resumeRun(currentRunId);
});
filtersEl.addEventListener("click", (e) => {
  const target = e.target as HTMLElement;
  const button = target.closest("button");
  const value = button?.dataset.filter as EventFilter | undefined;
  if (!value) return;
  filter = value;
  for (const b of filtersEl.querySelectorAll("button")) {
    b.classList.toggle("active", b === button);
  }
  renderEvents();
});

/** 页面加载时恢复上次会话：把最近一次 run 拉回来，事件回放一遍，
 * 这样刷新之后直接输入下一句话仍然是"接着说"。 */
async function restoreLastRun(): Promise<void> {
  const runId = restoreRunId();
  if (!runId) return;
  try {
    const resp = await fetch(`/runs/${runId}`);
    if (!resp.ok) {
      forgetRun();
      return;
    }
    const run = (await resp.json()) as RunRow;
    if (!canFollowup(run) && !canResume(run)) {
      // 上次那个还在跑或者状态异常：不要把"接着说"挂在一个不存在的会话上
      forgetRun();
      return;
    }
    subscribe(runId, run);
    updateChatMode();
    setStatus(`已恢复上次会话（${runId}），直接输入就是接着说`);
  } catch {
    forgetRun();
  }
}

void loadHealth();
void loadHistory();
void restoreLastRun();
