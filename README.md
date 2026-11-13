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

- 包结构，src layout，能 `pip install -e` 装上
- CI，Python 3.11 和 3.12 都跑
- 几个自检，不需要 API key

## 还差什么

- [ ] 主循环和工具调用
- [ ] 权限判定，现在还是空的
- [ ] 命令行入口
- [ ] 评测任务集和判定器
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
