# minicode

自己写的一个 coding agent，重点放在**权限控制**和**长期记忆**

- `--memory` 或界面「记忆」开关
- 存 `<workspace>/.minicode/memory.jsonl`：一行一条 JSON（id/正文/标签/时间），
  同一句话只存一份，单工作区最多 200 条
- 工具 `remember` / `recall` / `forget`；`GET /memory` 查看，右栏「记忆」分区展示
- 注入时只挑**和当前任务相关**的几条（≤8 条、≤1200 字符），不是全塞

**评测**上。

能跑完一个任务、能改代码、能跑测试；但真正的重心在治理层：
每一步都有结构化事件、命令执行前要过权限判定、结果用隐藏测试判定而不是靠模型自述。

## 为什么做这个

能跑起来的 coding agent 已经很多了。opencode 二十多万 star，codex 十二万，
gemini-cli 十万，OpenHands 八万多。再多一个"能读文件、能改代码、能跑测试"的
其实没什么意思。

我搜了一圈，发现有两块基本是空的：

- 权限判定。能找到的开源实现里，star 最高的只有二十几个。
- 轨迹级评测。agentevals 七百多 star，而且去年 7 月之后就没再更新。

所以这个项目不打算拼功能面，就把这两块做扎实，每个结论尽量带上数字。

## 现在有什么

**核心**

- 主循环：调模型 → 执行工具 → 带退出码回填 → 收尾，支持断点续跑
- bash 工具（Windows 上底层是 PowerShell，超时会杀掉整棵进程树）
- 文件读写工具（`read_file` / `write_file` / `edit_file`），默认不开，用来做对照
- 多轮对话：一轮结束后可以接着聊，上下文接续上一轮
- 运行中可随时取消（当前这步结束后停，界面有「停止」按钮）
- 子代理：`task` 工具，子代理有自己的上下文，父代理只拿结论

**治理**

- **钩子层**：权限、审计、参数改写都挂在钩子上，主循环不用为治理需求改动
- 权限判定：先分词再按参数判（子串黑名单会被一层 shell 包装绕过，见下面的数字）
- **技能**：`skills/*/SKILL.md`，目录进系统提示词、正文按需 `load_skill`
- **任务清单**：`todo_write`，支持 `blockedBy` 依赖和环检测
- **任务图**（`--tasks`）：带依赖、认领、解锁报告的任务板，跨 run 持久化
- **上下文压缩**：大工具输出落盘、旧结果截断、剪掉最早整对（确定性，不用 LLM 摘要）

**界面 / 服务**

- 命令行：`minicode run "任务"` / `minicode serve` / `minicode dev` / `minicode app`
- HTTP 服务：提交任务返回 id，事件用 SSE 推，`curl` 就能看；接口支持删除 run / 删除会话；
  可选**访问令牌**（`--token` 或 `MINICODE_TOKEN`），令牌走 `Authorization: Bearer` 或 SSE 的 `?token=`
- 三栏界面：左栏工作区 + 历史会话，中间流式对话（Markdown 渲染），右栏详情面板（工具调用 / 任务看板 / 原始事件，可收起）
- 会话分组：`GET /conversations` 按父子链把 run 串成会话，左栏按日期分组
- 深链：`http://127.0.0.1:8765/#/c/<会话 id>` 可以直接打开某个会话
- 一键开发环境 `minicode dev`（自动起 Redis、可选开窗口、端口冲突有提示）
- 桌面入口 `minicode app`（独立窗口；渲染不出来会自动降级到普通窗口/系统浏览器）

**长期记忆**

- `--memory` 或界面「记忆」开关
- 存 `<workspace>/.minicode/memory.jsonl`：一行一条 JSON（id/正文/标签/时间），
  同一句话只存一份，单工作区最多 200 条
- 工具 `remember` / `recall` / `forget`；`GET /memory` 查看，右栏「记忆」分区展示
- 注入时只挑**和当前任务相关**的几条（≤8 条、≤1200 字符），不是全塞

**评测**

- 沙箱任务集：6 个任务，**隐藏测试**判定，防作弊两条线（测试文件只读 + 跑完比对哈希）
- 任务自洽检查：`eval/verify_tasks.py`，几秒钟验证"起始代码下隐藏测试必须是挂的"
- CI：Python 3.11 / 3.12 + 前端测试

