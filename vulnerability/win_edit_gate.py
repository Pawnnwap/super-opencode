"""win_edit_gate.py
Codex PostToolUse gate for Windows script edits (global, standalone).

Third sibling of py_edit_gate.py / rs_edit_gate.py, registered on the same
[[hooks.PostToolUse]] and ignoring payloads with no .ps1/.psm1/.psd1/.cmd/.bat
files. Stdlib only, fail-open (any internal error => exit 0, silent).

What is actually checkable, per language:

  PowerShell  real syntax check via PowerShell's own parser
              ([Language.Parser]::ParseFile). It parses, it never executes.
              pwsh is preferred, powershell.exe is the fallback.
  Batch       there is no cmd parser, so: encoding traps and the two classic
              silent bugs - invoking a .bat without `call` (control never
              returns to the caller) and using !var! without
              `setlocal EnableDelayedExpansion` (expands to nothing).

Encoding is checked for both because it is the failure class that bites hardest
on this box: PS 5.1 reads BOM-less UTF-8 as ANSI (mojibake), cmd.exe cannot read
UTF-16 at all, and cmd misbehaves on LF-only files.

Nothing is rewritten by default. `--format` runs PSScriptAnalyzer's
Invoke-Formatter on PowerShell files (churn-guarded, reverted if the file stops
parsing); `--lint` runs Invoke-ScriptAnalyzer, throttled per file.

Kill switches: WIN_EDIT_GATE=0, .edit-gate-disable, --no-autofix.
Audit log: ~/.codex/logs/win_edit_gate.jsonl (override dir with
WIN_EDIT_GATE_LOG_DIR).
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HARD_CAP_SECONDS = 18.0
PS_STARTUP_TIMEOUT = 12.0
LINT_TIMEOUT = 25.0
LINT_INTERVAL = 300.0
MAX_FILE_BYTES = 1_000_000
ADVISORY_MAX_CHARS = 1200
MAX_ITEMS_PER_SECTION = 6

MAX_FIX_LINES = 20
MAX_FIX_FRACTION = 0.10

PS_SUFFIXES = {".ps1", ".psm1", ".psd1"}
BATCH_SUFFIXES = {".cmd", ".bat"}
WATCHED_SUFFIXES = PS_SUFFIXES | BATCH_SUFFIXES

POWERShell_FALLBACK = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

_IGNORE_DIR_NAMES = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "target",
    "build", "dist", ".idea", ".vscode",
}

_PATCH_FILE_RE = re.compile(r"^\*\*\*\s+(?:Add|Update|Delete)\s+File:\s*(.+?)\s*$", re.MULTILINE)
_BATCH_LEAD_RE = re.compile(
    # A statement that runs a script: optional @echo-suppressor, optional `call`,
    # then a bare/quoted .bat|.cmd path. Anchored at the statement start, so
    # `echo run.bat`, `if exist run.bat` and `set X=run.bat` cannot match.
    r"""^\s*@?\s*(call\s+)?['"]?(?P<target>[^\s'"]+\.(?:bat|cmd))\b""",
    re.IGNORECASE,
)
_DELAYED_EXPANSION_RE = re.compile(r"![A-Za-z_][A-Za-z0-9_]*!")


