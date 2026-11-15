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
from fnmatch import fnmatch
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
DENIED_PROTECTED_FILE = "protected_file"
ALLOWED = "ok"

# 测试文件不允许 agent 改。改测试等于自己给自己判卷。
# 这条是防作弊的第一道防线，和评测层的隐藏测试配合使用。
PROTECTED_NAME_PATTERNS = (
    "test_*.py", "*_test.py", "conftest.py", "hidden_*.py", "check_*.py",
)

WRITE_TOOLS = ("write_file", "edit_file")
FILE_TOOLS = ("read_file", "write_file", "edit_file")


# 直接禁止的程序
BANNED_PROGRAMS = {
    "shutdown", "reboot", "poweroff", "halt", "init", "telinit",
    "fdisk", "parted", "mkfs", "mkfs.ext4", "mkfs.xfs", "mkfs.vfat",
    "wipefs", "shred",
    # Windows 上等价的东西 —— 实际执行器是 PowerShell，不认这些名字等于没装锁
    "format", "diskpart", "reg", "regedit", "bcdedit", "vssadmin",
    "wbadmin", "cipher",
}

# 递归删除时要拦下的目标。**全部小写**：比较前会统一转小写，
# 之前 $HOME 写成大写导致这条规则永远不命中（实测能删掉家目录）。
DANGEROUS_TARGETS = {
    "/", "/*", "~", "~/*", "$home", "${home}", "$env:userprofile",
    "*", ".", "..", "c:\\", "c:/", "d:\\", "d:/",
}

# 系统目录：写入一律拦
SYSTEM_PATHS = (
    "/etc", "/usr", "/bin", "/sbin", "/boot", "/dev", "/var", "/lib", "/opt",
    "c:\\windows", "c:/windows", "c:\\program files", "c:/program files",
    "c:\\programdata", "c:/programdata",
)

# 删除类命令：POSIX 的、PowerShell 的、cmd 内建的都要认
DELETE_CMDS = {
    "rm", "del", "rd", "rmdir", "erase",
    "remove-item", "ri", "clear-content", "clc",
}

# 写入类命令：判它们的目标是不是越界（文件工具已经判了，shell 这条线也要判）
WRITE_CMDS = {
    "set-content", "add-content", "out-file", "new-item",
    "copy-item", "move-item", "rename-item", "copy", "move", "tee",
}

# 这些参数后面跟的是"值"，不是路径 —— 判越界时要跳过它的取值。
# 注意 -Path / -Destination / -NewName 不在里面：它们的取值**就是**路径，要判。
_VALUE_FLAGS = {
    "-value", "-encoding", "-itemtype", "-filter", "-include",
    "-exclude", "-inputobject", "-credential", "-confirm",
}

# 会破坏工作区改动的 git 子命令（按 flag 组合判，不再靠字面相邻对）
GIT_FLAGS = {"-f", "-d", "-x", "--hard", "--force"}

REMOTE_FETCHERS = {
    "curl", "wget", "fetch",
    "invoke-webrequest", "iwr", "invoke-restmethod", "irm",
}
SHELLS = {"sh", "bash", "zsh", "dash", "powershell", "pwsh", "cmd"}

# 包装命令：本身无害，剥掉选项之后判里面的东西
WRAPPERS = {"sudo", "doas", "runas", "env", "timeout", "nohup", "nice",
            "command", "exec", "xargs"}

# 哪些参数后面跟的是"一段要执行的命令"
SHELL_PAYLOAD_FLAGS = {
    "sh": ("-c",),
    "bash": ("-c",),
    "zsh": ("-c",),
    "dash": ("-c",),
    "powershell": ("-command", "-c"),
    "pwsh": ("-command", "-c"),
    "cmd": ("/c", "/k"),
}