现状：Python 测试 384 条、前端 67 条、界面链路检查 64 项。

## 跑出来的数字

| 项 | 结果 | 条件 |
|---|---|---|
| 危险命令拦截率 | 56.2% → **100%** | 16 条危险命令，子串黑名单 vs 分词+参数判定 |
| 正常命令误报率 | 33.3% → **0%** | 12 条正常命令，同上 |
| 沙箱任务通过率 | **96% → 100%** | 6 个任务 × 4 次，只有 bash vs 加文件工具 |
| 沙箱花费 | 7.1 步 / 1912 token → 7.2 步 / **2336 token（+22%）** | 同上。通过率高 4 个点，代价是多 22% token |
| 工具调用失败率 | 10% → **7%** | 同上。加了文件工具之后失败反而更少 |
| 改多行结构 | 8.0 步 / 1431 token → **7.5 步 / 893 token** | `t4_config`，改带中文和引号的字典，文件工具省 38% |
| 改一行代码 | 5.8 步 / 546 token → 5.8 步 / 648 token | `t1_counter`，加了工具反而更贵 |
| shell 写法不匹配 | **6 次 → 0 次** | 系统提示词里说明"底层是 PowerShell"，4 任务 × 2 次 |
| 解释器提示的效果 | 工具失败 **6 → 3**，耗时 29.0 → 26.0 秒 | 同一任务跑 3 次/组，只改"说不说用哪个 Python" |
| 技能目录占比 | 目录只占全部正文的 **21.8%** | 2 个技能，目录 132 字符 vs 正文 605 字符，每轮省约 157 token |
| 上下文压缩 | 输入 token **7597 → 4185（−45%）**，完成 3/3 | 读两个大文档，n=3/组。默认阈值 4000 字符；阈值调到 12000 反而一次都不触发 |
| 子代理隔离 | 父代理 2 步 / 526 token 拿到结论 | 子代理实际跑了 3 步 / 969 token，中间过程没进父上下文 |
| 断点续跑 | 中断在第 1 步 → 续跑后完成 | `max_steps=1` 故意中断，杀掉服务进程重启后 `/resume` |
| 进程重启不丢 | 结果和事件都还在 | Redis + AOF，杀掉 uvicorn 再起 |
| 改测试文件 | 权限层拦下 3 次，哈希比对抓到 1 次 | 防作弊两条线都真的触发过 |

样本量得说清楚：权限用例是我自己写的，不是从真实操作里抽的；沙箱每格 **4 次**重复
（每条件 24 次运行）；提示词实验每组 3 次。这个量级能说明方向和量级，
说不出"提升几个百分点"。

有几件事值得单独讲：

- **隐藏测试曾经根本没被执行**（文件名不匹配 pytest 的收集规则），所以更早那批
  "全过"的沙箱数字是错的。修好之后才重新量出 96%/100%（6 任务 × 4 次，干净重测）。详见 D24。
- **"工具失败率"这个口径错过一次**：`pytest` 测试红本来就返回 1，那是 agent 要的信号。
  拆开之后真正的环境问题只占 9%。
- **加文件工具不是单向改进**：通过率 +4 个点，token +22%；但失败率反而下降了（10%→7%）。
- **基准任务自己被污染过一次**：agent 从工作区用 `..` 爬回项目，把起始代码改成了答案，
  导致后续一轮数据作废。现在工作区移出仓库，并且每次跑前后比对源目录指纹。详见 D26。
- **权限层被一层包装绕过**：`bash -c "rm -rf /"`、`powershell -Command "Remove-Item ..."`、
  `rm -rf $HOME` 这些在加固前全部放行。现在改成"嵌套命令递归判定"，并有 67 条攻击/误杀用例。详见 D34。

原始记录都在 `eval/runs/` 里，能自己复跑：

