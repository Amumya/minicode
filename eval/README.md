# eval

评测相关的都放这里：6 个沙箱任务、判定器、对照实验脚本，以及原始运行记录。

一开始定的几条规矩，现在还在用：

1. 每个数字都要有对应的原始记录，放在 `runs/` 里。找不到记录的，那个数字作废。
2. 每条结果写清楚条件：模型、样本量、改了哪个变量、什么时候跑的。不带条件的数字不算数。
3. 判定器本身也要测。判定器有 bug 时指标会乱飘，而且不会报错。
4. 样本量少就写少。两个样本的差别只能说"这个变量影响挺大，值得多跑几个"。
5. 撞到天花板的指标要标出来。满分基线证明不了任何机制有用。

## 目录

```
eval/
├── sandbox/                 6 个任务，每个是一份起始代码 + 可见测试 + 隐藏测试
├── sandbox_run.py           跑沙箱任务（条件：bash / files）
├── verify_tasks.py          不跑模型，检查任务自洽
├── check_permission.py      权限判定对照实验
├── permission_cases.json    权限用例（16 条危险 + 12 条正常）
├── ab_python_hint.py        "说不说用哪个 Python"的对照
├── ab_compact.py            上下文压缩对照
├── ab_compact_tuning.py     压缩阈值调优
├── compare_conditions.py    汇总沙箱两个条件，按任务拆开看
├── compare_prompt.py        汇总提示词对照（Windows/PowerShell 说明）
├── failure_kinds.py         把工具失败按原因分类
├── show_kind.py             看某一类失败的具体命令
├── inspect_run.py           看某一组运行里 agent 到底做了什么
├── measure_prompts.py       量提示词和技能目录的字符/token 量级
├── runs/                    原始运行记录（故意入库，方便核对）
└── tmp/                     临时工作区，跑完就删（已忽略）
```

## 任务集

每个任务目录长这样：

```
t1_counter/
├── task.txt   给模型的任务描述（系统提示词之外唯一输入）
├── src/       起始代码
├── tests/     可见测试，模型能看能跑
└── hidden/    隐藏测试，评测时才拷进工作区
```

`t6_spec` 多一个 `SPEC.md`：需求写在里面，可见测试覆盖不到，只能读文档改代码。

六个任务分别考什么：

| 任务 | 考什么 | 隐藏测试文件 |
|---|---|---|
| t1_counter | 修一个数词函数的实现（起始代码可见测试就是红的） | 2 |
| t2_ranges | 补区间解析：`"1-3,5"` 要展开成 `[1,2,3,5]` | 3 |
| t3_median | 实现 median（奇数取中间、偶数取平均、空列表 None），别改坏 mean | 1 |
| t4_config | 改带中文和引号的默认配置字典（timeout 30→60、retries 2→3） | 2 |
| t5_multi | 跨文件修 None 处理（report.build 和 summary.total） | 1 |
| t6_spec | 按 SPEC.md 改语义（可见测试没覆盖到，容易被"测试过就是对了"骗到） | 2 |

判定方式是：跑之前把 `hidden/*.py` 拷进工作区 `tests/`，改名为 `test_<原名>.py`，
然后在工作区根目录跑 `pytest tests`，全过才算通过。为什么要改名：pytest 默认只收集
`test_*.py`，直接把 `hidden_*.py` 放进 tests 目录会被跳过——那样"隐藏测试判定"其实
只是在重跑可见测试（这个坑真踩过，见 `docs/取舍与证据.md` D24）。

工作区建在仓库外面：`ROOT.parent/_eval_tmp/<task>_<随机后缀>`，跑完删掉。位置必须出仓库，
因为之前放在 `eval/tmp` 下时，agent 用 `..` 爬回项目把起始代码改成了答案。另外每次跑
前后都会比对源目录指纹（`source_fingerprint`），被改过就把 `source_contaminated` 标出来。

防作弊是两条线，都只能"发现"、不能绝对"阻止"，README 里不装作全覆盖：

- 写测试文件会被权限层拦下（`policy.py` 里 `PROTECTED_NAME_PATTERNS` 覆盖 `test_*.py`、
  `hidden_*.py`、`conftest.py` 等）
