"""Deterministic filesystem access policy (whitelist + writable cwd).

Scope
-----
* The run workspace (``config.workspace``, the agent's cwd) is fully
  read/write.
* Every whitelist entry is **read-only**, always. A write attempt there is
  denied even if the entry was supplied with a writable intent.
* Every other path is denied - read and write - no matter what the execution
  backend's own permission defaults say.

The policy is deliberately independent of any agent CLI: it is computed once
per run and then serialized into each backend's native gate (opencode
permissions/plugin, pi extension, codex hook). Those gates call the same
normalization rules implemented here (the JS/TS gates mirror
:func:`is_within`), and the supervisor uses this module for prompt text,
whitelist validation, and AI-suggestion filtering.

Path handling is the security-critical part. On Windows a "prefix" check that
compares strings is wrong in several ways, all of which are covered here:

* ``C:\\work`` must not authorize ``C:\\work-evil``;
* case differences and ``/`` vs ``\\`` are irrelevant;
* ``..`` segments, ``.`` segments, and trailing dots/spaces are normalized;
* symlinks and NTFS junctions are resolved with ``os.path.realpath`` so a
  link inside the workspace cannot smuggle a write outside;
* UNC paths, device paths (``\\\\?\\``, ``\\\\.\\``), drive-relative paths
  (``C:foo``), and DOS device names (``CON``, ``NUL``) are rejected;
* forward-slash MSYS paths (``/c/Users/...``) are normalized to Windows form.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal

AccessKind = Literal["read", "write"]

_WINDOWS = os.name == "nt"

# Directories that must never be offered as a whitelist entry: the whole drive,
# the OS tree, and the user-profile root are not "reference material", and
# allowing them would silently restore full-disk access.
_SYSTEM_ROOT_NAMES: frozenset[str] = frozenset({
    "windows",
    "program files",
    "program files (x86)",
    "programdata",
    "system volume information",
    "$recycle.bin",
    "recovery",
    "perflogs",
})

_DEVICE_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    "com1",
    "com2",
    "com3",
    "com4",
    "com5",
    "com6",
    "com7",
    "com8",
    "com9",
    "lpt1",
    "lpt2",
    "lpt3",
    "lpt4",
    "lpt5",
    "lpt6",
    "lpt7",
    "lpt8",
    "lpt9",
}

# Absolute-path shapes worth extracting from a shell command.
_ABS_WIN_RE = re.compile(r"""(?<![\w])([A-Za-z]:[\\/][^\s"'|;&<>\n]*)""")
_ABS_UNC_RE = re.compile(r"""(\\\\[^\s"'|;&<>\n]+)""")
_ABS_POSIX_RE = re.compile(r"""(?<![\w:/.-])(/(?!/)[^\s"'|;&<>\n]+)""")

# Write intent markers. The scan is conservative: when a path follows one of
# these, it is treated as a write (and therefore denied outside the cwd).
_WRITE_VERBS = {
    "rm",
    "rmdir",
    "del",
    "erase",
    "mv",
    "move",
    "cp",
    "copy",
    "xcopy",
    "robocopy",
    "mkdir",
    "md",
    "touch",
    "tee",
    "chmod",
    "chown",
    "attrib",
    "icacls",
    "takeown",
    "ln",
    "mklink",
    "truncate",
    "dd",
    "install",
    "unlink",
    "rename",
    "ren",
    "sed",
    "perl",
    "remove-item",
    "move-item",
    "copy-item",
    "new-item",
    "set-content",
    "add-content",
    "out-file",
    "clear-content",
    "rename-item",
    "expand-archive",
    "compress-archive",
}

# Windows shell builtins whose arguments are paths, for `>`-less writes.
_WRITE_REDIRECT_RE = re.compile(r"(?<![0-9])(?:>>?|1>>?|2>>?)\s*$")

_QUOTE_TRIM = "\"'` \t\r\n,;:)]}"


class AccessPolicyError(ValueError):
    """Raised when a whitelist entry cannot be accepted."""


@dataclass(frozen=True)
class PolicyDecision:
    """Outcome of one path check."""

    allowed: bool
    path: str
    normalized: str
    access: AccessKind
    reason: str
    matched_root: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "path": self.path,
            "normalized": self.normalized,
            "access": self.access,
            "reason": self.reason,
            "matched_root": self.matched_root,
        }


