"""Conservative shell-command analyzer for the access policy.

The previous scanner tokenised on whitespace and regexes. That is a heuristic:
escaped spaces, command substitution, encoded PowerShell, `cmd /c` wrappers and
`for` loops all let a command name paths the scanner never saw. This module
follows the portable-scanner approach used by opencode: parse the command into
segments, resolve what can be resolved literally, and **deny anything that
cannot be statically determined** (fail closed).

What is resolved:

* quoting for POSIX shells, cmd.exe and PowerShell, including escaped spaces
  (``C:\\Program\\ Files``, ``C:\\Program` Files``, ``C:\\Program^ Files``);
* known environment variables (``%TEMP%``, ``$env:TEMP``, ``$HOME``, ...);
* redirect targets (``>``, ``>>``, ``2>``, ``&>``);
* wrapper commands (``cmd /c``, ``powershell -Command``, ``bash -c``,
  ``env``, ``sudo``, ``command``) by parsing the inner command recursively.

What is denied outright:

* command substitution (``$(...)``, backticks), unknown variables, delayed
  expansion (``!VAR!``), script blocks, heredocs and control-flow commands;
* encoded/dynamic execution (``-EncodedCommand``, ``Invoke-Expression``,
  ``certutil -decode``, ``mshta``, ``wsl``, ``ssh``, ...);
* inline interpreter code (``python -c``, ``node -e``, ...);
* executables that read a script from stdin (``bash``, ``cmd``, ``pwsh``
  with no ``-c``/``/c``).

Every parsed path is still checked against the same :class:`AccessPolicy`
(read/write, cwd, read-only whitelist).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from supervisor.security.access_policy import (
    AccessPolicy,
    PolicyViolation,
)

# Commands whose file effects cannot be bounded by argument inspection.
_DENY_COMMANDS = frozenset(
    {
        "eval",
        "exec",
        "iex",
        "invoke-expression",
        "invoke-command",
        "start-process",
        "start-job",
        "register-scheduledtask",
        "schtasks",
        "wmic",
        "reg",
        "regedit",
        "certutil",
        "bitsadmin",
        "mshta",
        "rundll32",
        "regsvr32",
        "cscript",
        "wscript",
        "wsl",
        "docker",
        "podman",
        "ssh",
        "scp",
        "sftp",
        "telnet",
        "ftp",
        "nc",
        "ncat",
        "netcat",
        "socat",
        "psexec",
        "winrs",
        "winrm",
        "xargs",
        "parallel",
        "for",
        "foreach",
        "while",
        "until",
        "goto",
        "call",
        "start",
        "setlocal",
        "pushd",
        "popd",
    },
)

# Process termination. A kill is only ever legal against a literal PID that
# the run's own ledger proves it owns (checked by guard_cli); every
# name/keyword/wildcard form is denied here because it can reach processes
# this run never started.
_KILL_COMMANDS = frozenset(
    {"taskkill", "stop-process", "kill", "killall", "pkill", "pskill", "tskill"},
)
# Flags that turn a PID kill into a name/tree/remote kill.
_KILL_FORBIDDEN_FLAGS = frozenset(
    {
        "/im",
        "-name",
        "/t",
        "-t",
        "/fi",
        "/s",
        "/u",
        "/p",
        "-inputobject",
        "-id?",
        "--signal",
        "-signal",
    },
)
# MATLAB must be started through the run's owned wrapper: a bare `matlab`
# spawns a launcher plus a MATLAB.exe engine that no ledger knows about, and
# the only cleanup left would be a name-based kill.
_MATLAB_COMMANDS = frozenset({"matlab", "matlab.exe"})
_MATLAB_WRAPPER = "supervisor.tools.run_child"

# Interpreters: a script file inside the cwd is allowed, inline code is not.
_INTERPRETERS = frozenset(
    {
        "python",
        "python3",
        "py",
        "node",
        "nodejs",
        "ruby",
        "perl",
        "php",
        "lua",
        "rscript",
        "deno",
        "bun",
        "osascript",
    },
)
_INLINE_FLAGS = frozenset({"-c", "-e", "-r", "-p", "--eval", "--print", "eval", "-command"})

_POSIX_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh"})

_COPY = frozenset({"cp", "copy", "copy-item", "cpi", "xcopy", "robocopy", "rsync", "install"})
_MOVE = frozenset({"mv", "move", "move-item", "mi", "rename", "ren"})
_WRITE = frozenset(
    {
        "rm",
        "del",
        "erase",
        "rmdir",
        "rd",
        "mkdir",
        "md",
        "touch",
        "ni",
        "new-item",
        "set-content",
        "sc",
        "add-content",
        "ac",
        "out-file",
        "tee",
        "truncate",
        "dd",
        "chmod",
        "chown",
        "attrib",
        "icacls",
        "takeown",
        "mklink",
        "ln",
        "unlink",
        "expand-archive",
        "compress-archive",
        "sed",
        "perl",
    },
)
_READ = frozenset(
    {
        "cat",
        "type",
        "gc",
        "get-content",
        "more",
        "less",
        "head",
        "tail",
        "find",
        "dir",
        "ls",
        "gci",
        "get-childitem",
        "stat",
        "file",
        "xxd",
        "od",
        "strings",
        "du",
        "df",
        "grep",
        "rg",
        "where",
        "which",
        "cd",
        "chdir",
        "pwd",
    },
)

# Commands whose arguments are text, not files; redirects still apply.
_TEXT_ONLY = frozenset({"echo", "printf", "write-output", "write-host", "set", "export"})

_NAMED_WRITE = frozenset({"-destination", "-target", "-outfile", "-outputfile", "-literalpath"})
_NAMED_READ = frozenset({"-path", "-source", "-from", "-include"})

_KNOWN_VARS = {
    "HOME",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "TEMP",
    "TMP",
    "PWD",
    "CD",
    "SYSTEMROOT",
    "WINDIR",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
}

_DYNAMIC_RE = re.compile(r"\$\(|`|%[A-Za-z_][A-Za-z0-9_]*%|![A-Za-z_][A-Za-z0-9_]*!|@\(|\$\{")


@dataclass(frozen=True)
class Token:
    text: str
    dynamic: bool = False


def analyze(
    policy: AccessPolicy,
    command: str,
    cwd: str | Path | None = None,
) -> list[PolicyViolation]:
    """Parse *command* and return every policy violation (fail closed)."""
    if not policy.enabled or not command or not command.strip():
        return []
    ctx = _Context(policy, Path(cwd) if cwd else policy.workspace)
    ctx.visit(command, depth=0)
    return ctx.violations


class _Context:
    def __init__(self, policy: AccessPolicy, cwd: Path) -> None:
        self.policy = policy
        self.cwd = cwd
        self.violations: list[PolicyViolation] = []
        self._seen: set[tuple[str, str]] = set()

    def deny(self, label: str, reason: str) -> None:
        key = (label, reason)
        if key in self._seen:
            return
        self._seen.add(key)
        self.violations.append(PolicyViolation(label, "write", reason))

    def check(self, raw: str, access: str) -> None:
        decision = self.policy.check(raw, access)  # type: ignore[arg-type]
        if decision.allowed:
            return
        key = (decision.normalized or raw, access)
        if key in self._seen:
            return
        self._seen.add(key)
        self.violations.append(PolicyViolation(raw, access, decision.reason))  # type: ignore[arg-type]

    # -- recursion ------------------------------------------------------- #

    def visit(self, command: str, depth: int) -> None:
        if depth > 4:
            self.deny(_short(command), "command nesting is too deep to verify")
            return

        segments = _split_segments(command)
        if not segments:
            self.deny(_short(command), "empty or unparseable command")
            return
        if len(segments) == 0:
            return

        for segment in segments:
            self._segment(segment, depth)

    def _segment(self, segment: str, depth: int) -> None:
        if _has_script_block(segment):
            self.deny(_short(segment), "script blocks cannot be verified")
            return
        if "<<" in segment:
            self.deny(_short(segment), "heredocs cannot be verified")
            return

        tokens = _tokenize(segment, self.cwd)
        if not tokens:
            return
        if any(token.dynamic for token in tokens):
            self.deny(_short(segment), "dynamic expansion cannot be verified")
            return

        # Redirect targets are writes, regardless of the command verb.
        for index, token in enumerate(tokens):
            if token.text in {">", ">>", "2>", "2>>", "&>", "1>", ">|"} and index + 1 < len(tokens):
                target = tokens[index + 1].text
                if target not in {"/dev/null", "NUL"}:
                    self.check(target, "write")

        words = [token for token in tokens if token.text not in _OPERATORS]
        if not words:
            return

        name = _command_name(words[0].text)
        if not name:
            return

        if name in {"powershell", "pwsh", "powershell.exe", "pwsh.exe"} and any(
            word.text.lower() in {"-encodedcommand", "-enc", "-e"} for word in words[1:]
        ):
            self.deny(name, "encoded commands cannot be verified")
            return
        inner = _unwrap(name, words)
        if inner is not None:
            if inner == "":
                self.deny(name, f"{name} executing from stdin cannot be verified")
                return
            self.visit(inner, depth + 1)
            return

        if name in _DENY_COMMANDS:
            self.deny(name, f"'{name}' can execute or modify files outside the static model")
            return
        if name in _KILL_COMMANDS:
            self._kill(name, words)
            return
        if name in _MATLAB_COMMANDS:
            self.deny(
                name,
                "MATLAB must be started through the run's owned wrapper "
                f"(python -m {_MATLAB_WRAPPER} matlab --script ...), so its "
                "process tree is ledgered and can be terminated precisely",
            )
            return
        if name in _POSIX_SHELLS and len(words) == 1:
            self.deny(name, f"{name} executing from stdin cannot be verified")
            return

        if name in _TEXT_ONLY:
            return
        if name in _INTERPRETERS:
            if self.policy.strict_dynamic:
                self.deny(name, "interpreter execution is disabled in strict mode")
                return
            self._interpreter(name, words)
            return

        if name == "git":
            self._git(words)
        if name == "find" and any(
            word.text.lower() in {"-exec", "-execdir", "-ok", "-okdir"} for word in words[1:]
        ):
            self.deny("find", "find -exec can execute an arbitrary command")
            return

        self._paths(name, words)

    # -- command families ------------------------------------------------ #

    def _kill(self, name: str, words: list[Token]) -> None:
        """Only a literal-PID kill is even considered; ownership is checked later.

        The static analyzer cannot know who owns a pid, so it accepts exactly
        one shape — ``taskkill /PID <digits>``, ``Stop-Process -Id <digits>``,
        ``kill <digits>`` — and rejects everything else. The gate
        (:mod:`supervisor.security.guard_cli`) then proves the pid belongs to
        this run's ledger before the command is allowed to run.
        """
        args = [word.text for word in words[1:]]
        lowered = [arg.lower() for arg in args]
        for flag in lowered:
            if flag in _KILL_FORBIDDEN_FLAGS:
                self.deny(
                    name,
                    f"'{name} {flag}' targets processes by name/tree/remote host; "
                    "only a literal PID owned by this run may be terminated",
                )
                return
        pid = _literal_kill_pid(name, args)
        if pid is None:
            self.deny(
                name,
                f"'{name}' must name exactly one literal PID "
                "(taskkill /PID <n>, Stop-Process -Id <n>, kill <n>); "
                "name, keyword and wildcard kills are forbidden",
            )
            return
        # The pid itself is not a path; nothing else to check here.

    def _interpreter(self, name: str, words: list[Token]) -> None:
        args = [word.text for word in words[1:]]
        for index, arg in enumerate(args):
            lowered = arg.lower()
            if lowered in _INLINE_FLAGS:
                self.deny(name, f"{name} inline code cannot be verified")
                return
            if arg == "-" or arg.startswith(("--eval=", "-e=")):
                self.deny(name, f"{name} executing from stdin cannot be verified")
                return
        # A script path is inspected like any other read; its *contents* are
        # not (that would require running a sandbox).
        for index, arg in enumerate(args):
            if arg.startswith("-") or "=" in arg and not arg.startswith(("/", "\\")):
                continue
            if _looks_like_path(arg):
                self.check(arg, "read")
        self._paths(name, words)

    def _git(self, words: list[Token]) -> None:
        args = [word.text for word in words[1:]]
        joined = " ".join(args).lower()
        if "-c" in args:
            lowered = [arg.lower() for arg in args]
            for index, arg in enumerate(lowered):
                if arg == "-c" and index + 1 < len(lowered):
                    setting = lowered[index + 1]
                    if setting.startswith(("core.hookspath", "alias.")) or "!" in setting:
                        self.deny("git", "git config can execute an arbitrary command")
                        return
        for danger in (
            "rebase -x",
            "bisect run",
            "submodule foreach",
            "filter-branch",
            "filter-repo",
            "difftool",
            "mergetool",
        ):
            if danger in joined:
                self.deny("git", f"git {danger} can execute an arbitrary command")
                return
        irreversible = _irreversible_git(args)
        if irreversible:
            self.deny(
                "git",
                f"git {irreversible} is irreversible; take an archive snapshot "
                "first (the supervisor archives before each run)",
            )
            return

    def _paths(self, name: str, words: list[Token]) -> None:
        args = [(index, word.text) for index, word in enumerate(words) if index > 0]
        path_positions = [
            index
            for index, text in args
            if not _is_flag(text) and not _is_operator(text)
        ]
        for index, text in args:
            if _is_flag(text) or _is_operator(text):
                continue
            if "://" in text:
                continue
            if text.lower() in {"/dev/null", "nul", "$null"}:
                continue
            if not _looks_like_path(text):
                continue
            access = _write_intent(name, words, index, path_positions)
            self.check(text, access)


# --------------------------------------------------------------------------- #
# Shell lexing
# --------------------------------------------------------------------------- #

_OPERATORS = frozenset(
    {">", ">>", "2>", "2>>", "&>", "1>", ">|", "<", "<<", "<<<", "|", "||", "&&", "&", ";"},
)


def _split_segments(command: str) -> list[str]:
    """Split on unquoted ``; | && || &`` and newlines (quote/escape aware)."""
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    i = 0
    length = len(command)
    while i < length:
        ch = command[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < length:
                buf.append(command[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "'\"`":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < length:
            buf.append(ch)
            buf.append(command[i + 1])
            i += 2
            continue
        if ch in ";|&\n":
            if buf:
                segments.append("".join(buf).strip())
                buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    if quote or buf:
        segments.append("".join(buf).strip())
    return [segment for segment in segments if segment]


def _tokenize(segment: str, cwd: Path) -> list[Token]:
    tokens: list[Token] = []
    buf: list[str] = []
    dynamic = False
    quote: str | None = None
    i = 0
    length = len(segment)

    def flush() -> None:
        nonlocal buf, dynamic
        if buf or dynamic:
            tokens.append(Token("".join(buf), dynamic))
        buf = []
        dynamic = False

    while i < length:
        ch = segment[i]
        if quote:
            if ch == quote:
                quote = None
                i += 1
                continue
            if quote == '"' and ch == "\\" and i + 1 < length and segment[i + 1] in '"\\$`':
                buf.append(segment[i + 1])
                i += 2
                continue
            if quote == '"' and ch == "`" and i + 1 < length:
                buf.append(_ps_unescape(segment[i + 1]))
                i += 2
                continue
            if quote == '"' and ch == "$":
                value, i, is_dynamic = _expand(segment, i, cwd)
                dynamic = dynamic or is_dynamic
                buf.append(value)
                continue
            buf.append(ch)
            i += 1
            continue

        if ch in " \t\r":
            flush()
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch == "`":
            if i + 1 < length and segment[i + 1] in " \t\"'`nrt$":
                buf.append(_ps_unescape(segment[i + 1]))
                i += 2
                continue
            dynamic = True
            i += 1
            continue
        if ch == "\\" and i + 1 < length and segment[i + 1] in " \t\"'\\":
            buf.append(segment[i + 1])
            i += 2
            continue
        if ch == "^" and i + 1 < length:
            # cmd.exe caret escape: `C://Program^ Files`
            buf.append(segment[i + 1])
            i += 2
            continue
        if ch == "$":
            value, i, is_dynamic = _expand(segment, i, cwd)
            dynamic = dynamic or is_dynamic
            buf.append(value)
            continue
        if ch == "%":
            match = re.match(r"%([A-Za-z_][A-Za-z0-9_]*)%", segment[i:])
            if match:
                name = match.group(1)
                if name.upper() in _KNOWN_VARS or name in os.environ:
                    buf.append(os.environ.get(name, ""))
                else:
                    dynamic = True
                i += len(match.group(0))
                continue
            buf.append(ch)
            i += 1
            continue
        if ch == "!" and re.match(r"![A-Za-z_][A-Za-z0-9_]*!", segment[i:]):
            dynamic = True
            i += 1
            continue
        if ch in ">|<":
            flush()
            operator = ch
            if ch in "><" and i + 1 < length and segment[i + 1] == ch:
                operator += ch
                i += 1
            tokens.append(Token(operator))
            i += 1
            continue
        buf.append(ch)
        i += 1

    flush()
    return tokens


def _ps_unescape(char: str) -> str:
    return {"n": "\n", "r": "\r", "t": "\t"}.get(char, char)


def _expand(text: str, index: int, cwd: Path | None) -> tuple[str, int, bool]:
    """Expand a ``$`` construct. Returns (value, next_index, dynamic)."""
    if text.startswith("$(", index):
        end = _balanced(text, index + 1, "(", ")")
        return "", end, True
    if text.startswith("${", index):
        end = text.find("}", index + 2)
        if end == -1:
            return "", len(text), True
        name = text[index + 2 : end]
        return _known(name), end + 1, name.upper() not in _KNOWN_VARS
    env_match = re.match(r"\$env:([A-Za-z_][A-Za-z0-9_]*)", text[index:], re.IGNORECASE)
    if env_match:
        name = env_match.group(1)
        return os.environ.get(name, ""), index + len(env_match.group(0)), name.upper() not in {
            item.upper() for item in _KNOWN_VARS
        } and name not in os.environ
    match = re.match(r"\$([A-Za-z_][A-Za-z0-9_]*)", text[index:])
    if match:
        name = match.group(1)
        if name in {"HOME", "PWD", "USERPROFILE", "TEMP", "TMP", "CD"}:
            value = str(cwd) if name in {"PWD", "CD"} and cwd else _known(name)
            return value, index + len(match.group(0)), False
        if name.upper() in _KNOWN_VARS:
            return _known(name), index + len(match.group(0)), False
        return "", index + len(match.group(0)), True
    if text.startswith("$?", index) or text.startswith("$$", index):
        return "", index + 2, False
    return "$", index + 1, False


def _known(name: str) -> str:
    upper = name.upper()
    if upper == "HOME":
        return os.path.expanduser("~")
    if upper in {"PWD", "CD"}:
        return os.getcwd()
    return os.environ.get(name, "")


def _balanced(text: str, start: int, opener: str, closer: str) -> int:
    depth = 0
    i = start
    while i < len(text):
        if text[i] == opener:
            depth += 1
        elif text[i] == closer:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return len(text)


def _has_script_block(segment: str) -> bool:
    """An unquoted ``{`` script block cannot be verified.

    ``${VAR}`` is variable expansion, not a block, and is handled by the
    tokenizer; only a bare ``{`` (or ``@{``) is rejected.
    """
    quote: str | None = None
    for index, ch in enumerate(segment):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            continue
        if ch == "{" and (index == 0 or segment[index - 1] != "$"):
            return True
    return False


# --------------------------------------------------------------------------- #
# Command helpers
# --------------------------------------------------------------------------- #


def _unwrap(name: str, words: list[Token]) -> str | None:
    texts = [word.text for word in words]
    if name in {"cmd", "cmd.exe"}:
        for index, text in enumerate(texts[1:], 1):
            if text.lower() in {"/c", "/k"}:
                return " ".join(texts[index + 1 :])
        return ""
    if name in {"powershell", "pwsh", "powershell.exe", "pwsh.exe"}:
        lowered = [text.lower() for text in texts]
        for index, text in enumerate(lowered[1:], 1):
            if text in {"-encodedcommand", "-enc", "-e"} and text != "-e":
                return None
            if text in {"-command", "-c", "--command"}:
                return " ".join(texts[index + 1 :])
            if text in {"-file", "-f"}:
                return None
        return ""
    if name in _POSIX_SHELLS:
        for index, text in enumerate(texts[1:], 1):
            if text == "-c" or (
                text.startswith("-")
                and not text.startswith("--")
                and "c" in text[1:]
            ):
                return " ".join(texts[index + 1 :])
        return "" if len(texts) == 1 else None
    if name in {"env"}:
        index = 1
        while index < len(texts) and ("=" in texts[index] or texts[index].startswith("-")):
            index += 1
        return " ".join(texts[index:]) if index < len(texts) else ""
    if name in {"sudo", "doas", "command", "nohup"}:
        index = 1
        while index < len(texts) and texts[index].startswith("-"):
            index += 1
        return " ".join(texts[index:]) if index < len(texts) else ""
    if name == "timeout":
        index = 1
        while index < len(texts) and texts[index].startswith("-"):
            index += 1
        if index < len(texts):
            index += 1
        return " ".join(texts[index:]) if index < len(texts) else ""
    return None


def _command_name(raw: str) -> str:
    name = raw.strip("\"'`").replace("\\", "/").rsplit("/", 1)[-1].lower()
    name = name.removesuffix(".exe")
    return name


def _literal_kill_pid(name: str, args: list[str]) -> int | None:
    """The single literal PID a kill command names, or None.

    Accepted shapes (nothing else):
      taskkill [/F] /PID <digits>
      Stop-Process [-Force] -Id <digits>
      kill [-9|-KILL] <digits>
    """
    pid_flags = {"/pid", "-id"}
    values: list[str] = []
    index = 0
    while index < len(args):
        arg = args[index]
        lowered = arg.lower()
        if lowered in _KILL_FORBIDDEN_FLAGS:
            # A tree/name/remote flag makes the target set unprovable, even
            # when a pid is also present (`taskkill /T /PID n`).
            return None
        if lowered in pid_flags:
            if index + 1 >= len(args):
                return None
            values.append(args[index + 1])
            index += 2
            continue
        if lowered.startswith(("/pid=", "-id=")):
            values.append(arg.split("=", 1)[1])
            index += 1
            continue
        index += 1
    if values:
        if len(values) != 1:
            return None
        return _as_pid(values[0])
    if name == "kill":
        # `kill <pid>` / `kill -9 <pid>`: the only positional form allowed.
        positional = [
            arg for arg in args if not arg.startswith("-") and not arg.startswith("/")
        ]
        if len(positional) != 1:
            return None
        return _as_pid(positional[0])
    return None


def _as_pid(text: str) -> int | None:
    token = text.strip("\"'`")
    if not token.isdigit():
        return None
    value = int(token)
    return value if value > 4 else None


def _kill_segments(command: str, depth: int = 0) -> list[tuple[str, list[str]]]:
    """Every kill command reachable in *command*, wrappers included.

    The static analyzer recurses through ``cmd /c``, ``powershell -Command``,
    ``bash -c``, ``env``, ``sudo`` and friends via :func:`_unwrap`. The
    ownership gate must see exactly the same commands, otherwise a wrapped
    kill is allowed by the analyzer and invisible to the gate — one shell
    layer would bypass the whole rule.
    """
    if depth > 4:
        return []
    found: list[tuple[str, list[str]]] = []
    for segment in _split_segments(command or ""):
        tokens = _tokenize(segment, Path.cwd())
        words = [token for token in tokens if token.text not in _OPERATORS]
        if not words:
            continue
        name = _command_name(words[0].text)
        if not name:
            continue
        inner = _unwrap(name, words)
        if inner is not None:
            if inner:
                found.extend(_kill_segments(inner, depth + 1))
            continue
        if name in _KILL_COMMANDS:
            found.append((name, [word.text for word in words[1:]]))
    return found


def kill_pids(command: str) -> list[int]:
    """Literal PIDs a command would terminate (empty when none/ambiguous).

    Used by the gate to prove ownership before allowing the command: the
    analyzer already rejected every non-literal shape, so whatever comes back
    here is exactly what the run is asking to kill. Wrapper commands are
    unwrapped with the analyzer's own logic, so ``cmd /c taskkill /PID n``
    yields the same pid as the bare form.
    """
    pids: list[int] = []
    for name, args in _kill_segments(command):
        pid = _literal_kill_pid(name, args)
        if pid is not None:
            pids.append(pid)
    return pids


def kill_command_present(command: str) -> bool:
    """Whether any kill command is reachable, wrappers included."""
    return bool(_kill_segments(command))


def _irreversible_git(args: list[str]) -> str:
    """The destructive git operation *args* performs, or '' when none.

    These discard work that no later command can recover: uncommitted edits
    (``reset --hard``, ``checkout -- .``, ``restore .``), untracked files
    (``clean -f``), commits (``push --force``), stashes and branches. The
    supervisor archives the workspace before each run, so the refusal message
    points at that snapshot instead of pretending the operation is safe.

    ``clean -n`` / ``--dry-run`` is allowed: it reports without deleting.
    """
    lowered = [arg.lower() for arg in args]
    if not lowered:
        return ""
    sub = lowered[0]
    rest = lowered[1:]

    def has(*flags: str) -> bool:
        return any(flag in rest for flag in flags)

    if sub == "reset" and has("--hard"):
        return "reset --hard"
    if sub == "clean":
        # -n / --dry-run only lists; any force flag actually deletes.
        if has("-n", "--dry-run"):
            return ""
        if has("-f", "--force") or any(
            arg.startswith("-") and "f" in arg[1:] and not arg.startswith("--")
            for arg in rest
        ):
            return "clean -f"
        return ""
    if sub == "checkout":
        # `checkout -- .` / `checkout .` discard every uncommitted edit.
        if "--" in rest and rest[-1] in {".", "./"}:
            return "checkout -- ."
        if rest and rest[-1] in {".", "./"} and not any(
            arg.startswith("-") for arg in rest
        ):
            return "checkout ."
        return ""
    if sub == "restore":
        if has("--source", "-s"):
            return "restore --source"
        if rest and rest[-1] in {".", "./"}:
            return "restore ."
        return ""
    if sub == "stash" and rest and rest[0] in {"drop", "clear"}:
        return f"stash {rest[0]}"
    if sub == "branch" and has("-d", "-D", "--delete"):
        return "branch -D"
    if sub == "push" and has("--force", "-f", "--force-with-lease"):
        return "push --force"
    return ""


def _is_flag(text: str) -> bool:
    token = text.strip("\"'`")
    if token.startswith("--"):
        return True
    if token.startswith("-") and len(token) <= 16:
        return True
    return bool(
        token.startswith("/")
        and token.count("/") == 1
        and len(token) <= 5
        and token[1:].isalnum()
    )


def _is_operator(text: str) -> bool:
    return text in _OPERATORS


def _looks_like_path(text: str) -> bool:
    return (
        text in {".", ".."}
        or "/" in text
        or "\\" in text
        or bool(re.match(r"^[A-Za-z]:", text))
        or text.startswith(("~", "%", "$"))
    )


def _write_intent(name: str, words: list[Token], index: int, path_positions: list[int]) -> str:
    if index > 0:
        previous = words[index - 1].text.strip("\"'`").lower()
        if previous in _NAMED_WRITE:
            return "write"
        if previous in _NAMED_READ:
            return "read"
    if name in _COPY:
        return "write" if path_positions and index == path_positions[-1] else "read"
    if name in _MOVE or name in _WRITE:
        if name in {"sed", "perl"} and not any(
            word.text.startswith("-i") for word in words[1:]
        ):
            return "read"
        return "write"
    if name in _READ:
        return "read"
    # Unknown command: its flags are not modelled, so a path argument could be
    # an output (`--out <path>`). Treat it as a write; that keeps the read-only
    # whitelist read-only even for tools we have never seen.
    return "write"


def _short(text: str, limit: int = 100) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


__all__ = ["Token", "analyze", "kill_command_present", "kill_pids"]
