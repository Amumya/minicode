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
- 权限判定，先把命令分词再按参数判
- 事件记录，跑完落成 JSON
- 命令行入口：`minicode run "任务"` / `minicode serve`
- HTTP 服务：提交任务返回 id，事件用 SSE 推，`curl` 就能看
- 沙箱评测：4 个任务，隐藏测试判定，防作弊有权限层和哈希比对两道
- CI，Python 3.11 和 3.12 都跑

## 跑出来的数字

| 项 | 结果 | 条件 |
|---|---|---|
| 危险命令拦截率 | 56.2% → **100%** | 16 条危险命令，子串黑名单 vs 分词+参数判定 |
| 正常命令误报率 | 33.3% → **0%** | 12 条正常命令，同上 |
| 沙箱任务通过率 | 8/8 vs 8/8 | 4 任务 × 2 次，只有 bash vs 加文件工具。**两边都满分，这个维度测不出差异** |
| 改多行配置的花费 | 11.0 步 / 1918 token → **7.5 步 / 972 token** | `t4_config`，要改带中文和引号的字典，文件工具明显省 |
| 改一行代码的花费 | 6.0 步 / 742 token → **10.0 步 / 1630 token** | `t1_counter`，加了文件工具反而更绕 |
| shell 写法不匹配的失败 | **6 次 → 0 次** | 系统提示词里说明"底层是 PowerShell"。4 任务 × 2 次，每组 8 次运行 |
| 工具调用非 0 退出 | 32.8% → 29.0% | 同上。但拆开看：其中大部分是测试没过的正常信号，真正的环境问题只有 6 次（9%） |
| 改测试文件 | 权限层拦下 3 次，哈希比对抓到 1 次 | 防作弊两条线都真的触发过 |
| 断点续跑 | 中断在第 1 步 → 续跑后第 3 步完成 | `max_steps=1` 故意中断，杀掉服务进程重启后 `POST /runs/{id}/resume` |
| 进程重启不丢 | 结果和 5 条事件都还在 | Redis + AOF，杀掉 uvicorn 再起 |

样本量得说清楚：权限用例 16 + 12 条是我自己写的，不是从真实操作里抽的；
沙箱每格只有 2 次重复。所以上面这些能说明的是方向和量级，不是"提升了百分之多少"。

特别要说明两条：

- **4 个任务对现在的模型都太简单，两个条件全过。** 所以"加文件工具提升表现"这件事我测不出来 —— 能测出来的是花费差异，而且方向跟任务形状有关。
- 一开始我把 `exit_code != 0` 全算成"工具失败"，得出 29% 的失败率。那个数是错的：`pytest` 测试不通过本来就返回 1，那是 agent 要的信息。分开算之后真正的环境问题只占 9%。

原始记录都在 `eval/runs/` 里，能自己复跑：

```powershell
# 权限对照，不需要 key，19 毫秒
.venv\Scripts\python.exe eval\check_permission.py

# 沙箱消融，要 key，两组各 8 次
.venv\Scripts\python.exe eval\sandbox_run.py --condition bash  --repeat 2 --max-steps 12
.venv\Scripts\python.exe eval\sandbox_run.py --condition files --repeat 2 --max-steps 12
.venv\Scripts\python.exe eval\compare_conditions.py
```

## 还差什么

- [x] 主循环和工具调用
- [x] 权限判定
- [x] 命令行入口
- [x] 文件读写工具
- [x] 沙箱任务集和判定器
- [x] FastAPI 服务 + SSE 事件流
- [x] Redis 存会话 + 断点续跑
- [ ] 更难的任务（现在全过，测不出差异）
- [ ] 上下文压缩
- [ ] 前端页面

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