# ── Logging / state ─────────────────────────────────────────────────────────
def _log_dir() -> Path:
    override = os.environ.get("WIN_EDIT_GATE_LOG_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".codex" / "logs"


def _rotate(path: Path, limit: int = 1_000_000) -> None:
    try:
        if path.exists() and path.stat().st_size > limit:
            older = path.with_suffix(path.suffix + ".1")
            if older.exists():
                older.unlink()
            path.rename(older)
    except OSError:
        pass


def audit(record: dict) -> None:
    try:
        folder = _log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        log = folder / "win_edit_gate.jsonl"
        _rotate(log)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _state_path() -> Path:
    return _log_dir() / "win_edit_gate.state.json"


def _load_state() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        folder = _log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = _state_path()
        _rotate(path, 400_000)
        path.write_text(json.dumps(dict(list(state.items())[-600:])), encoding="utf-8")
    except Exception:
        pass


def _now() -> float:
    return time.monotonic()


def _run(cmd: list, timeout: float, cwd: Path | None = None):
    try:
        return subprocess.run(
            [str(c) for c in cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            cwd=str(cwd) if cwd else None,
        )
    except Exception:
        return None


def shell_exe() -> str | None:
    for name in ("pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            return found
    return POWERShell_FALLBACK if Path(POWERShell_FALLBACK).is_file() else None


def autofix_disabled_here(path: Path) -> bool:
    try:
        for parent in [path.parent, *list(path.parents)[:8]]:
            if (parent / ".edit-gate-disable").exists():
                return True
            if (parent / ".git").exists():
                break
    except OSError:
        pass
    return False


# ── Payload ─────────────────────────────────────────────────────────────────
def parse_patch_paths(text: str) -> list[str]:
    return [m.group(1) for m in _PATCH_FILE_RE.finditer(text) if m.group(1).strip()]


def extract_targets(payload: dict, cwd: Path) -> list[Path]:
    raw: list[str] = []

    def scan(value) -> None:
        if isinstance(value, str):
            raw.extend(parse_patch_paths(value))
        elif isinstance(value, dict):
            for key, sub in value.items():
                if key in ("file_path", "path", "filePath", "target_file"):
                    if isinstance(sub, str):
                        raw.append(sub)
                else:
                    scan(sub)
        elif isinstance(value, list):
            for item in value:
                scan(item)

    scan(payload.get("tool_input"))
    out: list[Path] = []
    seen: set[str] = set()
    for item in raw:
        cleaned = item.strip().strip('"').strip("'")
        if Path(cleaned).suffix.lower() not in WATCHED_SUFFIXES:
            continue
        candidate = Path(cleaned)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        try:
            candidate = candidate.resolve()
        except OSError:
            continue
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def skip_reason(path: Path) -> str:
    try:
        if not path.is_file():
            return "not a file"
        if {p.lower() for p in path.parts} & _IGNORE_DIR_NAMES:
            return "ignored dir"
        if path.stat().st_size > MAX_FILE_BYTES:
            return "too large"
    except OSError as exc:
        return f"oserror: {exc}"
    return ""


# ── Pure checks (no subprocess, unit-testable) ──────────────────────────────
def encoding_findings(blob: bytes, path: Path) -> list[str]:
    """Encoding traps that make a script parse but not run."""
    name = path.name
    findings: list[str] = []
    if blob.startswith(b"\xff\xfe") or blob.startswith(b"\xfe\xff"):
        if path.suffix.lower() in BATCH_SUFFIXES:
            findings.append(f"{name}: UTF-16 file, cmd.exe cannot read it - save as ANSI/UTF-8 (no BOM)")
        return findings
    if path.suffix.lower() in BATCH_SUFFIXES:
        if b"\xef\xbb\xbf" in blob[:8]:
            findings.append(f"{name}: UTF-8 BOM confuses cmd.exe (first line is not recognised)")
        if b"\r\n" not in blob and b"\n" in blob:
            findings.append(f"{name}: LF-only line endings, cmd.exe mis-parses blocks - convert to CRLF")
        return findings
    if b"\xef\xbb\xbf" not in blob and any(byte > 127 for byte in blob):
        findings.append(
            f"{name}: non-ASCII text in BOM-less UTF-8 - PowerShell 5.1 reads it as ANSI and garbles it"
        )
    return findings


def batch_findings(text: str, path: Path) -> list[str]:
    """The two batch bugs that produce no error message, only wrong behaviour."""
    findings: list[str] = []
    lowered_all = text.lower()
    has_delayed = "setlocal enabledelayedexpansion" in lowered_all
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered.startswith(("rem ", "rem\t", "::", "@rem")):
            continue
        match = _BATCH_LEAD_RE.match(stripped)
        if match and not match.group(1):
            findings.append(
                f"{path.name}:{number} runs {match.group('target')} without `call` - control transfers and this script never resumes"
            )
        if not has_delayed and _DELAYED_EXPANSION_RE.search(stripped):
            findings.append(
                f"{path.name}:{number} uses !var! syntax without `setlocal EnableDelayedExpansion` - it expands to nothing"
            )
    return findings


def parse_ps_parser_output(stdout: str) -> list[str]:
    """`line:col message` lines emitted by the PowerShell parse probe."""
    findings: list[str] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line or line.upper().startswith(("ERRORS=", "PARSE-OK")):
            continue
        findings.append(line)
    return findings


def classify_analyzer_output(stdout: str) -> list[str]:
    findings: list[str] = []
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line or line.upper().startswith("LINT-DONE"):
            continue
        findings.append(line)
    return findings


# ── PowerShell probes ───────────────────────────────────────────────────────
def _ps_command(script: str) -> list[str]:
    exe = shell_exe()
    # powershell.exe writes console text in the OEM codepage (CP936 here), which
    # arrives as mojibake when decoded as UTF-8. Force UTF-8 on the child console.
    preamble = "try{[Console]::OutputEncoding=[System.Text.Encoding]::UTF8}catch{};"
    return [
        exe,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        preamble + script,
    ]


def _quote_ps_path(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def powershell_parse_findings(path: Path) -> tuple[list[str], str]:
    """(syntax errors, note). Parses the file; never executes it."""
    if shell_exe() is None:
        return ([], "no PowerShell available to parse this file")
    script = (
        "$t=@();$e=@();"
        f"[void][System.Management.Automation.Language.Parser]::ParseFile({_quote_ps_path(path)},[ref]$t,[ref]$e);"
        "if($e.Count -gt 0){$e|ForEach-Object{\"{0}:{1} {2}\" -f $_.Extent.StartLineNumber,$_.Extent.StartColumnNumber,$_.Message}};"
        "\"ERRORS=$($e.Count)\""
    )
    proc = _run(_ps_command(script), PS_STARTUP_TIMEOUT)
    if proc is None:
        return ([], "PowerShell parse timed out")
    if proc.returncode != 0 and not (proc.stdout or "").strip():
        return ([], f"PowerShell parse failed to run: {(proc.stderr or '').strip()[:120]}")
    return (parse_ps_parser_output(proc.stdout), "")


def _churn_budget(before: bytes) -> int:
    lines = before.decode("utf-8", "replace").count("\n") + 1
    return max(MAX_FIX_LINES, int(lines * MAX_FIX_FRACTION))


def _changed_line_count(before: bytes, after: bytes) -> int:
    a = before.decode("utf-8", "replace").splitlines()
    b = after.decode("utf-8", "replace").splitlines()
    total = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            total += max(i2 - i1, j2 - j1)
    return total


def format_powershell(path: Path) -> tuple[bool, str]:
    """Invoke-Formatter, reverted unless the result parses and stays inside the churn budget.

    Returns (reformatted, note). The note explains a no-op, e.g. the optional
    PSScriptAnalyzer module being absent on this machine.
    """
    try:
        before = path.read_bytes()
    except OSError:
        return (False, "")
    script = (
        "if(-not (Get-Module -ListAvailable -Name PSScriptAnalyzer)){'FMT-UNAVAILABLE';exit};"
        "Import-Module PSScriptAnalyzer -Verbose:$false;"
        f"$t=Get-Content -Raw -LiteralPath {_quote_ps_path(path)};"
        "try{(Invoke-Formatter -ScriptDefinition $t -Verbose:$false)|Write-Output -NoNewline}catch{}}"
    )
    proc = _run(_ps_command(script), LINT_TIMEOUT)
    if proc is None:
        return (False, "Invoke-Formatter timed out")
    blob = (proc.stdout or "").strip()
    if "FMT-UNAVAILABLE" in blob:
        return (False, "Invoke-Formatter needs PSScriptAnalyzer (Install-Module PSScriptAnalyzer)")
    if not blob:
        return (False, "")
    try:
        path.write_text(proc.stdout, encoding="utf-8-sig" if b"\xef\xbb\xbf" in before else "utf-8", newline="")
    except OSError:
        return (False, "")
    after = path.read_bytes()
    if after == before:
        return (False, "")
    if _changed_line_count(before, after) > _churn_budget(before):
        path.write_bytes(before)
        return (False, "")
    errors, _note = powershell_parse_findings(path)
    if errors:
        path.write_bytes(before)
        return (False, "formatter output stopped parsing - reverted")
    return (True, "")


def lint_powershell(path: Path) -> tuple[list[str], str]:
    script = (
        "if(-not (Get-Module -ListAvailable -Name PSScriptAnalyzer)){'LINT-UNAVAILABLE';exit};"
        "Import-Module PSScriptAnalyzer -Verbose:$false;"
        f"Invoke-ScriptAnalyzer -LiteralPath {_quote_ps_path(path)} -Severity Warning,Error -Verbose:$false"
        "|ForEach-Object{\"{0}:{1} [{2}] {3}\" -f $_.Line,$_.Column,$_.RuleName,$_.Message};"
        "'LINT-DONE'"
    )
    proc = _run(_ps_command(script), LINT_TIMEOUT)
    if proc is None:
        return ([], "PSScriptAnalyzer timed out")
    blob = (proc.stdout or "").strip()
    if "LINT-UNAVAILABLE" in blob:
        return ([], "PSScriptAnalyzer not installed (Install-Module PSScriptAnalyzer)")
    return (classify_analyzer_output(blob), "")


# ── Advisory ────────────────────────────────────────────────────────────────
def compose_advisory(fixed: list[str], syntax: list[str], encoding: list[str], batch: list[str], lint: list[str], notes: list[str]) -> str:
    blocks: list[str] = []
    if fixed:
        blocks.append(
            "[win-edit-gate] AUTO-FIXED: " + "; ".join(fixed)
            + ". RE-READ the file before your next patch and do not resubmit the whole file."
        )

    def add(label: str, items: list[str], limit: int = MAX_ITEMS_PER_SECTION) -> None:
        if not items:
            return
        shown = items[:limit]
        tail = f" (+{len(items) - len(shown)} more)" if len(items) > len(shown) else ""
        blocks.append(f"[win-edit-gate] {label}: " + " | ".join(shown) + tail)

    add("DOES NOT PARSE", syntax, 5)
    add("encoding", encoding, 3)
    add("batch", batch, 4)
    add("lint", lint, 5)
    add("note", notes, 3)
    if not blocks:
        return ""
    if not fixed:
        blocks.append(
            "[win-edit-gate] Re-read the file and repair it with a targeted patch - do not resubmit the whole file."
        )
    text = "\n".join(blocks)
    return text if len(text) <= ADVISORY_MAX_CHARS else text[: ADVISORY_MAX_CHARS - 3] + "..."


def _emit(advisory: str) -> None:
    if not advisory:
        return
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": advisory,
            },
            "systemMessage": advisory.split("\n")[0][:200],
        },
        sys.stdout,
    )
    sys.stdout.write("\n")


def main(argv: list[str]) -> int:
    started = _now()
    if os.environ.get("WIN_EDIT_GATE", "1").strip().lower() in ("0", "off", "false"):
        return 0
    raw = sys.stdin.buffer.read()
    if not raw.strip():
        return 0
    try:
        payload = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0

    cwd = Path(str(payload.get("cwd") or os.getcwd()))
    targets = [p for p in extract_targets(payload, cwd) if not skip_reason(p)]
    if not targets:
        return 0

    deadline = started + HARD_CAP_SECONDS
    allow_fix = "--no-autofix" not in argv and "--format" in argv
    allow_lint = "--lint" in argv
    state = _load_state()

    sections = {"fixed": [], "syntax": [], "encoding": [], "batch": [], "lint": [], "notes": []}
    for path in targets:
        try:
            blob = path.read_bytes()
        except OSError as exc:
            sections["notes"].append(f"{path.name}: unreadable ({exc})")
            continue
        sections["encoding"].extend(encoding_findings(blob, path))

        if path.suffix.lower() in BATCH_SUFFIXES:
            sections["batch"].extend(batch_findings(blob.decode("utf-8", "replace"), path))
            continue

        if _now() >= deadline:
            sections["notes"].append("parse skipped (time cap)")
            break
        errors, note = powershell_parse_findings(path)
        sections["syntax"].extend(errors)
        if note:
            sections["notes"].append(note)
        if not errors and allow_fix and not autofix_disabled_here(path):
            reformatted, fix_note = format_powershell(path)
            if reformatted:
                sections["fixed"].append(f"{path.name} formatted")
            elif fix_note:
                sections["notes"].append(fix_note)
        if allow_lint and not errors and _now() < deadline:
            key = "lint:" + str(path).lower()
            last = state.get(key)
            if isinstance(last, (int, float)) and time.time() - last < LINT_INTERVAL:
                pass
            else:
                findings, lint_note = lint_powershell(path)
                state[key] = time.time()
                sections["lint"].extend(findings)
                if lint_note:
                    sections["notes"].append(lint_note)

    notes = list(sections["notes"])
    if _now() >= deadline:
        notes.append("checks truncated by the hook time cap")
    advisory = compose_advisory(**sections)
    audit(
        {
            "ts": round(time.time(), 2),
            "event": payload.get("hook_event_name"),
            "tool": payload.get("tool_name"),
            "files": [str(p) for p in targets],
            "shell": shell_exe(),
            "fix_allowed": allow_fix,
            "lint_allowed": allow_lint,
            "counts": {key: len(items) for key, items in sections.items()},
            "ms": int((_now() - started) * 1000),
        }
    )
    _save_state(state)
    _emit(advisory)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as exc:  # fail open, always
        audit({"error": f"{type(exc).__name__}: {exc}"})
        sys.exit(0)