# 注意单个 &：POSIX 是后台分隔符，PowerShell 是语句分隔符，两边都能藏命令。
# 但不能把 2>&1 / && 切开 —— 用前后断言排除。
_SEPARATORS = re.compile(r"\|\||&&|;|\n|(?<![>&])&(?![&>])")
_FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{")
MAX_NEST_DEPTH = 4

# cmd 风格开关：/s /q /c /k /f
_CMD_SWITCH = re.compile(r"^/[a-z](?:[a-z])?$")


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


def _tokens_fallback(part: str) -> tuple[str, ...]:
    """shlex 解析失败时的退路：按空白硬切。

    真实轨迹里大量命令是 `python -c "..."` 这种带换行和嵌套引号的单行，
    shlex 会直接抛 ValueError。如果一律当作"无法解析 → 拒绝"，
    agent 会被莫名其妙地拦下来，白白浪费步数（实测每个任务被拒 2-3 次）。

    退路不做引号解析，所以会把引号里的内容也当参数 —— 方向是**偏严**，
    但至少能把 `rm -rf /` 这类显式危险命令认出来。
    """
    cleaned = part.replace("\\\n", " ")
    raw = [t for t in re.split(r"\s+", cleaned) if t]
    return tuple(_strip_quotes(t) for t in raw)


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
            except ValueError:
                raw = list(_tokens_fallback(part))
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


def _expand(target: str) -> str:
    """展开 ~ 和 $VAR / %VAR%，再交给后面的路径判断。

    不展开的话 `rm -rf ~/data` 会被当成工作区里的相对路径 —— 实测放行。
    """
    raw = _strip_quotes(str(target))
    try:
        return os.path.expandvars(os.path.expanduser(raw))
    except (ValueError, OSError):
        return raw


def _is_dangerous_target(target: str) -> bool:
    raw = _strip_quotes(target).strip().lower()
    expanded = _expand(target).strip().lower()
    for candidate in (raw, expanded, raw.rstrip("/"), expanded.rstrip("/")):
        if candidate in DANGEROUS_TARGETS:
            return True
    if raw in ("", ".", "..") or expanded in ("", "."):
        return True
    return False


def _is_system_path(target: str) -> bool:
    raw = _strip_quotes(target).strip().lower().replace("\\\\", "\\")
    expanded = _expand(target).strip().lower().replace("\\\\", "\\")
    for candidate in (raw, expanded):
        if not candidate:
            continue
        for prefix in SYSTEM_PATHS:
            if candidate == prefix or candidate.startswith(prefix + os.sep) or candidate.startswith(prefix + "/"):
                return True
    return False


def _outside_workspace(target: str, workspace: Path) -> bool:
    if not target or target.startswith("-"):
        return False
    try:
        p = Path(_expand(target))
    except (ValueError, OSError):
        return False
    try:
        resolved = p.resolve() if p.is_absolute() else (workspace / p).resolve()
        resolved.relative_to(workspace.resolve())
        return False
    except (ValueError, OSError):
        return True


def _path_targets(args: tuple[str, ...], skip_value_flags: bool = False) -> list[str]:
    """从参数里挑出"看起来是路径"的位置参数。

    跳过 -Force/-Recurse 这类开关、/s /q 这类 cmd 开关，以及
    -Path/-Value 这类"后面跟值"的选项（值不是路径）。
    """
    out: list[str] = []
    skip_next = False
    no_value_flags = {
        "-f", "-r", "-rf", "-fr", "-force", "-recurse", "-confirm",
        "-verbose", "-whatif", "-passthru", "--force", "--recursive",
        "-y", "-q",
    }
    for a in args:
        if skip_next:
            skip_next = False
            continue
        low = a.lower()
        # 注意：不能只判断 startswith("/") —— 那会把根目录 "/" 和 "/usr" 一起吃掉，
        # 导致 rm -rf / 直接放行（第一版就是这么错的）。只认 /s /q /c 这种 cmd 开关。
        if low in no_value_flags or _CMD_SWITCH.match(low):
            continue
        if a.startswith("-"):
            if skip_value_flags and low in _VALUE_FLAGS:
                skip_next = True
            continue
        out.append(a)
    return out


def _strip_wrapper(prog: str, args: tuple[str, ...]) -> list[str]:
    """剥掉包装命令的选项和它们的取值，返回真正要执行的命令。"""
    if prog == "env":
        i = 0
        while i < len(args) and (args[i].startswith("-") or "=" in args[i]):
            i += 1
        return list(args[i:])
    if prog == "timeout":
        i = 0
        while i < len(args) and args[i].startswith("-"):
            i += 1
        if i < len(args):
            i += 1          # 跳过 DURATION
        return list(args[i:])
    if prog == "nice":
        if len(args) >= 2 and args[0] == "-n":
            return list(args[2:])
        if args and args[0].startswith("-"):
            return list(args[1:])
        return list(args)
    if prog == "runas":
        i = 0
        while i < len(args) and args[i].startswith("/"):
            i += 1
        return list(args[i:])
    if prog == "xargs":
        # xargs [options] cmd args...
        takes_value = {"-n", "-i", "-l", "-p", "-s", "-e", "-d", "-a", "-eof"}
        i = 0
        while i < len(args) and args[i].startswith("-"):
            a = args[i].lower()
            if a == "--":
                i += 1
                break
            i += 2 if a in takes_value else 1
        return list(args[i:])
    takes_value = {"-u", "-g", "-p", "-c", "-t", "-r", "-cwd", "-user",
                   "--user", "--group", "--prompt", "--chdir"}
    i = 0
    while i < len(args) and args[i].startswith("-"):
        a = args[i].lower()
        if a == "--":
            i += 1
            break
        i += 2 if a in takes_value else 1
    return list(args[i:])


def _shell_payload(prog: str, args: tuple[str, ...]) -> str | None:
    """取出 `bash -c "..."` / `powershell -Command "..."` / `cmd /c ...` 里的那段命令。"""
    flags = SHELL_PAYLOAD_FLAGS.get(prog)
    if not flags:
        return None
    lowered = [a.lower() for a in args]
    for i, a in enumerate(lowered):
        hit = a in flags
        # bash -lc / -ec 这种合写
        if not hit and prog in ("sh", "bash", "zsh", "dash"):
            hit = re.fullmatch(r"-[a-z]*c[a-z]*", a) is not None
        if hit and i + 1 < len(args):
            # cmd /c、powershell -Command 后面整段都是命令；POSIX 的 -c 只取下一个参数
            if prog in ("cmd", "powershell", "pwsh"):
                return " ".join(args[i + 1:])
            return args[i + 1]
    return None


def _check_git(args: tuple[str, ...]) -> Verdict:
    """git 的破坏性操作按 flag 组合判。

    之前用"相邻参数对"精确匹配，`git clean -f -d`、`-df`、`-fdxx` 全部漏网。
    """
    flags: set[str] = set()
    positional: list[str] = []
    for a in args:
        if a.startswith("--"):
            flags.add(a.lower())
        elif a.startswith("-"):
            flags.update("-" + c for c in a[1:].lower())
        else:
            positional.append(a)

    sub = positional[0] if positional else ""
    rest = positional[1:]

    if sub == "reset" and "--hard" in flags:
        return Verdict(False, DENIED_DESTRUCTIVE_GIT, "git reset --hard")
    if sub == "clean" and "-f" in flags and ("-d" in flags or "-x" in flags):
        return Verdict(False, DENIED_DESTRUCTIVE_GIT, "git clean 会删掉未跟踪文件")
    if sub == "restore" and any(p in (".", "*", ":/") for p in rest):
        return Verdict(False, DENIED_DESTRUCTIVE_GIT, "git restore .")
    if sub == "checkout" and any(p in (".", "*") for p in rest):
        return Verdict(False, DENIED_DESTRUCTIVE_GIT, "git checkout -- .")
    return Verdict(True, ALLOWED)


def judge(command: str, workspace: Path | None = None) -> Verdict:
    """判一条命令能不能执行。workspace 用来判断"是否越界"。"""
    return _judge_inner(command, workspace, 0)


def _judge_inner(command: str, workspace: Path | None, depth: int) -> Verdict:
    workspace = (workspace or Path.cwd()).resolve()
    cmd = (command or "").strip()
    if not cmd:
        return Verdict(True, ALLOWED, "空命令")
    if depth > MAX_NEST_DEPTH:
        return Verdict(False, DENIED_UNPARSABLE, "嵌套层数太深，拒绝判定")

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
            verdict = _check_segment(seg, workspace, depth)
            if verdict.denied:
                return verdict

    return Verdict(True, ALLOWED, "")


def _check_segment(seg: Segment, workspace: Path, depth: int = 0) -> Verdict:
    prog = seg.program
    args = seg.args

    if depth > MAX_NEST_DEPTH:
        return Verdict(False, DENIED_UNPARSABLE, "嵌套层数太深")

    # 包装命令：判里面的那条，而不是判 sudo/env/timeout 自己
    if prog in WRAPPERS:
        inner = _strip_wrapper(prog, args)
        if inner:
            return _check_segment(Segment(tuple(inner)), workspace, depth + 1)
        return Verdict(True, ALLOWED)

    # shell -c "...": 把 payload 当命令重新判（之前这里直接放行，一层包装就能绕过）
    if prog in SHELL_PAYLOAD_FLAGS:
        payload = _shell_payload(prog, args)
        if payload:
            nested = _judge_inner(payload, workspace, depth + 1)
            if nested.denied:
                return Verdict(False, nested.reason,
                               f"{prog} 里嵌套的命令: {nested.detail}", prog)
        return Verdict(True, ALLOWED)

    if prog in BANNED_PROGRAMS or prog.startswith("mkfs"):
        return Verdict(False, DENIED_SYSTEM_CMD, prog)

    if prog == "dd" and any(a.lower().startswith("of=/dev/") for a in args):
        return Verdict(False, DENIED_DEVICE_WRITE, "dd 写设备")

    is_find_delete = prog == "find" and "-delete" in args
    if prog in DELETE_CMDS or is_find_delete:
        recursive = (
            _has_short_flag(args, "r") or "--recursive" in args
            or "--recurse" in args or "/s" in args or is_find_delete
        )
        targets = _path_targets(args)
        for t in targets:
            if _is_dangerous_target(t):
                return Verdict(False, DENIED_DANGEROUS_TARGET, f"删除 {t}")
            if _is_system_path(t):
                return Verdict(False, DENIED_SYSTEM_PATH_WRITE, f"删除系统目录 {t}")
            if recursive and _outside_workspace(t, workspace):
                return Verdict(False, DENIED_OUTSIDE_WORKSPACE, f"递归删除工作区外的 {t}")
        return Verdict(True, ALLOWED)

    if prog in ("chmod", "chown", "takeown", "icacls"):
        recursive = _has_short_flag(args, "r") or "--recursive" in args
        for t in _path_targets(args):
            if _is_dangerous_target(t) or _is_system_path(t):
                return Verdict(False, DENIED_DANGEROUS_TARGET, f"{prog} {t}")
            if recursive and _outside_workspace(t, workspace):
                return Verdict(False, DENIED_OUTSIDE_WORKSPACE, f"{prog} -R {t}")
        return Verdict(True, ALLOWED)

    # 写入类命令：和文件工具保持同一条工作区边界
    if prog in WRITE_CMDS:
        for t in _path_targets(args, skip_value_flags=True):
            if _is_system_path(t):
                return Verdict(False, DENIED_SYSTEM_PATH_WRITE, f"{prog} 写系统目录 {t}")
            if _outside_workspace(t, workspace):
                return Verdict(False, DENIED_OUTSIDE_WORKSPACE, f"{prog} 写工作区外 {t}")
        return Verdict(True, ALLOWED)

    if prog == "git":
        return _check_git(args)

    # 重定向写系统目录（也顺带盯住工作区外）
    for i, tok in enumerate(seg.tokens):
        if tok in (">", ">>"):
            if i + 1 < len(seg.tokens):
                target = seg.tokens[i + 1]
                if _is_system_path(target):
                    return Verdict(False, DENIED_SYSTEM_PATH_WRITE, f"写入 {target}")
                if _outside_workspace(target, workspace):
                    return Verdict(False, DENIED_OUTSIDE_WORKSPACE,
                                   f"重定向到工作区外 {target}")

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


def _is_protected_path(path: str) -> bool:
    name = os.path.basename(str(path).replace("\\", "/")).lower()
    return any(fnmatch(name, pat) for pat in PROTECTED_NAME_PATTERNS)


def judge_tool_call(
    tool_name: str, args: dict | None, workspace: Path | None = None
) -> Verdict | None:
    """把工具调用翻译成"要不要判"。返回 None 表示这个工具不需要判权限。

    bash 判命令本身；文件工具判路径（越界、改测试文件）。
    """
    workspace = (workspace or Path.cwd()).resolve()
    args = args or {}

    if tool_name == "bash":
        return judge(str(args.get("command", "")), workspace)

    if tool_name in FILE_TOOLS:
        rel = str(args.get("path", ""))
        if not rel:
            return Verdict(False, DENIED_OUTSIDE_WORKSPACE, "没有 path")
        try:
            target = Path(rel)
            resolved = (target if target.is_absolute() else workspace / target).resolve()
            resolved.relative_to(workspace)
        except (ValueError, OSError):
            return Verdict(False, DENIED_OUTSIDE_WORKSPACE, f"工作目录外: {rel}")

        if tool_name in WRITE_TOOLS and _is_protected_path(rel):
            return Verdict(False, DENIED_PROTECTED_FILE, f"不能改测试文件: {rel}")
        return None

    return None


