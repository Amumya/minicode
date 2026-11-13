"""权限判定：先把命令解析成结构，再按规则判 allow / deny。

为什么不直接用子串黑名单：

    子串匹配 `"rm -rf" in cmd` 会同时犯两类错。
    误报：`echo "rm -rf /"` 和 `grep -r "rm -rf" .` 都会被当成危险命令。
    漏报：`rm  -rf  /`（多空格）、`rm -fr /`（参数顺序）、`/bin/rm -rf /`
         （绝对路径）都匹配不上 `rm -rf` 这个字面量。

    这个模块先把命令切开、分词，拿到"程序名 + 参数"，再按参数语义判断。
    这样多空格、参数顺序、绝对路径都不影响判定。

已知的局限（先写在这，免得被当成全）：

    * 按 `;` `&&` `||` `|` 换行切分，不看引号里的分隔符。
      所以 `echo "a; rm -rf /"` 会被切成两段，第二段是假阳性 —— 但方向是误报，不是漏报。
    * 不处理 `$(...)`、反引号、heredoc 里面的命令。
    * 规则是白名单式的判断，不是完整的 shell 语法分析。
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

# 判定结果里用稳定的原因码，不要用中文描述去比对（描述以后会改）
DENIED_UNPARSABLE = "unparsable"
DENIED_SYSTEM_CMD = "system_command"
DENIED_DEVICE_WRITE = "device_write"
DENIED_FORK_BOMB = "fork_bomb"
DENIED_REMOTE_EXEC = "remote_exec"
DENIED_DANGEROUS_TARGET = "dangerous_target"
DENIED_SYSTEM_PATH_WRITE = "system_path_write"
DENIED_OUTSIDE_WORKSPACE = "outside_workspace"
DENIED_DESTRUCTIVE_GIT = "destructive_git"
ALLOWED = "ok"

# 直接禁止的程序
BANNED_PROGRAMS = {
    "shutdown", "reboot", "poweroff", "halt", "init", "telinit",
    "fdisk", "parted", "mkfs", "mkfs.ext4", "mkfs.xfs", "mkfs.vfat",
    "wipefs", "shred",
}

# 递归删除时要拦下的目标
DANGEROUS_TARGETS = {
    "/", "/*", "~", "~/*", "$HOME", "*", ".", "..",
    "c:\\", "c:/", "d:\\", "d:/",
}

# 系统目录：写入一律拦
SYSTEM_PATHS = (
    "/etc", "/usr", "/bin", "/sbin", "/boot", "/dev", "/var", "/lib", "/opt",
    "c:\\windows", "c:/windows", "c:\\program files", "c:/program files",
)

# 会破坏工作区、但不属于"删系统"的命令组合
DESTRUCTIVE_GIT = {
    ("reset", "--hard"),
    ("clean", "-fdx"),
    ("clean", "-fd"),
    ("clean", "-xdf"),
}

REMOTE_FETCHERS = {"curl", "wget", "fetch"}
SHELLS = {"sh", "bash", "zsh", "dash", "powershell", "pwsh"}

_SEPARATORS = re.compile(r"\|\||&&|;|\n")
_FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{|:\s*\(\s*\)\s*\{")


@dataclass(frozen=True)
class Segment:
    """命令链里的一段。"""

    tokens: tuple[str, ...]

    @property
    def program(self) -> str:
        if not self.tokens:
            return ""
        return os.path.basename(self.tokens[0].strip("\"'")).lower()

    @property
    def args(self) -> tuple[str, ...]:
        return tuple(self.tokens[1:])


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    reason: str
    detail: str = ""
    program: str = ""

    @property
    def denied(self) -> bool:
        return not self.allowed


def _strip_quotes(tok: str) -> str:
    if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in "\"'":
        return tok[1:-1]
    return tok


def split_segments(command: str) -> list[list[Segment]]:
    """切成若干条"管道链"，每条链里再切成段。

    返回结构：外层是 `;` `&&` `||` 换行切开的链，内层是 `|` 切开的段。
    """
    chains: list[list[Segment]] = []
    for chain in _SEPARATORS.split(command):
        if not chain.strip():
            continue
        segs: list[Segment] = []
        for part in chain.split("|"):
            if not part.strip():
                continue
            try:
                # posix=False：保留反斜杠，不然 Windows 路径会被吃掉
                raw = shlex.split(part, posix=False)
            except ValueError as exc:
                raise ValueError(f"无法分词: {exc}") from exc
            tokens = tuple(_strip_quotes(t) for t in raw)
            if tokens:
                segs.append(Segment(tokens))
        if segs:
            chains.append(segs)
    return chains


def _has_short_flag(args: tuple[str, ...], letter: str) -> bool:
    """判断是否出现 -r / -f 这类短参数，支持 -rf 合写。"""
    for a in args:
        if a.startswith("-") and not a.startswith("--") and letter in a[1:]:
            return True
    return False


def _is_dangerous_target(target: str) -> bool:
    t = target.strip().strip("\"'").lower().rstrip("/")
    if t in ("", "."):
        return True
    return target.strip().strip("\"'").lower() in DANGEROUS_TARGETS or t in (
        "", "/", "*", "~",
    )


def _is_system_path(target: str) -> bool:
    low = target.strip().strip("\"'").lower().replace("\\\\", "\\")
    return any(low == p or low.startswith(p + os.sep) or low.startswith(p + "/")
               for p in SYSTEM_PATHS)


def _outside_workspace(target: str, workspace: Path) -> bool:
    if not target or target.startswith("-"):
        return False
    try:
        p = Path(target)
    except (ValueError, OSError):
        return False
    try:
        resolved = p.resolve() if p.is_absolute() else (workspace / p).resolve()
        resolved.relative_to(workspace.resolve())
        return False
    except (ValueError, OSError):
        return True


def judge(command: str, workspace: Path | None = None) -> Verdict:
    """判一条命令能不能执行。workspace 用来判断"是否越界"。"""
    workspace = (workspace or Path.cwd()).resolve()
    cmd = (command or "").strip()
    if not cmd:
        return Verdict(True, ALLOWED, "空命令")

    if _FORK_BOMB.search(cmd):
        return Verdict(False, DENIED_FORK_BOMB, "fork 炸弹")

    try:
        chains = split_segments(cmd)
    except ValueError as exc:
        return Verdict(False, DENIED_UNPARSABLE, str(exc))

    for chain in chains:
        # 同一条管道里：抓取远程内容直接喂给 shell
        programs = [s.program for s in chain]
        if len(programs) > 1:
            first = programs[0]
            if first in REMOTE_FETCHERS and any(p in SHELLS for p in programs[1:]):
                return Verdict(False, DENIED_REMOTE_EXEC,
                               f"{first} 的输出直接管道给 shell")

        for seg in chain:
            verdict = _check_segment(seg, workspace)
            if verdict.denied:
                return verdict

    return Verdict(True, ALLOWED, "")


def _check_segment(seg: Segment, workspace: Path) -> Verdict:
    prog = seg.program
    args = seg.args

    # sudo / doas 只是提权包装，真正要判的是它后面的命令
    if prog in ("sudo", "doas", "runas"):
        inner = [a for a in args if not a.startswith("-")]
        if inner:
            return _check_segment(Segment(tuple(inner)), workspace)
        return Verdict(True, ALLOWED)

    if prog in BANNED_PROGRAMS or prog.startswith("mkfs"):
        return Verdict(False, DENIED_SYSTEM_CMD, prog)

    if prog == "dd" and any(a.lower().startswith("of=/dev/") for a in args):
        return Verdict(False, DENIED_DEVICE_WRITE, "dd 写设备")

    if prog in ("rm", "del", "rd", "rmdir"):
        recursive = _has_short_flag(args, "r") or "--recursive" in args
        force = _has_short_flag(args, "f") or "--force" in args
        targets = [a for a in args if not a.startswith("-")]
        for t in targets:
            if _is_dangerous_target(t):
                return Verdict(False, DENIED_DANGEROUS_TARGET, f"删除 {t}")
            if _is_system_path(t):
                return Verdict(False, DENIED_SYSTEM_PATH_WRITE, f"删除系统目录 {t}")
            if recursive and _outside_workspace(t, workspace):
                return Verdict(False, DENIED_OUTSIDE_WORKSPACE, f"递归删除工作区外的 {t}")
        # 递归+强制 但目标都在工作区内 → 允许（清构建产物是正常操作）
        _ = force
        return Verdict(True, ALLOWED)

    if prog in ("chmod", "chown", "takeown", "icacls"):
        recursive = _has_short_flag(args, "r") or "--recursive" in args
        for t in args:
            if t.startswith("-"):
                continue
            if _is_dangerous_target(t) or _is_system_path(t):
                return Verdict(False, DENIED_DANGEROUS_TARGET, f"{prog} {t}")
            if recursive and _outside_workspace(t, workspace):
                return Verdict(False, DENIED_OUTSIDE_WORKSPACE, f"{prog} -R {t}")
        return Verdict(True, ALLOWED)

    if prog == "git":
        pairs = {(args[i], args[i + 1]) for i in range(len(args) - 1)}
        if pairs & DESTRUCTIVE_GIT:
            return Verdict(False, DENIED_DESTRUCTIVE_GIT, " ".join(args[:2]))
        return Verdict(True, ALLOWED)

    # 重定向写系统目录
    for i, tok in enumerate(seg.tokens):
        if tok in (">", ">>"):
            if i + 1 < len(seg.tokens) and _is_system_path(seg.tokens[i + 1]):
                return Verdict(False, DENIED_SYSTEM_PATH_WRITE,
                               f"写入 {seg.tokens[i + 1]}")

    return Verdict(True, ALLOWED)


class SubstringBlacklistJudge:
    """对照组：照着最朴素的写法做的子串黑名单。

    存在的意义是当基线 —— 不做结构化解析会漏什么、误伤什么，用它来量化。
    """

    PATTERNS = ("rm -rf", "mkfs", "dd if=", "shutdown", "reboot", "| sh", "| bash")

    def judge(self, command: str, workspace: Path | None = None) -> Verdict:
        low = (command or "").lower()
        for p in self.PATTERNS:
            if p in low:
                return Verdict(False, "blacklist", p)
        return Verdict(True, ALLOWED)


def judge_tool_call(
    tool_name: str, args: dict | None, workspace: Path | None = None
) -> Verdict | None:
    """把工具调用翻译成"要不要判"。返回 None 表示这个工具不需要判权限。

    现在只有 bash 需要判 —— 它的参数里就是一条命令。
    以后加了文件工具，写路径也要在这里判越界。
    """
    if tool_name == "bash":
        command = (args or {}).get("command", "")
        return judge(str(command), workspace)
    return None

