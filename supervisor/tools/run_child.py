"""The agent's only sanctioned way to start a MATLAB / Python / cargo child.

Every child started here is registered in the run's process ledger and
contained in the run's job object, so it can be terminated precisely later —
by this run only. The shell gate denies bare ``matlab`` and global-kill
commands, which leaves this module as the single supported entry point:

    python -m supervisor.tools.run_child matlab --script run_factor
    python -m supervisor.tools.run_child python --module pytest -- -q tests
    python -m supervisor.tools.run_child cargo --sub test -- --release

Exit status is the child's own status, so callers can chain on it. Without a
run context (``SOP_RUN_ID`` missing — an interactive human session) the tool
refuses to start anything: an unowned child is exactly what the hard rule
forbids, and silently falling back to a bare spawn would recreate the hole.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from supervisor.runtime.process_base import ProcResult
from supervisor.runtime.processes import MatlabProcess, PythonProcess, RustProcess


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m supervisor.tools.run_child",
        description="Run a MATLAB / Python / cargo child owned by this run.",
    )
    parser.add_argument(
        "kind",
        choices=("matlab", "python", "cargo"),
        help="which language wrapper to use",
    )
    parser.add_argument("--workspace", default="", help="working directory (default: cwd)")
    parser.add_argument("--timeout", type=float, default=0.0, help="seconds; 0 = no limit")

    matlab = parser.add_argument_group("matlab")
    matlab.add_argument("--script", default="", help="MATLAB statement passed to -batch")

    python = parser.add_argument_group("python")
    python.add_argument("--module", default="", help="python -m <module> (e.g. pytest)")
    python.add_argument("--python-script", default="", help="path to a .py file")

    cargo = parser.add_argument_group("cargo")
    cargo.add_argument("--sub", default="", help="cargo subcommand (build/test/run/check)")

    return parser


def _split_remainder(items: list[str]) -> list[str]:
    args = list(items or [])
    if args and args[0] == "--":
        args = args[1:]
    return args


def _parse(argv: list[str] | None) -> tuple[argparse.Namespace, list[str]]:
    """Parse the tool's own flags; everything after ``--`` goes to the child."""
    raw = list(sys.argv[1:] if argv is None else argv)
    head, child_args = raw, []
    if "--" in raw:
        index = raw.index("--")
        head, child_args = raw[:index], raw[index + 1 :]
    return build_parser().parse_args(head), child_args


def _make_child(args, child_args: list[str], workspace: Path, run_id: str):
    timeout = args.timeout if args.timeout and args.timeout > 0 else None
    if args.kind == "matlab":
        if not args.script.strip():
            raise SystemExit("matlab needs --script <statement>")
        return MatlabProcess(
            run_id=run_id,
            script=args.script,
            cwd=workspace,
            timeout=timeout,
        )
    if args.kind == "python":
        if not (args.module or args.python_script):
            raise SystemExit("python needs --module <mod> or --python-script <file>")
        return PythonProcess(
            run_id=run_id,
            script=args.python_script,
            module=args.module,
            args=child_args,
            cwd=workspace,
            timeout=timeout,
            log_dir=workspace / "logs",
        )
    if not args.sub.strip():
        raise SystemExit("cargo needs --sub <build|test|run|check>")
    return RustProcess(
        run_id=run_id,
        subcommand=args.sub,
        args=child_args,
        cwd=workspace,
        timeout=timeout,
        log_dir=workspace / "logs",
    )


def main(argv: list[str] | None = None) -> int:
    args, child_args = _parse(argv)
    run_id = (os.environ.get("SOP_RUN_ID") or "").strip()
    if not run_id:
        print(
            "run_child: SOP_RUN_ID is not set. This tool only starts children "
            "that belong to a supervised run; an unowned child cannot be "
            "terminated safely later. Start the work from the supervisor run "
            "instead of calling this directly.",
            file=sys.stderr,
        )
        return 2

    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd()
    if not workspace.is_dir():
        print(f"run_child: workspace not found: {workspace}", file=sys.stderr)
        return 2

    child = _make_child(args, child_args, workspace, run_id)
    try:
        result = child.run()
    except FileNotFoundError as exc:
        print(f"run_child: {exc}", file=sys.stderr)
        return 127

    _emit(result)
    return result.returncode if result.returncode >= 0 else 1


def _emit(result: ProcResult) -> None:
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
    if result.timed_out:
        print(
            f"run_child: child timed out after {result.duration_s:.0f}s and was terminated "
            "through its own containment scope",
            file=sys.stderr,
        )
    if result.log_path is not None:
        print(f"run_child: log {result.log_path}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
