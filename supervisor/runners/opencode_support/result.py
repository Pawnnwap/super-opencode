from __future__ import annotations


class RunResult:
    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
        timed_out: bool = False,
        exception: str = "",
    ):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        self.timed_out = timed_out
        self.exception = exception

    @property
    def output(self) -> str:
        """Combined output surfaced to supervisor loop."""
        parts = []
        if self.exception:
            parts.append(f"[EXCEPTION] {self.exception}")
        if self.timed_out:
            parts.append("[TIMED OUT]")
        if self.stdout.strip():
            parts.append(self.stdout.strip())
        if self.stderr.strip():
            parts.append(f"[stderr]\n{self.stderr.strip()}")
        if self.returncode not in (0, None):
            parts.append(f"[exit {self.returncode}]")
        return "\n".join(parts)

    @property
    def ok(self) -> bool:
        return not self.timed_out and not self.exception and self.returncode == 0

    def diagnostic(self) -> str:
        lines = [
            f"exit_code : {self.returncode}",
            f"timed_out : {self.timed_out}",
            f"exception : {self.exception or '(none)'}",
            f"stdout    : {len(self.stdout)} chars",
            f"stderr    : {len(self.stderr)} chars",
        ]
        if self.stdout.strip():
            lines.append(f"--- stdout ---\n{self.stdout[:1200]}")
        if self.stderr.strip():
            lines.append(f"--- stderr ---\n{self.stderr[:1200]}")
        return "\n".join(lines)

