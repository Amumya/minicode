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
- 命令行入口，`minicode run "任务"`
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
| 工具调用失败率 | 29% vs 27% | 62 次 vs 78 次调用。主要是模型写 Unix 写法，PowerShell 里跑不通 |
| 改测试文件 | 权限层拦下 3 次，哈希比对抓到 1 次 | 防作弊两条线都真的触发过 |

样本量得说清楚：权限用例 16 + 12 条是我自己写的，不是从真实操作里抽的；
沙箱每格只有 2 次重复。所以上面这些能说明的是方向和量级，不是"提升了百分之多少"。

特别要说明一条：**4 个任务对现在的模型都太简单，两个条件全过**。所以"加文件工具提升表现"
这件事我测不出来 —— 能测出来的是花费差异，而且方向跟任务形状有关。

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
- [ ] 更难的任务（现在全过，测不出差异）
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