@dataclass(frozen=True)
class PolicyViolation:
    """A path denied by :meth:`AccessPolicy.scan_command`."""

    path: str
    access: AccessKind
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "access": self.access, "reason": self.reason}

    def headline(self) -> str:
        return f"{self.access} denied: {self.path} ({self.reason})"


@dataclass(frozen=True)
class AccessPolicy:
    """Writable cwd + read-only whitelist + deny-everything-else.

    ``whitelist`` entries keep the order the operator supplied (stable for UI
    chips and prompt text). Duplicates and nested children are collapsed:
    keeping the parent is sufficient and makes the generated native rules
    smaller.
    """

    workspace: Path
    whitelist: tuple[Path, ...] = ()
    enabled: bool = True
    # Reject commands whose inline code cannot be statically verified
    # (`python -c`, `node -e`, `bash -c`, ...). Off by default: coding agents
    # legitimately use one-liners, and every literal path in them is still
    # scanned. Turn on for maximally strict runs.
    strict_dynamic: bool = False
    extra_notes: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        resolved_ws = _resolve(Path(self.workspace))
        object.__setattr__(self, "workspace", resolved_ws)

        cleaned: list[Path] = []
        seen: set[str] = set()
        for raw in self.whitelist:
            try:
                entry = _resolve(Path(raw))
            except (OSError, ValueError):
                continue
            if _key(entry) in seen or is_within(entry, resolved_ws):
                continue
            # A hand-edited settings file must not be able to widen the policy
            # to a drive root or an OS tree.
            if _is_dangerous_root(entry):
                continue
            # Nested entries add nothing: the parent already covers them.
            if any(is_within(entry, parent) for parent in cleaned):
                continue
            cleaned = [p for p in cleaned if not is_within(p, entry)]
            cleaned.append(entry)
            seen.add(_key(entry))
        object.__setattr__(self, "whitelist", tuple(cleaned))

    # -- construction ---------------------------------------------------- #

    @classmethod
    def from_config(cls, config: Any) -> AccessPolicy:
        """Build from a ``SupervisorConfig`` (or any duck-typed object)."""
        raw = getattr(config, "access_whitelist", ()) or ()
        if isinstance(raw, str):
            raw = [line for line in raw.splitlines() if line.strip()]
        return cls(
            workspace=Path(getattr(config, "workspace")),
            whitelist=tuple(Path(str(item)) for item in raw if str(item).strip()),
            enabled=bool(getattr(config, "access_policy_enabled", True)),
            strict_dynamic=bool(getattr(config, "access_policy_strict", False)),
        )

    # -- core check ------------------------------------------------------ #

    def check(self, raw_path: str | Path | None, access: AccessKind = "read") -> PolicyDecision:
        """Decide whether *raw_path* may be accessed.

        Relative paths resolve against :attr:`workspace`, which is also the
        only writable root. A missing/blank path is denied.
        """
        if not self.enabled:
            text = str(raw_path or "")
            return PolicyDecision(True, text, text, access, "policy disabled")

        text = str(raw_path or "").strip()
        if not text:
            return PolicyDecision(False, text, "", access, "empty path")

        unsafe = _unsafe_reason(text)
        if unsafe:
            return PolicyDecision(False, text, text, access, unsafe)

        try:
            resolved = _resolve(text, base=self.workspace)
        except (OSError, ValueError) as exc:
            return PolicyDecision(False, text, text, access, f"unresolvable path: {exc}")

        if is_within(resolved, self.workspace):
            return PolicyDecision(
                True, text, str(resolved), access, "inside workspace", str(self.workspace)
            )

        for entry in self.whitelist:
            if is_within(resolved, entry):
                if access == "write":
                    return PolicyDecision(
                        False,
                        text,
                        str(resolved),
                        access,
                        "whitelist directories are read-only",
                        str(entry),
                    )
                return PolicyDecision(
                    True, text, str(resolved), access, "read-only whitelist", str(entry)
                )

        return PolicyDecision(
            False,
            text,
            str(resolved),
            access,
            "outside workspace and whitelist",
        )

    def check_read(self, path: str | Path) -> PolicyDecision:
        return self.check(path, "read")

    def check_write(self, path: str | Path) -> PolicyDecision:
        return self.check(path, "write")

    def is_readable(self, path: str | Path) -> bool:
        return self.check_read(path).allowed

    def is_writable(self, path: str | Path) -> bool:
        return self.check_write(path).allowed

    # -- shell command scanning ------------------------------------------ #

    def scan_command(self, command: str, cwd: str | Path | None = None) -> list[PolicyViolation]:
        """Statically scan a shell command for out-of-policy paths.

        This is a *gate*, not a sandbox. It extracts absolute and
        home-relative paths, decides read vs. write intent from redirections
        and common write verbs, and denies anything outside the policy. Inline
        interpreter code is rejected only when ``strict_dynamic`` is set;
        otherwise its literal paths are still scanned.
        """
        if not self.enabled or not command:
            return []

        base = Path(cwd) if cwd else self.workspace
        violations: list[PolicyViolation] = []
        seen: set[tuple[str, str]] = set()

        def add(path_text: str, access: AccessKind) -> None:
            decision = self.check(path_text, access)
            if decision.allowed:
                return
            key = (decision.normalized or path_text, access)
            if key in seen:
                return
            seen.add(key)
            violations.append(PolicyViolation(path_text, access, decision.reason))

        if self.strict_dynamic and _has_inline_code(command):
            violations.append(
                PolicyViolation(
                    _truncate(command, 120),
                    "write",
                    "inline interpreter code cannot be statically verified",
                )
            )
            return violations

        for match in _ABS_WIN_RE.finditer(command):
            raw = _trim(match.group(1))
            if raw:
                add(raw, "write" if _write_context(command, match.start()) else "read")

        for match in _ABS_UNC_RE.finditer(command):
            raw = _trim(match.group(1))
            if raw:
                add(raw, "read")  # UNC is denied outright

        for match in _ABS_POSIX_RE.finditer(command):
            raw = _trim(match.group(1))
            if not _looks_like_posix_path(raw, base):
                continue
            add(_posix_to_local(raw), "write" if _write_context(command, match.start()) else "read")

        for match in re.finditer(r"(?:~|%USERPROFILE%|\$HOME|\$\{HOME\})([\\/][^\s\"'|;&<>\n]*)", command):
            raw = _trim(match.group(0))
            expanded = os.path.expandvars(os.path.expanduser(raw))
            add(expanded, "write" if _write_context(command, match.start()) else "read")

        # Relative path arguments: `cat ../../secret` or `cp x ../out/y` escape
        # the cwd without ever spelling an absolute path. Resolve every
        # path-shaped token against the cwd and check it like any other path.
        for match in re.finditer(r"""[^\s"'|;&<>]+""", command):
            token = _trim(match.group(0))
            if not token or token.startswith(("-", "/", "\\", "%", "$", "~")):
                continue
            if "://" in token:
                continue
            if "=" in token:
                token = token.split("=", 1)[1]
                if not token:
                    continue
            if token in {".", ".."} or "/" in token or "\\" in token or re.match(
                r"^[A-Za-z]:", token,
            ):
                add(token, "write" if _write_context(command, match.start()) else "read")

        for match in re.finditer(r"\bcd\s+(?:/d\s+)?([^\s&|;]+)", command, re.IGNORECASE):
            raw = _trim(match.group(1))
            if raw and raw not in ("-", ".."):
                add(raw, "read")

        return violations

    # -- serialization for the engine gates ------------------------------- #

    def roots(self) -> list[dict[str, str]]:
        entries = [{"path": str(self.workspace), "mode": "rw"}]
        entries.extend({"path": str(entry), "mode": "ro"} for entry in self.whitelist)
        return entries

    def snapshot(self) -> dict[str, Any]:
        """JSON-ready description consumed by the JS/TS gates."""
        return {
            "version": 1,
            "enabled": self.enabled,
            "caseInsensitive": _WINDOWS,
            "workspace": _posix(str(self.workspace)),
            "roots": [
                {"path": _posix(item["path"]), "mode": item["mode"]}
                for item in self.roots()
            ],
            "strictDynamic": self.strict_dynamic,
        }

    # -- prompt text ----------------------------------------------------- #

    def describe(self, limit: int = 8) -> str:
        """Compact HARD-RULE block injected into every agent-facing prompt."""
        if not self.enabled:
            return ""
        lines = [
            "[ACCESS POLICY - HARD RULE, ENFORCED BY THE SUPERVISOR]",
            f"- Read and write: {self.workspace} (the project cwd, full access)",
        ]
        if self.whitelist:
            lines.append("- Read-only reference directories (never modify):")
            for entry in self.whitelist[:limit]:
                lines.append(f"    {entry}")
            if len(self.whitelist) > limit:
                lines.append(f"    ... and {len(self.whitelist) - limit} more")
        else:
            lines.append("- Read-only reference directories: none")
        lines.append(
            "- Every other path is DENIED for both reading and writing. "
            "This overrides any other permission setting; there are no exceptions.",
        )
        if self.strict_dynamic:
            lines.append(
                "- Inline interpreter code (python -c, node -e, bash -c) is denied.",
            )
        lines.extend(f"- {note}" for note in self.extra_notes)
        return "\n".join(lines)

    def prompt_preamble(self) -> str:
        block = self.describe()
        return f"{block}\n\n" if block else ""