```powershell
# 权限对照，不需要 key，19 毫秒
.venv\Scripts\python.exe eval\check_permission.py

# 任务自洽检查（不跑模型）：起始代码下隐藏测试必须是挂的
.venv\Scripts\python.exe eval\verify_tasks.py

# 沙箱消融，要 key，两组各 24 次
.venv\Scripts\python.exe eval\sandbox_run.py --condition bash  --repeat 4 --max-steps 12
.venv\Scripts\python.exe eval\sandbox_run.py --condition files --repeat 4 --max-steps 12
.venv\Scripts\python.exe eval\compare_conditions.py

# 提示词与压缩的对照实验
.venv\Scripts\python.exe eval\ab_python_hint.py --repeat 3
.venv\Scripts\python.exe eval\ab_compact.py --repeat 2
.venv\Scripts\python.exe eval\measure_prompts.py
```

## MCP（外部工具）

接的是官方协议：**2026-07-28 现代版**（无握手，每个请求带 `_meta`）+ **旧版回退**
（2025-11-25 / 2025-06-18 / 2024-11-05 的 initialize 握手），只做 stdio 传输、只接 tools 能力。

配置放 `<workspace>/.minicode/mcp.json`（示例见 [docs/mcp.example.json](docs/mcp.example.json)）：

```json
{
  "servers": [
    {
      "name": "filesystem",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "D:\\work\\demo"],
      "enabled": true,
      "allow": ["read_file", "list_directory"],
      "timeout": 30
    }
  ]
}
```

三层限制，缺一不可：

1. **`enabled: true`** 才会去连（默认 false）；
2. **`allow`** 是枚举白名单：没列出的工具**不会注册**，模型看不到；
3. 运行时再查一次白名单（防止绕过注册直接调用）。

工具名是 `mcp__<server>__<tool>`，界面右栏会把它显示成 `server/tool` 并加 **MCP** 徽标，
每次调用的耗时/协议版本记在事件里。先体检配置（不花模型钱）：

```powershell
minicode mcp                 # 连一遍 server，列出模式/版本/工具/白名单过滤/schema 大小
minicode run --mcp "用 MCP 的 add 工具算 7+35"   # 跑任务时挂上 MCP 工具
```

界面里也可以：输入区勾「MCP」（默认不勾）。

> ⚠️ 配置里的 `command` 等于**允许它在你机器上执行任意代码**，这是 MCP 的固有风险。
> 所以默认关闭 + 白名单；`minicode mcp check` 会先把完整命令行打印出来让你确认。

**不做**：HTTP/SSE 传输、resources/prompts/sampling、MRTP（server 要求补输入时直接报错）、
多 server 并行、自动重启。原则是"先能用、能审计、能失败"，不假装支持全量。

## 还差什么

- [x] 主循环和工具调用
- [x] 权限判定（已经是钩子之一）
- [x] 命令行入口（run / serve / dev / app）
- [x] 文件读写工具
- [x] 沙箱任务集和判定器（6 个任务，隐藏测试真的生效）
- [x] FastAPI 服务 + SSE 事件流（支持断点续传和尾部补齐）
- [x] Redis 存会话 + 断点续跑（僵尸 running 可恢复）
- [x] 三栏界面：左栏会话 / 中间流式对话 / 右栏详情面板
- [x] 钩子层（审计 / 拦截 / 参数改写）
- [x] 技能按需加载
- [x] 任务清单（含依赖与环检测）
- [x] 上下文压缩（阈值已实测：默认 4000 最好）
- [x] 子代理（上下文隔离）
- [x] 完整任务图（--tasks）+ 任务看板
- [x] 桌面入口（三级降级：app → 普通窗口 → 系统浏览器）
- [x] 多轮对话（followup，上下文接续）
- [x] 权限层加固（嵌套 shell / PowerShell / 包装器 / 分隔符）
- [x] 主循环加固（resume 追加预算、截断不算成功、异常边界）
- [x] 服务层加固（SSE 断点续传、双 store 语义一致、请求上限）
- [x] 结果区 Markdown 渲染（自己写的解析器，无第三方依赖）
- [x] 流式输出（按 80ms / 120 字符合并增量事件）
- [x] 取消正在跑的 run（当前这步结束后立刻停，界面有「停止」按钮）
- [x] bash 工具流式读取输出（攒够上限就杀进程，超大输出不会打爆内存）
- [x] HTTP 接口访问令牌（`--token` / `MINICODE_TOKEN`，默认关闭）
- [x] 记忆（跨 run 的长期笔记，`--memory` / 界面开关）
- [x] MCP 接入（stdio，tools 能力；本地 server 默认关闭 + 白名单）

