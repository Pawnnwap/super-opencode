"""Install/remove the global Python edit gate as a Codex lifecycle hook.

The hook is a ``[[hooks.PostToolUse]]`` entry that runs
``vulnerability/py_edit_gate.py`` after every file edit, so every Codex session
on this machine compile-checks and lints Python the agent just wrote, and
safe-fixes imports/formatting in place.

Same idempotence contract as ``ensure_codehelp_codex_mcp``: inspect, change
only what is missing, leave every unrelated config key alone, report via
callbacks, return bool.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import re
import shutil
import sys
from pathlib import Path
from typing import Callable

import tomllib

GATE_SCRIPT_NAME = "py_edit_gate.py"
HOOK_MARK = "py_edit_gate.py"
HOOK_TABLE = "[[hooks.PostToolUse]]"
RUST_GATE_SCRIPT_NAME = "rs_edit_gate.py"
RUST_HOOK_MARK = "rs_edit_gate.py"
WINDOWS_GATE_SCRIPT_NAME = "win_edit_gate.py"
WINDOWS_HOOK_MARK = "win_edit_gate.py"
STOP_HOOK_TABLE = "[[hooks.Stop]]"
STOP_HOOK_TIMEOUT_SECONDS = 60
MATCHER = "apply_patch|Edit|Write|MultiEdit|edit_file|write_file"
HOOK_TIMEOUT_SECONDS = 25

_TABLE_HEADER_RE = re.compile(r"^\s*\[")
_TRUST_HINT = (
    "Accept the hook-trust prompt on the next interactive `codex` launch; the "
    "hash is stored in [hooks.state] keyed by that config path, so one "
    "acceptance covers the CLI, `codex exec`, and the Desktop app."
)


def _hook_block(command: str, timeout: int = HOOK_TIMEOUT_SECONDS) -> list[str]:
    # TOML literal strings (single quotes) keep Windows backslash paths intact.
    return [
        "",
        HOOK_TABLE,
        f'matcher = "{MATCHER}"',
        "hooks = [",
        f"  {{ type = \"command\", command = '{command}', timeout = {timeout} }},",
        "]",
    ]


def _stop_hook_block(command: str, timeout: int = STOP_HOOK_TIMEOUT_SECONDS) -> list[str]:
    # No matcher: the Stop event has no tool to match on.
    return [
        "",
        STOP_HOOK_TABLE,
        "hooks = [",
        f"  {{ type = \"command\", command = '{command}', timeout = {timeout} }},",
        "]",
    ]


def _find_existing_block(
    lines: list[str], mark: str = HOOK_MARK, table: str = HOOK_TABLE
) -> tuple[int, int] | None:
    """Start/end indices of the gate's PostToolUse block, blanks included.

    The separator blank lines belong to the block, so a reinstall compares
    apples to apples and cannot grow the file by one line every run.
    """
    index = 0
    while index < len(lines):
        if lines[index].strip() == table:
            end = index + 1
            while end < len(lines) and not _TABLE_HEADER_RE.match(lines[end]):
                end += 1
            if any(mark in line for line in lines[index:end]):
                start = index
                while start > 0 and not lines[start - 1].strip():
                    start -= 1
                return start, end
            index = end
            continue
        index += 1
    return None


def _validated(config: Path) -> bool:
    try:
        tomllib.loads(config.read_text(encoding="utf-8"))
        return True
    except Exception:
        return False


def install_gate(
    codex_home: Path,
    source_script: Path,
    *,
    interpreter: str | None = None,
    allow_autofix: bool = True,
    allow_format: bool = False,
    shared_script: Path | None = None,
    on_info: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> bool:
    """Copy the gate script and register the hook; no-op when already present."""
    say = on_info or (lambda _msg: None)
    warn = on_warning or (lambda _msg: None)

    if not source_script.is_file():
        warn(f"Gate script missing: {source_script}")
        return False

    home = Path(codex_home)
    config = home / "config.toml"
    if not config.is_file():
        warn(f"Codex config missing: {config}")
        return False
    if not _validated(config):
        warn(f"Codex config does not parse, refusing to edit: {config}")
        return False

    scripts = home / "scripts"
    logs = home / "logs"
    if shared_script is not None:
        # A secondary CODEX_HOME (e.g. supervised runs) points at the one
        # installed copy, so there is a single script to keep in sync.
        installed = Path(shared_script)
        if not installed.is_file():
            warn(f"Shared gate script missing: {installed}")
            return False
        try:
            logs.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            warn(f"Could not create {logs}: {exc}")
            return False
    else:
        installed = scripts / GATE_SCRIPT_NAME
        try:
            scripts.mkdir(parents=True, exist_ok=True)
            logs.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_script, installed)
        except OSError as exc:
            warn(f"Could not install gate script to {installed}: {exc}")
            return False

    interpreter = interpreter or sys.executable
    command = f"{interpreter} {installed}"
    if not allow_autofix:
        command += " --no-autofix"
    if allow_format:
        command += " --format"
    if "'" in command:
        # TOML literal strings cannot quote an apostrophe; refuse over guessing.
        warn(f"Refusing to embed a path containing an apostrophe: {command}")
        return False

    lines = config.read_text(encoding="utf-8").splitlines()
    existing = _find_existing_block(lines)
    if existing:
        start, end = existing
        block = _hook_block(command)
        if start == 0:
            block = block[1:]  # nothing to separate from at the top of the file
        if lines[start:end] == block:
            say(f"Python edit gate already registered in {config}")
            return True
        lines[start:end] = block
        say(f"Refreshed the existing Python edit gate hook in {config}")
    else:
        lines.extend(_hook_block(command))  # the block carries its own separator
        say(f"Registered the Python edit gate hook in {config}")

    stamp = _dt.date.today().strftime("%Y%m%d")
    backup = config.with_name(f"{config.name}.bak-editgate-{stamp}")
    try:
        if not backup.exists():
            shutil.copyfile(config, backup)
        config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        warn(f"Could not write {config}: {exc}")
        return False

    if not _validated(config):
        shutil.copyfile(backup, config)
        warn(f"Hook block broke {config}; restored {backup.name}")
        return False

    say(f"Config backup: {backup}")
    say(f"Audit log: {logs / 'py_edit_gate.jsonl'}")
    say(_TRUST_HINT)
    return True


def remove_gate(
    codex_home: Path,
    *,
    on_info: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> bool:
    """Drop the hook block (the installed script and logs are kept)."""
    say = on_info or (lambda _msg: None)
    warn = on_warning or (lambda _msg: None)
    config = Path(codex_home) / "config.toml"
    if not config.is_file():
        warn(f"Codex config missing: {config}")
        return False

    lines = config.read_text(encoding="utf-8").splitlines()
    existing = _find_existing_block(lines)
    if not existing:
        say("No Python edit gate hook is registered.")
        return True
    start, end = existing
    del lines[start:end]
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if not _validated(config):
        warn(
            f"Removal produced an unparseable config; restore your .bak-editgate file in {config.parent}"
        )
        return False
    say(f"Removed the Python edit gate hook from {config}")
    return True


def default_codex_home() -> Path:
    import os

    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def install_rust_gate(
    codex_home: Path,
    source_script: Path,
    *,
    interpreter: str | None = None,
    shared_script: Path | None = None,
    on_info: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> bool:
    """Register the Rust gate: PostToolUse (rustfmt parse + cargo check) and
    Stop (clippy). Same idempotence contract as install_gate.
    """
    say = on_info or (lambda _msg: None)
    warn = on_warning or (lambda _msg: None)
    if not source_script.is_file():
        warn(f"Rust gate script missing: {source_script}")
        return False

    home = Path(codex_home)
    config = home / "config.toml"
    if not config.is_file():
        warn(f"Codex config missing: {config}")
        return False
    if not _validated(config):
        warn(f"Codex config does not parse, refusing to edit: {config}")
        return False

    if shared_script is not None:
        installed = Path(shared_script)
        if not installed.is_file():
            warn(f"Shared Rust gate script missing: {installed}")
            return False
    else:
        installed = home / "scripts" / RUST_GATE_SCRIPT_NAME
        try:
            installed.parent.mkdir(parents=True, exist_ok=True)
            (home / "logs").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_script, installed)
        except OSError as exc:
            warn(f"Could not install Rust gate script to {installed}: {exc}")
            return False

    interpreter = interpreter or sys.executable
    base_command = f"{interpreter} {installed}"
    if "'" in base_command:
        warn(f"Refusing to embed a path containing an apostrophe: {base_command}")
        return False

    lines = config.read_text(encoding="utf-8").splitlines()
    wanted = [
        (HOOK_TABLE, RUST_HOOK_MARK, _hook_block(base_command)),
        (STOP_HOOK_TABLE, RUST_HOOK_MARK, _stop_hook_block(f"{base_command} --stop")),
    ]
    changed = False
    for table, mark, block in wanted:
        existing = _find_existing_block(lines, mark=mark, table=table)
        if existing:
            start, end = existing
            payload = block[1:] if start == 0 else block
            if lines[start:end] == payload:
                continue
            lines[start:end] = payload
        else:
            lines.extend(block)
        changed = True

    if not changed:
        say(f"Rust edit gate already registered in {config}")
        return True

    stamp = _dt.date.today().strftime("%Y%m%d")
    backup = config.with_name(f"{config.name}.bak-editgate-{stamp}")
    try:
        if not backup.exists():
            shutil.copyfile(config, backup)
        config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        warn(f"Could not write {config}: {exc}")
        return False
    if not _validated(config):
        shutil.copyfile(backup, config)
        warn(f"Rust hook block broke {config}; restored {backup.name}")
        return False

    say(f"Registered the Rust edit gate (PostToolUse + Stop clippy) in {config}")
    say(f"Config backup: {backup}")
    return True


def remove_rust_gate(
    codex_home: Path,
    *,
    on_info: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> bool:
    say = on_info or (lambda _msg: None)
    warn = on_warning or (lambda _msg: None)
    config = Path(codex_home) / "config.toml"
    if not config.is_file():
        warn(f"Codex config missing: {config}")
        return False
    lines = config.read_text(encoding="utf-8").splitlines()
    removed = 0
    for table in (HOOK_TABLE, STOP_HOOK_TABLE):
        while True:
            existing = _find_existing_block(lines, mark=RUST_HOOK_MARK, table=table)
            if not existing:
                break
            del lines[existing[0] : existing[1]]
            removed += 1
    if not removed:
        say("No Rust edit gate hook is registered.")
        return True
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if not _validated(config):
        warn(f"Removal produced an unparseable config; restore your .bak-editgate file in {config.parent}")
        return False
    say(f"Removed {removed} Rust edit gate hook block(s) from {config}")
    return True




def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--codex-home", default="", help="Defaults to $CODEX_HOME or ~/.codex"
    )
    parser.add_argument("--source", default="", help="Path to py_edit_gate.py")
    parser.add_argument(
        "--no-autofix",
        action="store_true",
        help="Advisory-only gate (never edits files)",
    )
    parser.add_argument("--remove", action="store_true", help="Unregister the hook")
    parser.add_argument(
        "--shared-script",
        default="",
        help="Register an already-installed gate script (secondary CODEX_HOME) instead of copying one",
    )
    parser.add_argument(
        "--format",
        action="store_true",
        help="Python gate: allow `ruff format` on edited files (widens churn; off by default)",
    )
    parser.add_argument(
        "--rust",
        action="store_true",
        help="Register the Rust gate (rustfmt parse + cargo check on edit, clippy on Stop)",
    )
    parser.add_argument(
        "--windows",
        action="store_true",
        help="Register the Windows-script gate (.ps1/.psm1/.psd1/.cmd/.bat)",
    )
    parser.add_argument(
        "--ps-format",
        action="store_true",
        help="Windows gate: allow Invoke-Formatter on edited PowerShell (needs PSScriptAnalyzer)",
    )
    parser.add_argument(
        "--ps-lint",
        action="store_true",
        help="Windows gate: run Invoke-ScriptAnalyzer on clean-parsing PowerShell",
    )
    args = parser.parse_args(argv)

    def say(msg: str) -> None:
        print(msg)

    def warn(msg: str) -> None:
        print(f"WARNING: {msg}")

    home = Path(args.codex_home) if args.codex_home else default_codex_home()
    if args.remove:
        ok = remove_gate(home, on_info=say, on_warning=warn)
        ok = remove_rust_gate(home, on_info=say, on_warning=warn) and ok
        ok = remove_windows_gate(home, on_info=say, on_warning=warn) and ok
        return 0 if ok else 1

    shared = Path(args.shared_script) if args.shared_script else None
    root = Path(__file__).resolve().parents[2] / "vulnerability"
    if args.windows:
        return (
            0
            if install_windows_gate(
                home,
                root / WINDOWS_GATE_SCRIPT_NAME,
                shared_script=shared,
                allow_format=args.ps_format,
                allow_lint=args.ps_lint,
                on_info=say,
                on_warning=warn,
            )
            else 1
        )
    if args.rust:
        return (
            0
            if install_rust_gate(
                home,
                root / RUST_GATE_SCRIPT_NAME,
                shared_script=shared,
                on_info=say,
                on_warning=warn,
            )
            else 1
        )

    source = Path(args.source) if args.source else root / GATE_SCRIPT_NAME
    return (
        0
        if install_gate(
            home,
            source,
            allow_autofix=not args.no_autofix,
            allow_format=args.format,
            shared_script=shared,
            on_info=say,
            on_warning=warn,
        )
        else 1
    )


def install_windows_gate(
    codex_home: Path,
    source_script: Path,
    *,
    interpreter: str | None = None,
    shared_script: Path | None = None,
    allow_format: bool = False,
    allow_lint: bool = False,
    on_info: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> bool:
    """Register the Windows-script gate on PostToolUse (PowerShell's own parser
    plus batch encoding/control-flow checks). No Stop hook: nothing runs at turn end.
    """
    say = on_info or (lambda _msg: None)
    warn = on_warning or (lambda _msg: None)
    if not source_script.is_file():
        warn(f"Windows gate script missing: {source_script}")
        return False

    home = Path(codex_home)
    config = home / "config.toml"
    if not config.is_file():
        warn(f"Codex config missing: {config}")
        return False
    if not _validated(config):
        warn(f"Codex config does not parse, refusing to edit: {config}")
        return False

    if shared_script is not None:
        installed = Path(shared_script)
        if not installed.is_file():
            warn(f"Shared Windows gate script missing: {installed}")
            return False
    else:
        installed = home / "scripts" / WINDOWS_GATE_SCRIPT_NAME
        try:
            installed.parent.mkdir(parents=True, exist_ok=True)
            (home / "logs").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source_script, installed)
        except OSError as exc:
            warn(f"Could not install Windows gate script to {installed}: {exc}")
            return False

    interpreter = interpreter or sys.executable
    command = f"{interpreter} {installed}"
    if allow_format:
        command += " --format"
    if allow_lint:
        command += " --lint"
    if "'" in command:
        warn(f"Refusing to embed a path containing an apostrophe: {command}")
        return False

    lines = config.read_text(encoding="utf-8").splitlines()
    block = _hook_block(command)
    existing = _find_existing_block(lines, mark=WINDOWS_HOOK_MARK, table=HOOK_TABLE)
    if existing:
        start, end = existing
        payload = block[1:] if start == 0 else block
        if lines[start:end] == payload:
            say(f"Windows edit gate already registered in {config}")
            return True
        lines[start:end] = payload
        say(f"Refreshed the existing Windows edit gate hook in {config}")
    else:
        lines.extend(block)
        say(f"Registered the Windows edit gate hook in {config}")

    stamp = _dt.date.today().strftime("%Y%m%d")
    backup = config.with_name(f"{config.name}.bak-editgate-{stamp}")
    try:
        if not backup.exists():
            shutil.copyfile(config, backup)
        config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        warn(f"Could not write {config}: {exc}")
        return False
    if not _validated(config):
        shutil.copyfile(backup, config)
        warn(f"Windows hook block broke {config}; restored {backup.name}")
        return False

    say(f"Config backup: {backup}")
    say(_TRUST_HINT)
    return True


def remove_windows_gate(
    codex_home: Path,
    *,
    on_info: Callable[[str], None] | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> bool:
    say = on_info or (lambda _msg: None)
    warn = on_warning or (lambda _msg: None)
    config = Path(codex_home) / "config.toml"
    if not config.is_file():
        warn(f"Codex config missing: {config}")
        return False
    lines = config.read_text(encoding="utf-8").splitlines()
    removed = 0
    while True:
        existing = _find_existing_block(lines, mark=WINDOWS_HOOK_MARK, table=HOOK_TABLE)
        if not existing:
            break
        del lines[existing[0] : existing[1]]
        removed += 1
    if not removed:
        say("No Windows edit gate hook is registered.")
        return True
    config.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if not _validated(config):
        warn(f"Removal produced an unparseable config; restore your .bak-editgate file in {config.parent}")
        return False
    say(f"Removed {removed} Windows edit gate hook block(s) from {config}")
    return True


if __name__ == "__main__":
    raise SystemExit(main())
