"""HTTP 服务层。

几个设计决定：

1. **run 在后台线程里跑，接口立刻返回 id。**
   一个任务要几十秒，同步等会把 HTTP 请求挂死。

2. **事件用 SSE 推，不用 WebSocket。**
   只需要服务端往客户端单向推；SSE 还能用 `curl` 直接验证。
   事件都存在 store 里（内存或 Redis），所以断线重连能从上次的位置接着推。

3. **每轮结束存一次检查点，所以能接着跑。**
   `POST /runs/{id}/resume` 会从最后一个检查点继续，而不是从头开始。
   进程被杀掉也不怕 —— 检查点在 store 里，重启后还在。

4. **默认只监听 127.0.0.1。**
   这个服务没有鉴权，别往公网开。

存储由 `MINICODE_REDIS_URL` 决定：设了就用 Redis，没设就用内存。
内存版重启就丢，这个限制是显式的，不是猜的。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from minicode import __version__
from minicode.compact import CompactConfig, Compactor, provider_summarizer
from minicode.config import Settings
from minicode.events import Event, Recorder
from minicode.loop import run as run_loop
from minicode.memory import MemoryStore
from minicode.prompts import default_system
from minicode.skills import SkillLoader
from minicode.tasks import TaskStore
from minicode.service.store import (
    InMemoryStore,
    RedisStore,
    RunState,
    SessionStore,
    new_id,
)
from minicode.tools import TodoList, build_registry

ProviderFactory = Callable[[Settings], object]

# 前端构建产物位于包目录旁（输出目录由 web/vite.config.ts 配置），
# 无论从源码运行还是以安装包运行，路径均一致。
WEB_DIR = Path(__file__).resolve().parent / "web"


def group_conversations(runs: list["RunState"]) -> dict[str, list["RunState"]]:
    """把 run 按 parent_id 串成会话，返回 {根 run id: 成员}。

    「沿着 parent_id 往上找根」这段逻辑 /conversations 和 DELETE /conversations 都要用，
    所以提出来放模块级。
    """
    by_id = {r.id: r for r in runs}
    groups: dict[str, list[RunState]] = {}

    def root_of(run: RunState) -> str:
        current = run
        seen = {current.id}
        while current.parent_id and current.parent_id in by_id:
            if current.parent_id in seen:
                break
            current = by_id[current.parent_id]
            seen.add(current.id)
        return current.id

    for run in runs:
        groups.setdefault(root_of(run), []).append(run)
    return groups


def is_usable_workspace(path: str) -> bool:
    """这个工作区值得放进下拉框吗。

    跑测试时会给 run 传 tmp_path，这些路径进了下拉框会占满列表
    （实测出现过一整屏 pytest-of-*）。这里只挡明显的临时/测试目录。
    """
    low = (path or "").lower().replace("\\", "/")
    if not low:
        return False
    junk = ("pytest-of-", "/tmp/", "appdata/local/temp", "/temp/")
    return not any(token in low for token in junk)


class RunRequest(BaseModel):
    task: str = Field(min_length=1, max_length=20000, description="任务内容")
    model: str | None = None
    workspace: str | None = None
    max_steps: int | None = Field(default=None, ge=0)
    include_files: bool = False
    plan: bool = False
    skills: bool = False
    compact: bool = False
    tasks: bool = False
    memory: bool = False
    mcp: bool = False


class FollowupRequest(BaseModel):
    """多轮对话：在某个已结束的 run 后面接着说一句。"""
    text: str = Field(min_length=1, max_length=8000)
    max_steps: int | None = Field(default=None, ge=0)


class CancelRequest(BaseModel):
    """取消正在跑的 run。空对象即可，留这个类是为了以后能带原因。"""
    reason: str = ""


class ResumeRequest(BaseModel):
    max_steps: int | None = Field(default=None, ge=0)


def default_store() -> SessionStore:
    url = os.environ.get("MINICODE_REDIS_URL", "").strip()
    if url:
        return RedisStore(url)
    return InMemoryStore()


def create_app(
    *,
    provider_factory: ProviderFactory | None = None,
    settings: Settings | None = None,
    store: SessionStore | None = None,
    default_workspace: Path | None = None,
    token: str | None = None,
) -> FastAPI:
    app = FastAPI(title="minicode", version=__version__)
    st = store or default_store()
    # --reload 模式下 uvicorn 用 import 字符串重建 app，拿不到参数，
    # 所以默认工作区允许从环境变量来。
    env_workspace = os.environ.get("MINICODE_WORKSPACE", "").strip()
    base_workspace = Path(default_workspace or env_workspace or Path.cwd()).resolve()

    # ---- 访问令牌 ----
    # 设了 MINICODE_TOKEN（或显式传 token）才启用；空 = 不鉴权（默认，配合只监听 127.0.0.1）。
    # 浏览器里 EventSource 不能自定义请求头，所以除了 Authorization 还接受 ?token=。
    expected_token = (token if token is not None else os.environ.get("MINICODE_TOKEN", "")).strip()
    if expected_token:
        import hmac

        # 这几个不鉴权：页面骨架、静态资源、健康检查（否则浏览器连页面都打不开）
        def _public_path(path: str) -> bool:
            return path in ("/", "/healthz") or path.startswith("/assets/") or path == "/favicon.ico"

        def _presented_token(request: Request) -> str:
            header = request.headers.get("authorization", "")
            if header.lower().startswith("bearer "):
                return header[7:].strip()
            return request.query_params.get("token", "")

        @app.middleware("http")
        async def _require_token(request: Request, call_next):
            if _public_path(request.url.path):
                return await call_next(request)
            if not hmac.compare_digest(_presented_token(request), expected_token):
                return JSONResponse(
                    {"detail": "需要访问令牌（Authorization: Bearer <token>，或给 SSE 加 ?token=）"},
                    status_code=401,
                )
            return await call_next(request)

    @app.middleware("http")
    async def _no_store_shell(request: Request, call_next):
        """index.html 不允许缓存：更新前端后刷新就要拿到新页面骨架。"""
        response = await call_next(request)
        if request.url.path in ("/", "/index.html"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
        return response

          
# 每个 run 对应一个 asyncio.Event，用于唤醒 SSE 生成器。
          
# 使用独立字典存储，使 store 仅负责数据，不参与异步控制。
    changed: dict[str, asyncio.Event] = {}

      
# 本进程内正在执行的 run，用于：
      
#   1) 进程终止后，store 中 status=running 的 run 不会再有 worker 处理；
      
#      通过该集合区分“正在执行”与“残留状态”，残留状态允许 resume。
      
#   2) followup 不得挂到正在执行的 run 上，否则会从半截检查点分叉出并发 run。
    active_runs: set[str] = set()

    # 每个 run 一个取消信号；worker 在每步开始时检查。
    cancel_flags: dict[str, threading.Event] = {}

    def _factory(settings_obj: Settings):
        if provider_factory is not None:
            return provider_factory(settings_obj)
        from minicode.provider import AnthropicProvider

        return AnthropicProvider(settings_obj)

    def _emit(state: RunState, cs: asyncio.Event, ev: Event, loop_ref) -> None:
        # event_count 一律由 store.append_event 维护 —— 之前这里又 +=1，
        # 内存版 store 自己也加，导致"事件 3 条但计数 4"。
        st.append_event(state.id, {"kind": ev.kind, "ts": ev.ts, "data": ev.data})
        state.updated_at = ev.ts
        try:
            loop_ref.call_soon_threadsafe(cs.set)
        except RuntimeError:
            # 事件循环已关（服务正在退出），丢掉这条即可
            pass

    def _skills_for(state: RunState) -> SkillLoader | None:
        return SkillLoader(Path(state.workspace) / "skills") if state.skills else None

    def _tasks_for(state: RunState):
        return TaskStore(Path(state.workspace) / ".minicode" / "tasks") if state.tasks else None

    def _memory_for(state: RunState) -> MemoryStore | None:
        if not state.memory:
            return None
        return MemoryStore(Path(state.workspace) / ".minicode" / "memory.jsonl")

    def _worker(state: RunState, cs: asyncio.Event, loop_ref, run_settings: Settings,
                resume: dict | None) -> None:
        mcp_session = None
        mcp_servers: list[str] = []
        # 所有可能抛异常的初始化都放进 try：任一步失败也要走 finally，
        # 否则 run 永远停在 queued/running，active_runs 里留下僵尸 id。
        try:
            active_runs.add(state.id)
            cancel_flags[state.id] = threading.Event()
            state.status = "running"
            st.update(state)
            rec = Recorder(on_event=lambda ev: _emit(state, cs, ev, loop_ref))

            def on_step(payload: dict) -> None:
                try:
                    st.save_checkpoint(state.id, payload)
                except Exception:  # noqa: BLE001 - 存检查点失败不能拖垮整个 run
                    pass
                st.update(state)

            todo = TodoList() if state.plan else None
            skills_loader = _skills_for(state)
            # provider 只建一次：摘要器和主循环共用同一个客户端，一次 run 里不该有两份连接。
            run_provider = _factory(run_settings)
            compactor = (
                Compactor(
                    Path(state.workspace),
                    CompactConfig(),
                    summarize=provider_summarizer(run_provider),
                )
                if state.compact else None
            )

            mcp_tools: list = []
            if state.mcp:
                from minicode.tools.mcp import connect_mcp

                mcp_session = connect_mcp(Path(state.workspace))
                mcp_tools = list(mcp_session.tools)
                mcp_servers = [item["server"] for item in mcp_session.report["servers"]]
                rec.add(
                    "mcp_ready",
                    server_count=len(mcp_servers),
                    tool_count=len(mcp_tools),
                    servers=mcp_session.report["servers"],
                    warnings=mcp_session.report["warnings"],
                )

            result = run_loop(
                state.task,
                provider=run_provider,
                tools=build_registry(
                    files=state.include_files, todo=todo,
                    skills=skills_loader, tasks=_tasks_for(state),
                    extra=list(mcp_tools),
                    memory=_memory_for(state),
                    compact=state.compact),
                settings=run_settings,
                workspace=Path(state.workspace),
                recorder=rec,
                system=default_system(
                    skills=skills_loader,
                    python_hint=sys.executable,
                    plan_hint=state.plan,
                    compact_hint=state.compact,
                    tasks_hint=state.tasks,
                    mcp_servers=mcp_servers or None,
                    memory_hint=state.memory,
                    memory_snapshot=(
                        _memory_for(state).snapshot(state.task)
                        if state.memory else None
                    ),
                ),
                resume=resume,
                on_step=on_step,
                compactor=compactor,
                should_stop=lambda: cancel_flags.get(state.id) is not None
                and cancel_flags[state.id].is_set(),
            )
            state.result = result.to_dict()
            if todo is not None:
                state.result["todos"] = todo.snapshot()
            if result.stop_reason == "provider_error":
                # 模型调用失败属于基础设施错误，应与“执行完成但未成功”区分：
                # 前者要标 error（界面会红），后者是 ok=False 的正常收尾。
                state.error = result.error or "模型调用失败"
                state.status = "error"
            elif result.stop_reason == "cancelled":
                state.status = "cancelled"
            elif result.stop_reason == "max_steps":
                state.status = "exhausted"
            else:
                state.status = "done"
        except Exception as exc:  # noqa: BLE001 - 后台线程的异常必须自己接住
            state.error = f"{type(exc).__name__}: {exc}"
            state.status = "error"
        finally:
            if mcp_session is not None:
                mcp_session.close()
            active_runs.discard(state.id)
            cancel_flags.pop(state.id, None)
            st.update(state)
            try:
                loop_ref.call_soon_threadsafe(cs.set)
            except RuntimeError:
                pass
            # 先唤醒等待者再 pop：等待者手里已有旧对象，pop 不影响它收到通知；
            # 新来的 SSE 连接会 setdefault 出新的 Event。
            changed.pop(state.id, None)

    def _start(state: RunState, run_settings: Settings, resume: dict | None) -> None:
        loop_ref = asyncio.get_running_loop()
        # 复用已有 Event：resume 时旧的 SSE 连接还在等它，覆盖掉会让旧连接错过唤醒。
        cs = changed.setdefault(state.id, asyncio.Event())
        threading.Thread(
            target=_worker,
            args=(state, cs, loop_ref, run_settings, resume),
            daemon=True,
            name=f"run-{state.id}",
        ).start()

    def _resolve_settings(model: str | None, max_steps: int | None) -> Settings:
        base = settings or Settings.from_env(base_workspace / ".env")
        return Settings(
            api_key=base.api_key,
            base_url=base.base_url,
            model=model or base.model,
            max_steps=max_steps if max_steps is not None else base.max_steps,
            max_tokens=base.max_tokens,
        )

    @app.get("/healthz")
    def healthz() -> dict:
        runs = st.all()
        return {
            "ok": True,
            "version": __version__,
            "runs": len(runs),
            "store": type(st).__name__,
        }

    def _resolve_workspace(raw: str | None) -> Path:
        """解析并校验工作区。

        为什么要校验：界面上允许手填路径，填错了不该在后台线程里炸 ——
        那样 run 会变成 error，用户还得翻事件才能看出是路径问题。
        """
        if not raw or not str(raw).strip():
            return base_workspace
        candidate = Path(str(raw).strip()).expanduser()
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise HTTPException(400, f"工作区路径无效: {raw}（{exc}）") from exc
        if not resolved.is_dir():
            raise HTTPException(400, f"工作区不存在或不是目录: {resolved}")
        return resolved

    @app.get("/workspaces")
    def list_workspaces(limit: int = 10) -> dict:
        """最近用过的工作区 + 默认工作区。左栏用它做下拉。

        来源就是历史 run 里的 workspace —— 不额外维护一份配置，
        免得"删了 run 但配置里还留着"这种不一致。
        """
        seen: list[str] = []
        for run in sorted(st.all(), key=lambda r: r.updated_at, reverse=True):
            if run.workspace and run.workspace not in seen and is_usable_workspace(run.workspace):
                seen.append(run.workspace)

        items = []
        default = str(base_workspace)
        ordered = [default] + [p for p in seen if p != default]
        for path in ordered[: max(0, limit)]:
            p = Path(path)
            items.append({
                "path": path,
                "name": p.name or path,
                "exists": p.is_dir(),
                "is_default": path == default,
            })
        return {"workspaces": items, "default": default}

    def _memory_path(workspace: str | None) -> Path:
        base = _resolve_workspace(workspace)
        return base / ".minicode" / "memory.jsonl"

    @app.get("/memory")
    def list_memory(workspace: str | None = None, limit: int = 50) -> dict:
        """读某个工作区的长期记忆（界面右栏用）。"""
        ws = _resolve_workspace(workspace).resolve()
        store = MemoryStore(ws / ".minicode" / "memory.jsonl")
        entries = store.entries()
        entries.sort(key=lambda e: e.created_at, reverse=True)
        picked = entries[: max(0, min(limit, 200))]
        return {
            "workspace": str(ws),
            "total": len(entries),
            "entries": [e.to_dict() for e in picked],
        }

    @app.delete("/memory/{entry_id}")
    def delete_memory(entry_id: str, workspace: str | None = None) -> dict:
        """删一条记忆（记错了/过时了要能清）。"""
        ws = _resolve_workspace(workspace).resolve()
        store = MemoryStore(ws / ".minicode" / "memory.jsonl")
        if not store.forget(entry_id):
            raise HTTPException(404, "没有这条记忆")
        return {"id": entry_id, "deleted": True}

    @app.delete("/conversations/{root_id}")
    def delete_conversation(root_id: str) -> dict:
        """删掉一个会话（连同它的全部 run、事件、检查点）。

        会话在存储里就是一条 parent_id 链，所以"删会话"= 找到链上所有 run 逐个删。
        """
        groups = group_conversations(st.all())
        members = groups.get(root_id)
        if not members:
            raise HTTPException(404, "没有这个会话")
        busy = [r.id for r in members if r.status in ("running", "queued") and r.id in active_runs]
        if busy:
            raise HTTPException(409, "会话里有正在执行的任务，请先停止")
        deleted = [r.id for r in members if st.delete(r.id)]
        for run_id in deleted:
            changed.pop(run_id, None)
            cancel_flags.pop(run_id, None)
        return {"root_id": root_id, "deleted": len(deleted), "run_ids": deleted}

    @app.get("/conversations")
    def list_conversations(limit: int = 60) -> dict:
        """把 run 按 parent_id 串成"会话"，按最近活动排序。

        为什么不在前端算：这个「沿着 parent_id 往上找根」的逻辑如果在界面里写，
        数据一多每次渲染都要重复算；放后端一次算完，前端只负责画。
        """
        groups = group_conversations(st.all())

        out = []
        for root_id, members in groups.items():
            members.sort(key=lambda r: (r.created_at, r.id))
            latest = members[-1]
            out.append({
                "root_id": root_id,
                "title": (members[0].task or "").strip()[:80],
                "workspace": members[0].workspace,
                "run_ids": [r.id for r in members],
                "latest_run_id": latest.id,
                "status": latest.status,
                "created_at": members[0].created_at,
                "updated_at": max(r.updated_at for r in members),
                "turns": len(members),
            })
        out.sort(key=lambda c: c["updated_at"], reverse=True)
        return {"conversations": out[: max(0, limit)]}

    @app.post("/runs", status_code=202)
    async def create_run(req: RunRequest) -> dict:
        run_settings = _resolve_settings(req.model, req.max_steps)
        if provider_factory is None and not run_settings.api_key:
            raise HTTPException(400, "没有 API key，先把 .env 配好")

        workspace = _resolve_workspace(req.workspace)
        state = RunState(
            id=new_id(),
            task=req.task,
            model=run_settings.model,
            workspace=str(workspace.resolve()),
            include_files=req.include_files,
            plan=req.plan,
            skills=req.skills,
            compact=req.compact,
            tasks=req.tasks,
            memory=req.memory,
            mcp=req.mcp,
        )
        st.create(state)
        _start(state, run_settings, resume=None)
        return {"id": state.id, "status": state.status, "model": state.model}

    @app.post("/runs/{run_id}/resume", status_code=202)
    async def resume_run(run_id: str, req: ResumeRequest | None = None) -> dict:
        state = st.get(run_id)
        if state is None:
            raise HTTPException(404, "没有这个 run")
        if state.status == "running" and state.id in active_runs:
            raise HTTPException(409, "这个 run 还在跑，不用恢复")
        # 状态为 running 但本进程无对应 worker，说明进程被终止或重启后遗留。
        # 存在检查点则允许续跑；否则该 run 无法恢复。
        if state.status == "running":
            checkpoint = st.get_checkpoint(run_id)
            if checkpoint is None:
                raise HTTPException(409, "这个 run 停在 running 但没有检查点，只能重新跑")
        # done 有两种：正常收尾（result.ok=True）和步数耗尽被中断（ok=False）。
        # 前者不该再 resume —— 会在没有新用户输入的情况下再调一次模型，要接着聊请用 followup；
        # 后者正是 resume 的用途。
        if state.status == "done" and (state.result or {}).get("ok"):
            raise HTTPException(409, "这个 run 已经正常跑完了，要接着聊请用 followup 接口")
        checkpoint = st.get_checkpoint(run_id)
        if not checkpoint:
            raise HTTPException(409, "没有检查点，只能从头跑")

        run_settings = _resolve_settings(
            state.model, (req.max_steps if req else None))
        if provider_factory is None and not run_settings.api_key:
            raise HTTPException(400, "没有 API key，先把 .env 配好")
        state.status = "queued"
        state.result = None
        state.error = None
        st.update(state)
        _start(state, run_settings, resume=checkpoint)
        return {
            "id": state.id,
            "status": state.status,
            "resumed_from_step": checkpoint.get("step"),
        }

    @app.post("/runs/{run_id}/cancel", status_code=202)
    async def cancel_run(run_id: str, req: CancelRequest | None = None) -> dict:
        """请求取消一个正在跑的 run。

        返回 202 而不是 200：这里只是把旗子插上，真正停下来要等当前这步结束。
        """
        state = st.get(run_id)
        if state is None:
            raise HTTPException(404, "没有这个 run")
        if state.status not in ("running", "queued"):
            raise HTTPException(409, f"这个 run 已经是 {state.status}，不用取消")
        flag = cancel_flags.get(run_id)
        if flag is None:
            # 状态为 running 但本进程无对应 worker：属于残留状态，直接标记为取消。
            state.status = "error"
            state.error = "已取消（没有活跃的 worker）"
            st.update(state)
            return {"id": run_id, "status": state.status, "cancelled": True}
        flag.set()
        return {"id": run_id, "status": "running", "cancelled": True,
                "note": "正在当前这步结束后停止"}

    @app.get("/runs")
    def list_runs() -> dict:
        return {"runs": [r.to_dict() for r in st.all()]}

    @app.delete("/runs/{run_id}")
    def delete_run(run_id: str) -> dict:
        """删掉一个 run（连同事件和检查点）。

        为什么需要：测试往真实 Redis 里写过脏数据之后，界面上只能看着它 —— 没有删的入口。
        """
        state = st.get(run_id)
        if state is None:
            raise HTTPException(404, "没有这个 run")
        if state.status in ("running", "queued") and run_id in active_runs:
            raise HTTPException(409, "这个 run 还在跑，先取消再删")
        st.delete(run_id)
        changed.pop(run_id, None)
        cancel_flags.pop(run_id, None)
        return {"id": run_id, "deleted": True}

    @app.get("/runs/{run_id}")
    def get_run(run_id: str, events: bool = False) -> dict:
        state = st.get(run_id)
        if state is None:
            raise HTTPException(404, "没有这个 run")
        return state.to_dict(events=st.read_events(run_id) if events else None)

    @app.get("/runs/{run_id}/tasks")
    def get_run_tasks(run_id: str) -> dict:
        """返回这次 run 的工作区里的任务图。

        为什么不从事件里重建：create_task 的 ID 是在工具**结果**里才出现的，
        按事件流反推容易错（模型可能没把 ID 抄对）。直接读任务目录最准。
        """
        state = st.get(run_id)
        if state is None:
            raise HTTPException(404, "没有这个 run")
        if not state.tasks:
            return {"enabled": False, "tasks": [], "progress": {"done": 0, "total": 0}}
        store = TaskStore(Path(state.workspace) / ".minicode" / "tasks")
        tasks = store.list()
        return {
            "enabled": True,
            "tasks": [t.to_public() for t in tasks],
            "progress": {
                "done": sum(1 for t in tasks if t.status == "completed"),
                "total": len(tasks),
            },
        }

    @app.post("/runs/{run_id}/followup", status_code=202)
    async def followup_run(run_id: str, req: FollowupRequest) -> dict:
        """接着上一轮继续聊。

        为什么用"起一个新 run"而不是在原 run 上改：事件流、历史、检查点都是按 run 存的，
        起新 run 能直接复用整套机制，只需把上一轮结束时的 messages 作为起点，
        再把新的一句话追加进去。父子关系记在 parent_id 上，历史里能看出是一条链。
        """
        parent = st.get(run_id)
        if parent is None:
            raise HTTPException(404, "没有这个 run")
        if parent.status in ("running", "queued") and parent.id in active_runs:
            # 父 run 仍在执行时续聊，会从半截检查点分叉出第二个 run，
            # 两个 run 共用同一个工作区和任务目录，互相覆盖。
            raise HTTPException(409, "上一轮还在跑，等它结束再接着聊")

        text = (req.text or "").strip()
        if not text:
            raise HTTPException(400, "续聊的内容不能为空")

        checkpoint = st.get_checkpoint(run_id)
        if not checkpoint or not checkpoint.get("messages"):
            raise HTTPException(409, "这个 run 还没有可续的对话记录（可能没跑完就被丢弃了）")

        messages = list(checkpoint["messages"]) + [{"role": "user", "content": text}]
        run_settings = _resolve_settings(parent.model, req.max_steps)
        if provider_factory is None and not run_settings.api_key:
            raise HTTPException(400, "没有 API key，先把 .env 配好")

        state = RunState(
            id=uuid.uuid4().hex[:12],
            task=text,
            model=run_settings.model,
            workspace=parent.workspace,
            parent_id=parent.id,
            include_files=parent.include_files,
            plan=parent.plan,
            skills=parent.skills,
            compact=parent.compact,
            tasks=parent.tasks,
            memory=parent.memory,
            mcp=parent.mcp,
        )
        st.create(state)
        # step=0 表示"从第 1 步开始"，usage 清零：新的一轮单独计费
        _start(state, run_settings, resume={"messages": messages, "step": 0, "usage": {}})
        return {"id": state.id, "parent_id": parent.id, "status": state.status}

    @app.get("/runs/{run_id}/events")
    async def stream_events(run_id: str, request: Request) -> StreamingResponse:
        state = st.get(run_id)
        if state is None:
            raise HTTPException(404, "没有这个 run")
        cs = changed.setdefault(run_id, asyncio.Event())

        # 断点续传：浏览器 EventSource 重连时会带 Last-Event-ID。
        # 之前每次连接都从 0 重放，前端把整段事件再渲染一遍（重复计数、重复工具行）。
        start_at = 0
        raw_last_id = request.headers.get("last-event-id", "").strip()
        if raw_last_id.isdigit():
            start_at = int(raw_last_id) + 1

        async def gen():
            sent = start_at
            yield "retry: 3000\n\n"
            while True:
                # 先 clear 再读：worker 在“读事件”期间 set() 的通知会被下一轮 wait 消费，
                # 不会像“读完再 clear”那样被抹掉，尾部事件最多延迟 15 秒。
                cs.clear()
                for ev in st.read_events(run_id, sent):
                    sent += 1
                    # id 使用事件序号；客户端重连时据此确定续传起点。
                    yield (f"id: {sent - 1}\nevent: {ev['kind']}\n"
                           f"data: {json.dumps(ev, ensure_ascii=False)}\n\n")
                fresh = st.get(run_id)
                if fresh is not None and fresh.status in ("done", "error", "cancelled", "exhausted"):
                    # 先发送已产生的事件：读取事件与查询状态之间可能新增事件，
                    # 若直接发送 end，客户端会缺少最后几条事件（已验证可能丢失 finish/model_call）。
                    while True:
                        rest = st.read_events(run_id, sent)
                        if not rest:
                            break
                        for ev in rest:
                            sent += 1
                            yield (f"id: {sent - 1}\nevent: {ev['kind']}\n"
                                   f"data: {json.dumps(ev, ensure_ascii=False)}\n\n")
                    # 收尾事件须与其他事件结构一致（kind / ts / data）。
                    # 之前这里直接发 {status, result, error}，少了外面那层，
                    # 前端就把它当普通事件渲染，结果画出一个空 JSON，
                    # 而且 showAnswer(ev.data) 拿到 undefined 直接抛 TypeError。
                    tail = {
                        "kind": "end",
                        "ts": time.time(),
                        "data": {
                            "status": fresh.status,
                            "result": fresh.result,
                            "error": fresh.error,
                        },
                    }
                    yield f"event: end\ndata: {json.dumps(tail, ensure_ascii=False)}\n\n"
                    return
                try:
                    await asyncio.wait_for(cs.wait(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api")
    def index() -> dict:
        return {
            "name": "minicode",
            "version": __version__,
            "store": type(st).__name__,
            "用法": [
                "POST /runs   {\"task\": \"...\"}",
                "POST /runs/{id}/resume",
                "GET  /runs/{id}",
                "GET  /runs/{id}/events   (SSE)",
                "GET  /runs",
                "GET  /healthz",
            ],
        }

    # 界面挂在根路径。必须在所有接口注册完之后再挂，
    # 否则这个覆盖 "/" 的 mount 会把后面的接口挡掉。
    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    else:
        @app.get("/")
        def index_no_ui() -> dict:
            return {
                "name": "minicode",
                "version": __version__,
                "注意": "没找到前端产物（web/），先跑 `npm --prefix web run build`",
                "接口": "/api",
            }

    return app