- 跑完对 `tests/` 做哈希比对：测试文件被改/删、出现 `sys.exit(0)` 或裸 `except: pass`
  都会记进 `cheat_flags`。这几条只是线索，真正兜底的是隐藏测试本身。

## 怎么跑

不需要模型 key 的：

```powershell
# 任务自洽：起始代码下隐藏测试必须至少挂一条，否则任务白送分
.venv\Scripts\python.exe eval\verify_tasks.py

# 权限判定对照，19 毫秒
.venv\Scripts\python.exe eval\check_permission.py
.venv\Scripts\python.exe eval\check_permission.py --json    # 只输出 JSON
.venv\Scripts\python.exe eval\check_permission.py --save    # 写进 eval/runs/
```

需要 `.env` 里的 `DEEPSEEK_API_KEY`：

```powershell
# 沙箱消融：两组各 24 次，README 里引用的数字就是这么跑的
.venv\Scripts\python.exe eval\sandbox_run.py --condition bash  --repeat 4 --max-steps 12
.venv\Scripts\python.exe eval\sandbox_run.py --condition files --repeat 4 --max-steps 12
.venv\Scripts\python.exe eval\compare_conditions.py

# 提示词和压缩
.venv\Scripts\python.exe eval\ab_python_hint.py --repeat 3
.venv\Scripts\python.exe eval\ab_compact.py --repeat 2
.venv\Scripts\python.exe eval\ab_compact_tuning.py --repeat 3
.venv\Scripts\python.exe eval\compare_prompt.py
.venv\Scripts\python.exe eval\measure_prompts.py
```

调试和看记录：

```powershell
# 只跑一个任务
.venv\Scripts\python.exe eval\sandbox_run.py --condition bash --task t4_config --repeat 1

# 只建工作区、不跑模型，看看会喂给 agent 什么
.venv\Scripts\python.exe eval\sandbox_run.py --condition bash --repeat 1 --dry-run

# 看某一组运行干了什么 / 某一类失败长什么样 / 失败按原因统计
.venv\Scripts\python.exe eval\inspect_run.py bash
.venv\Scripts\python.exe eval\show_kind.py shell_mismatch
.venv\Scripts\python.exe eval\failure_kinds.py
```

注意脚本里 `--repeat` 的默认值是 2，README 引用的数字是 `--repeat 4` 跑出来的，
复跑时记得带参数，否则样本量对不上。

## 记录

`runs/` 里现在有 6 份，都能和 README 里的数字对上：

| 文件 | 内容 |
|---|---|
| perm_1794547513.json | 权限对照：16 条危险 + 12 条正常命令，黑名单 vs 分词判定 |
| sandbox_bash_hint_1794671930.json | 沙箱 bash 条件，24 行（6 任务 × 4 次） |
| sandbox_files_hint_1794672565.json | 沙箱 files 条件，24 行 |
| ab_python_hint_1794564583.json | 解释器提示对照，3 次/组 |
| ab_compact_1794564646.json | 压缩对照，2 次/组 |
| ab_compact_tuning_1794566733.json | 压缩阈值调优，3 次/组 |

沙箱记录的每行主要字段：`passed`（隐藏测试判定）、`steps`、`tool_calls`、
`failed_tools`、`failure_kinds`、`denied`、`usage`（`input_tokens` / `output_tokens`）、
`elapsed_s`、`cheat_flags`、`source_contaminated`、`commands`（完整工具调用记录，
可以复盘 agent 每一步干了什么）。

口径：说"token"时默认指输出 token，输入 token 在 `usage.input_tokens` 里。
记录里的本机路径和个别内部文件名做了占位替换，不影响任何判定结果。

## 加新任务

在 `eval/sandbox/` 下新建目录，放 `task.txt`、`src/`、`tests/`、`hidden/`（至少一个
`hidden_*.py`，里面写 `test_` 开头的函数）。然后：

1. 跑 `verify_tasks.py`，确认起始代码下隐藏测试是挂的（否则这个任务没有要修的东西）；
2. 跑 `sandbox_run.py --task <新任务> --repeat 1` 试一次，看工作区和判定是否符合预期；
3. 正式跑 `--repeat 4`，记录会落到 `runs/`。