## 跑起来

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest
```

装不上 pytest 的话（这台机器上 PyPI 时通时不通），可以直接跑自检脚本，不用 pytest：

```powershell
$env:PYTHONPATH = "src"
.venv\Scripts\python.exe tests\run_tests.py
```

前端构建产物（`src/minicode/service/web/`）也一起提交了，所以 clone 下来不用装 npm
就能直接看界面。要改前端源码：

```powershell
npm ci --prefix web
npm --prefix web run build     # 产物直接吐到 src/minicode/service/web/
npm --prefix web test          # 纯逻辑 + 接线检查，不需要浏览器
```

## 用一下

最省事的方式（自动起 Redis + 服务 + 窗口）：

```powershell
minicode dev --build --open
```

或者双击 `tools\dev.cmd`。

只想跑一个任务，不起服务：

```powershell
minicode run "数一下 src/minicode 下有几个 py 文件"
```

起服务，然后另开一个窗口看事件流：

```powershell
# 窗口 1
minicode serve --port 8765

# 窗口 2
curl.exe -s -X POST http://127.0.0.1:8765/runs -H "Content-Type: application/json" `
  -d '{\"task\":\"数一下 src 下有几个 py 文件\"}'

curl.exe -sN http://127.0.0.1:8765/runs/<上一步返回的 id>/events
```

事件流长这样（`start` → `model_call` → `tool_call` → `assistant_delta` → `finish` → `end`），
每条 `data` 都是 JSON；`assistant_delta` 是流式输出的文字增量。

浏览器打开 <http://127.0.0.1:8765/> 就是界面：三栏布局，文件工具 / 计划 / 技能 / 压缩 / 任务图默认全开，步数默认 20（可改，上限 50）；
左栏选工作区和历史会话，中间写任务/看对话，右栏看工具调用、任务看板和原始事件。
会话可以直接用链接分享：`/#/c/<会话 id>`。

默认**没有鉴权**，只监听 `127.0.0.1`。想加一层令牌就启动时带上：

```powershell
minicode serve --port 8765 --token 换成你的令牌
```

开了之后：页面和静态资源仍可直接打开，浏览器界面在左栏「令牌」里填同一个值
（会存 localStorage）；直接用 `curl` 时加 `-H "Authorization: Bearer <令牌>"`，
SSE 那条流加 `?token=<令牌>`（EventSource 加不了请求头）。`/healthz` 始终免鉴权。

store 里如果混进了测试数据（历史里出现 `x`、`回滚` 这类，点开报"模型不存在"），
可以用脚本清掉（先 `--dry-run` 看要删什么）：

```powershell
$env:MINICODE_REDIS_URL = "redis://127.0.0.1:6380/0"
.venv\Scripts\python.exe tools\clean_store.py --dry-run
.venv\Scripts\python.exe tools\clean_store.py
```

也可以单条删：`DELETE /runs/{id}`。

默认用内存存 run，重启就丢。想持久化就接 Redis（本机的便携 Redis 怎么起见
[docs/环境实测.md](docs/环境实测.md)）：

```powershell
$env:MINICODE_REDIS_URL = "redis://127.0.0.1:6380/0"
minicode serve --port 8765
```

接了 Redis 之后，进程被杀掉再起来，run 的状态和历史事件都还在；
跑了一半的任务可以接着跑：

```powershell
curl.exe -s -X POST http://127.0.0.1:8765/runs/<id>/resume -H "Content-Type: application/json" -d "{\"max_steps\":8}"
```

## 环境

这台机器的网络比较怪，实测记录在 [docs/环境实测.md](docs/环境实测.md)。

根因是 **IPv6 不通，但 DNS 优先返回 IPv6**，于是 npm、清华源、git 都要卡七八秒才回退到 IPv4，
看起来就像"完全不通"。现在的处理是：npm 换国内镜像并强制 IPv4 优先，git 走本地代理。
具体配置和验证方式都写在那份记录里。

## 参考

- [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)，s01 到 s17 我照着学了一遍
- [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)，最小闭环的实现

代码是自己写的，没有把这两个项目的源码复制进来。
