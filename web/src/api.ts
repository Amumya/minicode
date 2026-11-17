/**
 * 后端接口的薄封装。
 *
 * 为什么单独一层：原来 main.ts 里散着 6 处 fetch，每处都自己拼 body、自己判断 status。
 * 收成一层之后：接口路径只在一处出现（接线测试好写），类型也只有一处声明。
 */

import type { AgentEvent } from "./format.ts";
import type { ConversationLike, RunLike } from "./chat.ts";
import type { TaskItem } from "./derive.ts";

/**
 * 访问令牌。服务端设了 MINICODE_TOKEN 才需要；空字符串 = 不鉴权。
 * 放模块级变量而不是每次调用传参：所有接口都要用，传参会污染每个方法签名。
 */
let accessToken = "";

export function setAccessToken(token: string): void {
  accessToken = token.trim();
}

function authHeaders(): Record<string, string> {
  return accessToken ? { Authorization: `Bearer ${accessToken}` } : {};
}

export interface RunRow extends RunLike {
  event_count?: number;
  workspace?: string;
  memory?: boolean;
  mcp?: boolean;
}

export interface Workspace {
  path: string;
  name: string;
  exists: boolean;
  is_default: boolean;
}

export interface Conversation extends ConversationLike {
  run_ids: string[];
  latest_run_id: string;
  workspace?: string;
  created_at: number;
}

export interface MemoryEntry {
  id: string;
  text: string;
  tags: string[];
  created_at: number;
}

export interface CreateRunOptions {
  task: string;
  workspace?: string;
  model?: string;
  maxSteps?: number;
  includeFiles?: boolean;
  plan?: boolean;
  skills?: boolean;
  compact?: boolean;
  tasks?: boolean;
  memory?: boolean;
  mcp?: boolean;
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = { ...(init?.headers as Record<string, string> | undefined), ...authHeaders() };
  const resp = await fetch(url, { ...init, headers });
  if (!resp.ok) {
    let detail = "";
    try {
      const data = (await resp.json()) as { detail?: unknown };
      detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch {
      detail = await resp.text().catch(() => "");
    }
    throw new ApiError(resp.status, detail || `HTTP ${resp.status}`);
  }
  return (await resp.json()) as T;
}

function jsonBody(payload: unknown): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  };
}

export const api = {
  health(): Promise<{ ok: boolean; store?: string; runs?: number }> {
    return request("/healthz");
  },

  workspaces(): Promise<{ workspaces: Workspace[]; default: string }> {
    return request("/workspaces");
  },

  conversations(limit = 60): Promise<{ conversations: Conversation[] }> {
    return request(`/conversations?limit=${limit}`);
  },

  deleteConversation(rootId: string): Promise<{ root_id: string; deleted: number }> {
    return request(`/conversations/${encodeURIComponent(rootId)}`, { method: "DELETE" });
  },

  memory(workspace?: string): Promise<{
    workspace: string;
    total: number;
    entries: MemoryEntry[];
  }> {
    const query = workspace ? `?workspace=${encodeURIComponent(workspace)}` : "";
    return request(`/memory${query}`);
  },

  runs(): Promise<{ runs: RunRow[] }> {
    return request("/runs");
  },

  run(runId: string): Promise<RunRow> {
    return request(`/runs/${encodeURIComponent(runId)}`);
  },

  /** 拿一个 run 和它的全部事件（历史会话回放用）。 */
  async runWithEvents(runId: string): Promise<RunRow> {
    const data = await request<RunRow & { events?: AgentEvent[] }>(
      `/runs/${encodeURIComponent(runId)}?events=true`,
    );
    return data;
  },

  createRun(opts: CreateRunOptions): Promise<{ id: string }> {
    const body: Record<string, unknown> = {
      task: opts.task,
      include_files: opts.includeFiles ?? false,
      plan: opts.plan ?? false,
      skills: opts.skills ?? false,
      compact: opts.compact ?? false,
      tasks: opts.tasks ?? false,
      memory: opts.memory ?? false,
      mcp: opts.mcp ?? false,
    };
    if (opts.workspace) body.workspace = opts.workspace;
    if (opts.model) body.model = opts.model;
    if (opts.maxSteps) body.max_steps = opts.maxSteps;
    return request("/runs", jsonBody(body));
  },

  followup(runId: string, text: string, maxSteps?: number): Promise<{ id: string }> {
    const body: Record<string, unknown> = { text };
    if (maxSteps) body.max_steps = maxSteps;
    return request(`/runs/${encodeURIComponent(runId)}/followup`, jsonBody(body));
  },

  cancel(runId: string): Promise<{ id: string; cancelled?: boolean }> {
    return request(`/runs/${encodeURIComponent(runId)}/cancel`, jsonBody({}));
  },

  resume(runId: string, maxSteps?: number): Promise<{ id: string }> {
    return request(`/runs/${encodeURIComponent(runId)}/resume`, jsonBody({ max_steps: maxSteps }));
  },

  runTasks(runId: string): Promise<{
    enabled: boolean;
    tasks: TaskItem[];
    progress: { done: number; total: number };
  }> {
    return request(`/runs/${encodeURIComponent(runId)}/tasks`);
  },
};

export const EVENT_KINDS = [
  "start",
  "resume",
  "model_call",
  "thinking",
  "tool_call",
  "assistant_delta",
  "denied",
  "unknown_tool",
  "hook_error",
  "compact",
  "finish",
  "end",
] as const;

/** 订阅一个 run 的事件流。返回关闭函数。 */
export function subscribeRun(
  runId: string,
  onEvent: (ev: AgentEvent) => void,
  onDone: (kind: string, ev: AgentEvent) => void,
  onError?: () => void,
): () => void {
  // EventSource 不能加请求头，所以令牌走查询参数
  const suffix = accessToken ? `?token=${encodeURIComponent(accessToken)}` : "";
  const source = new EventSource(`/runs/${encodeURIComponent(runId)}/events${suffix}`);
  for (const kind of EVENT_KINDS) {
    source.addEventListener(kind, (raw) => {
      const message = raw as MessageEvent<string>;
      let ev: AgentEvent;
      try {
        ev = JSON.parse(message.data) as AgentEvent;
      } catch {
        return;
      }
      onEvent(ev);
      if (kind === "end") {
        source.close();
        onDone(kind, ev);
      }
    });
  }
  source.addEventListener("error", () => {
    if (source.readyState === EventSource.CLOSED) onError?.();
  });
  return () => source.close();
}