# --------------------------------------------------------------------------- #
# Path helpers
# --------------------------------------------------------------------------- #


def _resolve(path: str | Path, base: Path | None = None) -> Path:
    """Expand, absolutize, and fully resolve a path (symlinks + junctions)."""
    text = str(path).strip()
    if not text:
        raise ValueError("empty path")
    if "\x00" in text:
        raise ValueError("NUL byte in path")

    expanded = os.path.expandvars(os.path.expanduser(text))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = Path(base or Path.cwd()) / candidate

    # realpath resolves symlinks/junctions and normalizes the case of existing
    # components on Windows. It does not require the final component to exist.
    real = os.path.realpath(str(candidate))
    # Strip the extended-length prefix that realpath may return; keep the
    # canonical drive form so prefix comparisons behave.
    if real.startswith("\\\\?\\"):
        real = real[4:]
        # \\?\UNC\server\share -> \\server\share
        if real.upper().startswith("UNC\\"):
            real = "\\\\" + real[4:]
    return Path(real)


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def is_within(child: Path | str, root: Path | str) -> bool:
    """True when *child* is *root* or lives under it (no prefix traps)."""
    c = _key(Path(child))
    r = _key(Path(root)).rstrip("\\/")
    if c == r:
        return True
    return c.startswith(r + os.sep)


