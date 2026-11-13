# minicode

自己写的一个 coding agent，重点放在权限控制和评测上。

现在还在起步阶段，只有项目骨架，agent 本体还没写。

## 为什么做这个

能跑起来的 coding agent 已经很多了。opencode 二十多万 star，codex 十二万，
gemini-cli 十万，OpenHands 八万多。再多一个"能读文件、能改代码、能跑测试"的
其实没什么意思。

我搜了一圈，发现有两块基本是空的：

- 权限判定。能找到的开源实现里，star 最高的只有二十几个。
- 轨迹级评测。agentevals 七百多 star，而且去年 7 月之后就没再更新。

所以这个项目不打算拼功能面，就把这两块做扎实，每个结论尽量带上数字。

## 现在有什么

- 主循环，能调工具、能跑完一个任务
- bash 工具，命令失败会把退出码带回去
- 文件读写工具（`read_file` / `write_file` / `edit_file`），默认不开，用来做对照
- **钩子层**：权限、审计、参数改写都挂在钩子上，主循环不用为治理需求改动
- **技能**：`skills/*/SKILL.md`，目录进系统提示词、正文按需 `load_skill`
- **任务清单**：`todo_write`，同一时间只允许一条 in_progress
- **上下文压缩**：大工具输出落盘、旧结果截断、剪掉最早整对
- **子代理**：`task` 工具，子代理有自己的上下文，父代理只拿结论
- 事件记录，跑完落成 JSON
- 命令行入口：`minicode run "任务"` / `minicode serve`
- HTTP 服务：提交任务返回 id，事件用 SSE 推，`curl` 就能看
- 浏览器界面：提交任务、事件实时滚动、历史 run 可回放（TypeScript + Vite）
- 沙箱评测：6 个任务、隐藏测试判定、防作弊两条线
- CI：Python 3.11/3.12 测试 + 前端测试

## 跑出来的数字

| 项 | 结果 | 条件 |
|---|---|---|
| 危险命令拦截率 | 56.2% → **100%** | 16 条危险命令，子串黑名单 vs 分词+参数判定 |
| 正常命令误报率 | 33.3% → **0%** | 12 条正常命令，同上 |
| 沙箱任务通过率 | **83% → 92%** | 6 个任务 × 2 次，只有 bash vs 加文件工具 |
| 沙箱花费 | 7.5 步 / 1618 token → 7.8 步 / **2433 token** | 同上。通过率高 9 个点，代价是多 50% token |
| 改多行配置 | 11.0 步 / 2228 token → **6.0 步 / 848 token** | `t4_config`，带中文和引号的多行字典，文件工具明显省 |
| 改一行代码 | 5.5 步 / 518 token → 6.0 步 / 634 token | `t1_counter`，加了工具反而更绕 |
| shell 写法不匹配 | **6 次 → 0 次** | 系统提示词里说明"底层是 PowerShell"，4 任务 × 2 次 |
| 解释器提示的效果 | 工具失败 **6 → 3**，耗时 29.0 → 26.0 秒 | 同一任务跑 3 次/组，只改"说不说用哪个 Python" |
| 技能目录占比 | 目录只占全部正文的 **21.8%** | 2 个技能，目录 132 字符 vs 正文 605 字符，每轮省约 157 token |
| 上下文压缩 | 输入 token **7597 → 4185（−45%）**，完成 3/3 | 读两个大文档，n=3/组。默认阈值 4000 字符；阈值调到 12000 反而一次都不触发 |
| 子代理隔离 | 父代理 2 步 / 526 token 拿到结论 | 子代理实际跑了 3 步 / 969 token，中间过程没进父上下文 |
| 断点续跑 | 中断在第 1 步 → 续跑后第 3 步完成 | `max_steps=1` 故意中断，杀掉服务进程重启后 `/resume` |
| 进程重启不丢 | 结果和事件都还在 | Redis + AOF，杀掉 uvicorn 再起 |
| 改测试文件 | 权限层拦下 3 次，哈希比对抓到 1 次 | 防作弊两条线都真的触发过 |

样本量得说清楚：权限用例是我自己写的，不是从真实操作里抽的；沙箱每格 2 次重复；
提示词实验每组 3 次。这个量级能说明方向和量级，说不出"提升几个百分点"。

有三件事值得单独讲：

- **隐藏测试曾经根本没被执行**（文件名不匹配 pytest 的收集规则），所以更早那批
  "全过"的沙箱数字是错的。修好之后通过率才从 100% 变成 83%/92%。详见
  `docs/取舍与证据.md` 的 D24。
- **"工具失败率"这个口径错过一次**：`pytest` 测试红本来就返回 1，那是 agent 要的信号。
  拆开之后真正的环境问题只占 9%。
- **加文件工具不是单向改进**：通过率 +9 个点，token +50%，失败率也翻倍。
- **基准任务自己被污染过一次**：agent 从工作区用 `..` 爬回项目，把起始代码改成了答案，
  导致后续一轮数据作废。现在工作区移出仓库，并且每次跑前后比对源目录指纹。
  详见 D26。

原始记录都在 `eval/runs/` 里，能自己复跑：

```powershell
# 权限对照，不需要 key，19 毫秒
.venv\Scripts\python.exe eval\check_permission.py

# 任务自洽检查（不跑模型）：起始代码下隐藏测试必须是挂的
.venv\Scripts\python.exe eval\verify_tasks.py

# 沙箱消融，要 key，两组各 12 次
.venv\Scripts\python.exe eval\sandbox_run.py --condition bash  --repeat 2 --max-steps 12
.venv\Scripts\python.exe eval\sandbox_run.py --condition files --repeat 2 --max-steps 12
.venv\Scripts\python.exe eval\compare_conditions.py

# 提示词与压缩的对照实验
.venv\Scripts\python.exe eval\ab_python_hint.py --repeat 3
.venv\Scripts\python.exe eval\ab_compact.py --repeat 2
.venv\Scripts\python.exe eval\measure_prompts.py
```

## 还差什么

- [x] 主循环和工具调用
- [x] 权限判定（已经是钩子之一）
- [x] 命令行入口
- [x] 文件读写工具
- [x] 沙箱任务集和判定器（6 个任务，隐藏测试真的生效）
- [x] FastAPI 服务 + SSE 事件流
- [x] Redis 存会话 + 断点续跑
- [x] 前端页面（TypeScript + Vite）
- [x] 钩子层（审计 / 拦截 / 参数改写）
- [x] 技能按需加载
- [x] 任务清单
- [x] 上下文压缩
- [x] 子代理（上下文隔离）
- [x] 更难的任务（通过率已不是 100%）
- [ ] 压缩阈值调优（现在偶尔会因为落盘多花一步）
- [ ] 记忆（跨 run 的长期笔记）
- [ ] MCP 接入

## 跑起来

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest
```

装不上 pytest 的话（这台机器上 PyPI 时通时不通），可以直接跑自检脚本，
不用 pytest：

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

命令行跑单个任务：

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

事件流长这样（`start` → `model_call` → `tool_call` → `model_call` → `finish` → `end`），
每条 `data` 都是 JSON。

浏览器打开 <http://127.0.0.1:8765/> 就是界面：写任务、点提交、事件一条条滚出来。
历史里的 run 点一下可以回放（事件存在 store 里，所以能重放）。

服务默认只监听 `127.0.0.1`。**它没有鉴权，别往公网开。**

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
