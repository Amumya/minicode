"""权限层的加固回归测试。

这些用例**全部来自代码审查实测出来的绕过**（2026-11-15）：
修复前它们大多返回 allow。写在这里的目的不是"再测一遍正常命令"，
而是"以后改判定逻辑时，别把已经堵上的洞重新打开"。

分组：
    1. 嵌套 shell：`bash -c "rm -rf /"` 这类，payload 必须被当成命令重新判
    2. Windows 等价物：PowerShell / cmd 的删除与写入命令
    3. 包装器：sudo / env / timeout 的选项和取值不能被当成命令
    4. 危险目标：$HOME、~ 要展开后再判
    5. 分隔符：单个 & 也是分隔符
    6. git：按 flag 组合判，不按字面相邻对
    7. 工作区边界：shell 写入越界也要拦（和文件工具同一条线）
    8. 误杀防护：正常命令不能因为加固而变成拒绝
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minicode.policy import judge

WS = Path(__file__).resolve().parents[1]


def denied(command: str) -> bool:
    return judge(command, WS).denied


# ------------------------------------------------------------------ 1. 嵌套 shell


@pytest.mark.parametrize("command", [
    'bash -c "rm -rf /"',
    "sh -c 'rm -rf /'",
    'zsh -c "rm -rf /"',
    'dash -c "rm -rf /"',
    'bash -lc "rm -rf /"',          # 组合 flag
    """bash -c 'bash -c "rm -rf /"'""",   # 两层嵌套
])
def test_nested_shell_payload_is_judged(command):
    assert denied(command), f"嵌套 shell 没被判: {command}"


def test_nested_shell_allows_safe_payload():
    assert not denied('bash -c "echo hello"')


def test_depth_guard_fails_closed():
    """嵌套太深不猜，直接拒绝 —— 宁可误杀也不能放行看不透的东西。

    这里直接测深度守卫本身：靠拼字符串造 6 层合法的 shell 嵌套很难写对
    （引号会互相吃掉），而守卫的逻辑就是"depth 超过上限就拒绝"。
    """
    from minicode.policy import MAX_NEST_DEPTH, _judge_inner

    assert _judge_inner("rm -rf /", WS, MAX_NEST_DEPTH + 1).denied
    assert _judge_inner("echo hi", WS, MAX_NEST_DEPTH + 1).denied


# ------------------------------------------------------------------ 2. Windows 等价物


@pytest.mark.parametrize("command", [
    'powershell -NoProfile -Command "Remove-Item -Recurse -Force C:\\Windows"',
    "cmd /c del /s /q C:\\Windows",
    "cmd /k rd /s /q C:\\",
    "Remove-Item -Recurse -Force C:\\Windows",
    "ri -Recurse -Force /etc",
    "erase /f /s /q C:\\Windows",
    "Set-Content -Path C:\\Windows\\x.txt -Value hi",
    "Copy-Item .env C:\\stolen\\",
])
def test_windows_destructive_commands_are_denied(command):
    assert denied(command), f"PowerShell/cmd 的破坏性命令没被拦: {command}"


def test_remove_inside_workspace_is_allowed():
    """工作区里删构建产物是正常操作，不能误杀。"""
    assert not denied("Remove-Item -Recurse -Force ./build")
    assert not denied("rm -rf dist")
    assert not denied("rm -rf node_modules")


# ------------------------------------------------------------------ 3. 包装器


@pytest.mark.parametrize("command", [
    "sudo -u root rm -rf /usr",
    "sudo env rm -rf /usr",
    "doas -u root rm -rf /etc",
    "timeout 5 rm -rf /usr",
    "env FOO=bar rm -rf /usr",
    "nice -n 10 rm -rf /usr",
])
def test_wrappers_are_unwrapped(command):
    assert denied(command), f"包装器后面的危险命令没判: {command}"


def test_wrapper_with_safe_command_allowed():
    assert not denied("sudo ls")
    assert not denied("timeout 5 echo hi")
    assert not denied("env A=1 python -m pytest")


# ------------------------------------------------------------------ 4. 危险目标


@pytest.mark.parametrize("command", [
    "rm -rf $HOME",
    "rm -rf ${HOME}",
    "rm -rf ~",
    "rm -rf ~/data",
    "rm -rf .",
    "rm -rf *",
])
def test_dangerous_targets_are_expanded(command):
    assert denied(command), f"危险目标没被识别: {command}"


def test_dangerous_target_case_insensitive():
    """$HOME 之前写成大写，比较时转小写导致规则永远不命中。"""
    assert denied("rm -rf $home")


# ------------------------------------------------------------------ 5. 分隔符


@pytest.mark.parametrize("command", [
    "true & rm -rf /",
    "echo hi & rm -rf /",
    "true && rm -rf /",
    "echo hi; rm -rf /",
    "echo hi | xargs rm -rf /",
])
def test_command_separators(command):
    assert denied(command), f"分隔符后面的危险命令没判: {command}"


def test_redirect_stderr_not_split():
    """2>&1 里的 & 不是分隔符，不能因此误判。"""
    assert not denied("echo hi 2>&1")
    assert not denied("python -m pytest > out.txt 2>&1")


# ------------------------------------------------------------------ 6. git


@pytest.mark.parametrize("command", [
    "git reset --hard",
    "git clean -f -d",
    "git clean -df",
    "git clean -fdxx",
    "git clean -fdx",
    "git restore .",
    "git checkout -- .",
])
def test_destructive_git_is_denied(command):
    assert denied(command), f"破坏性 git 没拦: {command}"


def test_safe_git_allowed():
    assert not denied("git status")
    assert not denied("git diff")
    assert not denied("git clean -n")
    assert not denied("git checkout -b feature")


# ------------------------------------------------------------------ 7. 工作区边界


@pytest.mark.parametrize("command", [
    "Set-Content -Path ../outside.txt -Value hi",
    "echo x > ../outside.txt",
    "Copy-Item .env D:\\stolen\\",
    "Move-Item a.txt C:\\Windows\\a.txt",
])
def test_shell_write_outside_workspace_denied(command):
    assert denied(command), f"shell 写到工作区外没拦: {command}"


def test_shell_write_inside_workspace_allowed():
    assert not denied("echo hello > out.txt")
    assert not denied("Set-Content -Path .\\build\\out.txt -Value hi")


# ------------------------------------------------------------------ 8. 误杀防护


@pytest.mark.parametrize("command", [
    "ls -la",
    "python -m pytest -q",
    "npm run build",
    "git log --oneline -5",
    "rg 'def main' src",
    "Get-ChildItem -Recurse -File",
    "Get-Content README.md",
    "cd src && python -m pytest",
])
def test_normal_commands_still_allowed(command):
    assert not denied(command), f"正常命令被误杀: {command}"