def _unsafe_reason(raw: str) -> str:
    """Reject path shapes that must never reach the filesystem."""
    if "\x00" in raw:
        return "NUL byte in path"
    if raw.startswith("\\\\?\\") or raw.startswith("\\\\.\\"):
        return "Windows device paths are not allowed"
    if raw.startswith("\\\\"):
        return "UNC paths are not allowed"
    if re.match(r"^[A-Za-z]:[^\\/]", raw):
        return "drive-relative paths are ambiguous"
    name = raw.rstrip(" .").split("\\")[-1].split("/")[-1].split(":")[0].strip().lower()
    if name in _DEVICE_NAMES:
        return f"DOS device name ({name}) is not allowed"
    return ""


def _write_context(command: str, index: int) -> bool:
    """Heuristic: does the path at *index* sit in a write position?"""
    head = command[:index]
    if _WRITE_REDIRECT_RE.search(head):
        return True
    # Look at the current command segment (after the last separator).
    segment = re.split(r"[|;&\n]", head)[-1]
    tokens = [t for t in re.split(r"\s+", segment.strip()) if t]
    if not tokens:
        return False
    lowered = [t.lower().strip("\"'") for t in tokens]
    if any(t in _WRITE_VERBS for t in lowered):
        return True
    # git subcommands that materialize files (clone/checkout/...) are writes;
    # read-only inspection (status/log/diff) is not.
    if re.search(
        r"\bgit\s+(clone|init|worktree|mv|rm|checkout|reset|clean|apply|am|pull|fetch)\b",
        segment,
        re.IGNORECASE,
    ):
        return True
    # `git ...` is not treated as a write unless a write verb is present.
    for token in lowered:
        if token.endswith((">", ">>")):
            return True
    return False


def _trim(raw: str) -> str:
    return raw.rstrip(_QUOTE_TRIM)


def _looks_like_posix_path(raw: str, base: Path) -> bool:
    if not raw.startswith("/") or raw.startswith("//"):
        return False
    if raw in ("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/zero"):
        return False
    # CLI flags such as `--opt/foo` are not paths.
    if raw.count("/") < 1:
        return False
    # `/usr/bin/env`, `/bin/bash` etc. are legitimate paths but outside the
    # policy on Windows; scanning them is harmless (they are denied only if
    # they resolve locally). Skip well-known POSIX system roots on Windows so
    # common shell grammar does not produce noise.
    if _WINDOWS:
        first = raw.split("/")[1].lower()
        if first in {"usr", "bin", "etc", "opt", "var", "proc", "sys", "dev"}:
            return False
    return True


