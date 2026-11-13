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
- 一个 bash 工具，命令失败会把退出码带回去
- 权限判定，先把命令分词再按参数判
- 事件记录，跑完落成 JSON
- 命令行入口，`minicode run "任务"`
- CI，Python 3.11 和 3.12 都跑

## 跑出来的数字

| 项 | 结果 | 条件 |
|---|---|---|
| 危险命令拦截率 | 56.2% → **100%** | 16 条危险命令，子串黑名单 vs 分词+参数判定 |
| 正常命令误报率 | 33.3% → **0%** | 12 条正常命令，同上 |
| 两个真实任务 | 都答对 | deepseek-flash，问某个目录下 py 文件个数 |
| 单个任务的花费 | 569 入 / 134 出 token，3.5 秒 | 同上，3 步 |
| 工具调用失败 | 3 次调用里失败 1 次 | 同上，模型先写了 Unix 管道（`wc` 在 PowerShell 里没有），看到 exit=1 后自己换成了 PowerShell 写法 |

样本量得说清楚：16 + 12 条用例是我自己写的，不是从真实操作里抽的；真实任务只有 2 个。
所以上面这些只能说明"两种判定方式差距很大""退出码确实改变了模型的行为"，
不能说成功率是多少。

原始记录在 `eval/runs/` 里，数都能自己复跑：

```powershell
# 权限对照，不需要 key，19 毫秒
.venv\Scripts\python.exe eval\check_permission.py

# 真实跑一个任务
.venv\Scripts\minicode.exe run "数一下 src/minicode 下有多少个 py 文件" --save
```

## 还差什么

- [x] 主循环和工具调用
- [x] 权限判定
- [x] 命令行入口
- [ ] 文件读写、搜索这些工具
- [ ] 评测任务集和判定器
- [ ] 上下文压缩
- [ ] FastAPI 服务，能流式看 agent 一步步跑
- [ ] Redis 存会话

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

## 环境

这台机器的网络比较怪，实测记录在 [docs/环境实测.md](docs/环境实测.md)，
主要是两条：

- PyPI 只有阿里云的镜像能通，清华和官方源都超时。
- `git push` 到 github 会被 reset，所以推代码是用自己的脚本走 REST API。

## 参考

- [learn-claude-code](https://github.com/shareAI-lab/learn-claude-code)，s01 到 s17 我照着学了一遍
- [mini-swe-agent](https://github.com/SWE-agent/mini-swe-agent)，最小闭环的实现

代码是自己写的，没有把这两个项目的源码复制进来。
