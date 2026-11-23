/**
 * 把后端推来的事件翻译成"一行能看懂的字"。
 *
 * 这个文件是纯函数，不碰 DOM —— 所以能用 `node --test` 直接测。
 * 界面渲染那一层没法自动验，但事件到底显示成什么，这里说了算。
 */

export interface AgentEvent {
  kind: string;
  ts?: number;
  data: Record<string, unknown>;
}

export type Tone = "info" | "ok" | "warn" | "error";

export interface Described {
  label: string;
  detail: string;
  tone: Tone;
}

function asRecord(v: unknown): Record<string, unknown> {
  return typeof v === "object" && v !== null ? (v as Record<string, unknown>) : {};
}

function str(v: unknown, fallback = ""): string {
  if (typeof v === "string") return v;
  if (v === undefined || v === null) return fallback;
  return String(v);
}

function num(v: unknown): number | undefined {
  return typeof v === "number" ? v : undefined;
}

export function clip(s: string, max: number): string {
  const oneLine = s.replace(/\s+/g, " ").trim();
  return oneLine.length > max ? oneLine.slice(0, max) + "…" : oneLine;
}

export function formatTime(ts?: number): string {
  if (typeof ts !== "number") return "";
  const d = new Date(ts * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function describeEvent(ev: AgentEvent): Described {
  const d = asRecord(ev.data);

  switch (ev.kind) {
    case "start": {
      const tools = Array.isArray(d.tools) ? d.tools.map((t) => str(t)).join("/") : "";
      return {
        label: "开始",
        detail: `${str(d.model, "?")} · 工具 ${tools || "?"} · ${clip(str(d.task), 60)}`,
        tone: "info",
      };
    }
    case "resume": {
      return {
        label: "恢复",
        detail: d.max_steps === 0
    ? `从第 ${num(d.from_step) ?? "?"} 步恢复 · 不设步数上限`
    : `从第 ${num(d.from_step) ?? "?"} 步恢复（上限 ${num(d.max_steps) ?? "?"}）`,
        tone: "info",
      };
    }
    case "model_call": {
      const usage = asRecord(d.usage);
      const tin = num(usage.input_tokens) ?? 0;
      const tout = num(usage.output_tokens) ?? 0;
      const calls = num(d.tool_calls) ?? 0;
      return {
        label: `模型调用 #${num(d.step) ?? "?"}`,
        detail: `${str(d.stop_reason, "?")} · 入 ${tin} 出 ${tout} · 工具调用 ${calls} 个`,
        tone: "info",
      };
    }
    case "assistant_delta":
      // 增量事件仅在流式阶段出现；原始事件列表仅显示摘要，避免逐条展开。
      return { label: "模型输出", detail: clip(str(d.text), 60), tone: "info" };
      case "stream_reset":
        return { label: "流式重试", detail: "模型输出从头开始（上下文过长重试）", tone: "info" };
      case "agent_progress": {
        const name = str(d.agent, "Agent");
        const phase = str(d.phase);
        return {
          label: "Agent 进度",
          detail: phase === "done"
            ? `${name} 完成（${num(d.steps)} 步 / ${num(d.tokens)} token）`
            : `${name} 开始工作`,
          tone: phase === "done" ? "ok" : "info",
        };
      }
      case "todo_reminder":
        return { label: "计划提醒", detail: `连续 ${num(d.since) ?? "?"} 轮未更新 todo，已提醒模型`, tone: "warn" };
    case "thinking": {
      const step = num(d.step) ?? 0;
      return {
        label: step ? `思考 #${step}` : "思考",
        detail: clip(str(d.text).replace(/\s+/g, " "), 60),
        tone: "info",
      };
    }
    case "mcp_ready": {
      const servers = num(d.server_count) ?? 0;
      const tools = num(d.tool_count) ?? 0;
      const warnings = Array.isArray(d.warnings) ? d.warnings.length : 0;
      return {
        label: "MCP",
        detail: `${servers} 个 server · ${tools} 个工具` + (warnings ? ` · ${warnings} 条警告` : ""),
        tone: warnings ? "warn" : "info",
      };
    }
    case "tool_call": {
      const args = asRecord(d.args);
      const target = str(args.command) || str(args.path) || "";
      const code = num(d.exit_code);
      const failed = d.failed === true;
      return {
        label: `工具 ${str(d.tool, "?")}`,
        detail: `${clip(target, 80)}${code === undefined ? "" : ` [exit=${code}]`}`,
        tone: failed ? "warn" : "ok",
      };
    }
    case "denied": {
      return {
        label: "已拒绝",
        detail: `${str(d.reason, "?")}：${clip(str(d.detail), 70)}`,
        tone: "warn",
      };
    }
    case "unknown_tool": {
      return { label: "未知工具", detail: str(d.tool, "?"), tone: "warn" };
    }
    case "finish": {
      const ok = d.ok === true;
      return {
        label: "完成",
        detail: ok ? `完成（${str(d.reason, "?")}）` : `未完成（${str(d.reason, "?")}）`,
        tone: ok ? "ok" : "error",
      };
    }
    case "end": {
      const status = str(d.status, "?");
      return {
        label: "结束",
        detail: status === "error" ? `失败：${clip(str(d.error), 80)}` : status,
        tone: status === "error" ? "error" : "ok",
      };
    }
    default:
      return { label: ev.kind, detail: clip(JSON.stringify(d), 80), tone: "info" };
  }
}

/** 把最终结果压成一行，给界面上的"结果"区域用。 */
export function summarizeResult(result: unknown): string {
  const r = asRecord(result);
  if (Object.keys(r).length === 0) return "（没有结果）";
  const ok = r.ok === true;
  const text = str(r.final_text).trim();
  const steps = num(r.steps);
  const usage = asRecord(r.usage);
  const head = ok ? "完成" : `未完成（${str(r.stop_reason, "?")}）`;
  const cost = `步数 ${steps ?? "?"} · 入 ${num(usage.input_tokens) ?? 0} 出 ${num(usage.output_tokens) ?? 0}`;
  return text ? `${head}｜${cost}\n\n${text}` : `${head}｜${cost}`;
}