def _posix_to_local(raw: str) -> str:
    """Map ``/c/Users/x`` to ``C:/Users/x`` on Windows; leave others alone."""
    if _WINDOWS:
        match = re.match(r"^/([A-Za-z])/(.*)$", raw)
        if match:
            return f"{match.group(1).upper()}:/{match.group(2)}"
    return raw


def _has_inline_code(command: str) -> bool:
    patterns = (
        r"\bpython[0-9.]*\s+-c\b",
        r"\bpy\s+-c\b",
        r"\bnode\s+(-e|--eval)\b",
        r"\b(bash|sh|zsh)\s+-c\b",
        r"\bpwsh(\.exe)?\b.*\s-(c|command)\b",
        r"\bpowershell(\.exe)?\b.*\s-(c|command)\b",
        r"\bcmd(\.exe)?\s+/c\b",
    )
    return any(re.search(pattern, command, re.IGNORECASE) for pattern in patterns)


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# Whitelist validation (shared by the UI and the AI advisor)
# --------------------------------------------------------------------------- #


def validate_whitelist_entry(
    raw: str | Path,
    workspace: str | Path,
) -> tuple[Path | None, str]:
    """Accept only existing absolute directories outside the cwd.

    Returns ``(resolved, "")`` on success or ``(None, reason)`` on rejection.
    System roots, drive roots, the user-profile root, UNC/device paths and
    wildcard characters are refused: each would silently widen the policy far
    beyond "reference material".
    """
    text = str(raw or "").strip()
    if not text:
        return None, "empty path"

    unsafe = _unsafe_reason(text)
    if unsafe:
        return None, unsafe
    if "*" in text or "?" in text:
        return None, "wildcards are not allowed in whitelist paths"
    if "\x00" in text:
        return None, "NUL byte in path"

    expanded = os.path.expandvars(os.path.expanduser(text))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        return None, "whitelist paths must be absolute"

    try:
        resolved = _resolve(candidate)
    except (OSError, ValueError) as exc:
        return None, f"unresolvable path: {exc}"

    if not resolved.exists():
        return None, "path does not exist"
    if not resolved.is_dir():
        return None, "path is not a directory"

    ws = _resolve(Path(workspace))
    if is_within(resolved, ws):
        return None, "already inside the workspace (cwd has full access)"

    if resolved.parent == resolved:
        return None, "drive/filesystem roots are not allowed"
    if resolved == Path.home().resolve():
        return None, "the user-profile root is not allowed"
    # Only the first segment below the anchor identifies an OS tree:
    # `C://Windows`, `C://Program Files\...`. Checking later components would
    # reject legitimate projects that merely contain a folder named "windows".
    first = resolved.parts[1].lower() if len(resolved.parts) > 1 else ""
    if first in _SYSTEM_ROOT_NAMES or resolved.parent == resolved.anchor:
        return None, "system directories are not allowed"
    if _WINDOWS and re.match(r"^[A-Za-z]:[\\/](Windows|Program Files.*)$", str(resolved), re.IGNORECASE):
        return None, "system directories are not allowed"

    return resolved, ""


def build_policy(
    workspace: str | Path,
    whitelist: Iterable[str | Path] = (),
    *,
    enabled: bool = True,
    strict_dynamic: bool = False,
) -> AccessPolicy:
    """Convenience constructor used by the UI and tests."""
    return AccessPolicy(
        workspace=Path(workspace),
        whitelist=tuple(Path(str(item)) for item in whitelist),
        enabled=enabled,
        strict_dynamic=strict_dynamic,
    )


def _is_dangerous_root(path: Path) -> bool:
    """True for drive/filesystem roots and OS trees (never whitelistable)."""
    if path.parent == path:
        return True
    parts = path.parts
    first = parts[1].lower() if len(parts) > 1 else ""
    return first in _SYSTEM_ROOT_NAMES


def _posix(path: str) -> str:
    return path.replace("\\", "/")


__all__ = [
    "AccessKind",
    "AccessPolicy",
    "AccessPolicyError",
    "PolicyDecision",
    "PolicyViolation",
    "build_policy",
    "is_within",
    "validate_whitelist_entry",
]
