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
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from minicode import __version__
from minicode.compact import CompactConfig, Compactor
from minicode.config import Settings
from minicode.events import Event, Recorder
from minicode.loop import run as run_loop
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

# 前端构建产物放在包旁边（web/vite.config.ts 里配的输出目录），
# 这样不管是从源码跑还是装成包跑，路径都对得上。
WEB_DIR = Path(__file__).resolve().parent / "web"


class RunRequest(BaseModel):
    task: str = Field(min_length=1, max_length=20000, description="任务内容")
    model: str | None = None
    workspace: str | None = None
    max_steps: int | None = Field(default=None, ge=1, le=50)
    include_files: bool = False
    plan: bool = False
    skills: bool = False
    compact: bool = False
    tasks: bool = False


class FollowupRequest(BaseModel):
    """多轮对话：在某个已结束的 run 后面接着说一句。"""
    text: str = Field(min_length=1, max_length=8000)
    max_steps: int | None = Field(default=None, ge=1, le=50)


class ResumeRequest(BaseModel):
    max_steps: int | None = Field(default=None, ge=1, le=50)


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
) -> FastAPI:
    app = FastAPI(title="minicode", version=__version__)
    st = store or default_store()
    # --reload 模式下 uvicorn 用 import 字符串重建 app，拿不到参数，
    # 所以默认工作区允许从环境变量来。
    env_workspace = os.environ.get("MINICODE_WORKSPACE", "").strip()
    base_workspace = Path(default_workspace or env_workspace or Path.cwd()).resolve()

    # 每个 run 一个 asyncio.Event，用来叫醒 SSE 生成器。
    # 放 dict 里是为了让 store 保持纯数据、不掺和异步。
    changed: dict[str, asyncio.Event] = {}

    # 本进程里正在跑的 run。用途：
    #   1) 进程被杀之后，store 里那些 status=running 的 run 永远等不到 worker，
    #      有了这份名单才能区分"真的在跑"和"僵尸" —— 僵尸允许 resume。
    #   2) followup 不能挂到一个还在跑的 run 上（会从半截检查点分叉出并发 run）。
    active_runs: set[str] = set()

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

    def _tools_for(state: RunState):
        return build_registry(
            files=state.include_files,
            todo=TodoList() if state.plan else None,
            skills=_skills_for(state),
            tasks=_tasks_for(state),
        )

    def _worker(state: RunState, cs: asyncio.Event, loop_ref, run_settings: Settings,
                resume: dict | None) -> None:
        active_runs.add(state.id)
        state.status = "running"
        st.update(state)
        rec = Recorder(on_event=lambda ev: _emit(state, cs, ev, loop_ref))

        def on_step(payload: dict) -> None:
            try:
                st.save_checkpoint(state.id, payload)
            except Exception:  # noqa: BLE001 - 存检查点失败不能拖垮整个 run
                pass

        todo = TodoList() if state.plan else None
        skills_loader = _skills_for(state)
        compactor = Compactor(Path(state.workspace), CompactConfig()) if state.compact else None

        try:
            result = run_loop(
                state.task,
                provider=_factory(run_settings),
                tools=build_registry(
                    files=state.include_files, todo=todo,
                    skills=skills_loader, tasks=_tasks_for(state)),
                settings=run_settings,
                workspace=Path(state.workspace),
                recorder=rec,
                system=default_system(
                    skills=skills_loader,
                    python_hint=sys.executable,
                    plan_hint=state.plan,
                ),
                resume=resume,
                on_step=on_step,
                compactor=compactor,
            )
            state.result = result.to_dict()
            if todo is not None:
                state.result["todos"] = todo.snapshot()
            if result.stop_reason == "provider_error":
                # 模型调用本身挂了属于基础设施错误，和"跑完了但没成功"要分开：
                # 前者要标 error（界面会红），后者是 ok=False 的正常收尾。
                state.error = result.error or "模型调用失败"
                state.status = "error"
            else:
                state.status = "done"
        except Exception as exc:  # noqa: BLE001 - 后台线程的异常必须自己接住
            state.error = f"{type(exc).__name__}: {exc}"
            state.status = "error"
        finally:
            active_runs.discard(state.id)
            st.update(state)
            try:
                loop_ref.call_soon_threadsafe(cs.set)
            except RuntimeError:
                pass

    def _start(state: RunState, run_settings: Settings, resume: dict | None) -> None:
        loop_ref = asyncio.get_running_loop()
        cs = asyncio.Event()
        changed[state.id] = cs
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
            max_steps=max_steps or base.max_steps,
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
            if run.workspace and run.workspace not in seen:
                seen.append(run.workspace)

        items = []
        default = str(base_workspace)
        ordered = [default] + [p for p in seen if p != default]
        for path in ordered[: max(1, limit)]:
            p = Path(path)
            items.append({
                "path": path,
                "name": p.name or path,
                "exists": p.is_dir(),
                "is_default": path == default,
            })
        return {"workspaces": items, "default": default}

    @app.get("/conversations")
    def list_conversations(limit: int = 60) -> dict:
        """把 run 按 parent_id 串成"会话"，按最近活动排序。

        为什么不在前端算：这个「沿着 parent_id 往上找根」的逻辑如果在界面里写，
        数据一多每次渲染都要重复算；放后端一次算完，前端只负责画。
        """
        runs = st.all()
        by_id = {r.id: r for r in runs}

        def root_of(run: RunState) -> str:
            current = run
            seen = {current.id}
            while current.parent_id and current.parent_id in by_id:
                if current.parent_id in seen:
                    break
                current = by_id[current.parent_id]
                seen.add(current.id)
            return current.id

        groups: dict[str, list[RunState]] = {}
        for run in runs:
            groups.setdefault(root_of(run), []).append(run)

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
        return {"conversations": out[: max(1, limit)]}

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
        # 走到这里说明状态是 running 但本进程没有它在跑 —— 进程被杀/重启留下的僵尸。
        # 检查点在的话允许续跑，否则这个 run 永远救不回来。
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

    @app.get("/runs")
    def list_runs() -> dict:
        return {"runs": [r.to_dict() for r in st.all()]}

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
            # 父 run 还在跑就续聊，会从半截检查点分叉出第二个 run，
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
                for ev in st.read_events(run_id, sent):
                    sent += 1
                    # id 用事件序号，客户端重连时靠它告诉我们从哪继续
                    yield (f"id: {sent - 1}\nevent: {ev['kind']}\n"
                           f"data: {json.dumps(ev, ensure_ascii=False)}\n\n")
                fresh = st.get(run_id)
                if fresh is not None and fresh.status in ("done", "error"):
                    # 先把手上的事件补发干净：从"读事件"到"查状态"之间可能又追加了几条，
                    # 直接发 end 会让客户端少最后几条（实测能丢 finish/model_call）。
                    while True:
                        rest = st.read_events(run_id, sent)
                        if not rest:
                            break
                        for ev in rest:
                            sent += 1
                            yield (f"id: {sent - 1}\nevent: {ev['kind']}\n"
                                   f"data: {json.dumps(ev, ensure_ascii=False)}\n\n")
                    # 收尾事件也必须和别的事件同一个形状（kind / ts / data）。
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
                cs.clear()
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