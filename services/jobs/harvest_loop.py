"""Worker-side harvest loop: one agent pass per selected run, one clean folder.

:class:`HarvestLoop` is the job type ``harvest``'s engine. It walks the
selected terminal runs, writes a report/metadata baseline for each, asks the
configured agent (codex / opencode / pi) to distil the run into
``harvest.md``, then writes the batch ``INDEX.md`` and, for more than one run,
a batch-level ``HARVEST.md``.

The baseline is written before any agent runs, and a fallback note is written
when an agent fails or writes nothing - so the folder layout is always
complete, even for a dead workspace or a broken agent turn.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
import traceback
from dataclasses import replace
from pathlib import Path

from services.jobs.harvest import (
    DEFAULT_HARVEST_PROMPT,
    HARVESTABLE_STATES,
    HarvestEntry,
    index_markdown,
    run_agent_prompt,
    run_report,
    run_slug,
    synthesis_prompt,
    unique_child,
    write_run_baseline,
    write_run_meta,
)
from services.jobs.state_store import JobStateStore
from supervisor.protocols.protocol import resolve_protocol_path
from supervisor.runners.factory import create_runner, resolve_engine
from supervisor.workspace.workspace_guard import WorkspaceGuard

logger = logging.getLogger(__name__)


def _ev(level: str, msg: str) -> dict:
    return {"level": level, "msg": msg}


def _has_text(path: Path) -> bool:
    """True when *path* exists and carries non-whitespace content."""
    try:
        return bool(path.read_text(encoding="utf-8", errors="replace").strip())
    except OSError:
        return False


class HarvestLoop:
    """Execute one harvest job: selected runs -> per-run folders -> index."""

    def __init__(self, config, store: JobStateStore | None = None):
        self.config = config
        self.store = store or JobStateStore()
        self.runner = None
        self._stop = threading.Event()

    def request_stop(self) -> None:
        """Cooperative cancel: stop the live agent and break the run walk."""
        self._stop.set()
        runner = self.runner
        if runner is not None:
            try:
                runner.stop()
            except Exception:
                logger.warning("Could not stop the harvest agent", exc_info=True)

    # ------------------------------------------------------------------ #

    def run_streaming(self):
        try:
            yield from self._run()
        except GeneratorExit:
            raise
        except Exception:  # noqa: BLE001 - report any harvest crash as an event
            yield _ev("error", "Harvest failed:\n" + traceback.format_exc())
        finally:
            self._stop_runner()

    def _run(self):
        started = time.time()
        selected = list(
            dict.fromkeys(
                str(job_id).strip()
                for job_id in (getattr(self.config, "harvest_job_ids", ()) or ())
                if str(job_id).strip()
            ),
        )
        prompt = (
            str(getattr(self.config, "harvest_prompt", "") or "").strip()
            or DEFAULT_HARVEST_PROMPT
        )
        raw_dir = str(getattr(self.config, "harvest_dir", "") or "").strip()

        if not selected:
            yield _ev("error", "Harvest has no selected runs.")
            return
        if not raw_dir:
            yield _ev("error", "Harvest location is empty.")
            return

        batch = Path(raw_dir).expanduser()
        try:
            batch.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            yield _ev("error", f"Cannot create harvest folder {batch}: {exc}")
            return

        yield _ev("info", f"Harvest started: {len(selected)} run(s) -> {batch}")

        entries: list[HarvestEntry] = []
        for index, job_id in enumerate(selected, 1):
            if self._stop.is_set():
                break
            state = self.store.get_job_state(job_id)
            if not state:
                yield _ev("warn", f"{job_id}: no longer in the job store - skipped")
                continue
            job_state = str(state.get("state") or "")
            if job_state not in HARVESTABLE_STATES:
                yield _ev(
                    "warn",
                    f"{job_id}: state {job_state or 'UNKNOWN'} is not finished - skipped",
                )
                continue

            config = state.get("config") or {}
            dest = unique_child(batch, run_slug(job_id, state))
            entry = HarvestEntry(
                job_id=job_id,
                state=job_state,
                workspace=str(config.get("workspace") or ""),
                slug=dest.name,
                dest=dest,
            )
            entries.append(entry)

            try:
                meta = write_run_baseline(dest, job_id, state)
            except OSError as exc:
                entry.harvest = "skipped"
                entry.detail = f"cannot write folder: {exc}"
                yield _ev("error", f"{job_id}: cannot write {dest}: {exc}")
                continue

            workspace = Path(entry.workspace).expanduser() if entry.workspace else None
            if workspace is None or not workspace.is_dir():
                entry.harvest = "fallback"
                entry.detail = "workspace missing"
                self._write_fallback(
                    dest,
                    job_id,
                    "the source workspace is missing, so only the stored report was collected",
                    "",
                )
                self._finish_meta(dest, meta, entry)
                yield _ev(
                    "warn",
                    f"{job_id}: workspace missing - report-only folder written",
                )
                continue

            yield _ev(
                "info",
                f"Harvesting {index}/{len(selected)}: {job_id} "
                f"({job_state}) from {workspace}",
            )
            log_file = self.store.store_dir / f"{job_id}.logs"
            agent_prompt = run_agent_prompt(
                prompt,
                job_id,
                state,
                dest,
                run_report(state),
                log_path=str(log_file.resolve()) if log_file.is_file() else "",
            )
            status, prose = yield from self._run_agent(workspace, agent_prompt)
            if status == "stopped":
                entry.harvest = "cancelled"
                entry.detail = "cancelled while the agent was running"
                self._finish_meta(dest, meta, entry)
                break

            target = dest / "harvest.md"
            if _has_text(target):
                entry.harvest = "agent"
                entry.detail = ""
                yield _ev(
                    "info",
                    f"{job_id}: harvest.md written ({target.stat().st_size:,} bytes)",
                )
            else:
                entry.harvest = "fallback"
                entry.detail = "agent wrote no harvest.md"
                reason = (
                    f"the harvest agent ended with an error: {prose}"
                    if status == "error"
                    else "the harvest agent did not write harvest.md"
                )
                self._write_fallback(
                    dest, job_id, reason, "" if status == "error" else prose,
                )
                yield _ev(
                    "warn",
                    f"{job_id}: no agent harvest - fallback note written to harvest.md",
                )
            self._finish_meta(dest, meta, entry)

        if not entries:
            yield _ev("error", "No harvestable run was processed.")
            return

        cancelled = self._stop.is_set()
        has_summary = False
        if not cancelled:
            has_summary = yield from self._synthesize(batch, prompt, entries)

        index_text = index_markdown(
            batch, started, prompt, entries, has_summary=has_summary,
        )
        try:
            (batch / "INDEX.md").write_text(index_text, encoding="utf-8")
        except OSError as exc:
            yield _ev("error", f"Cannot write {batch / 'INDEX.md'}: {exc}")
            return

        final_path = (batch / "HARVEST.md") if has_summary else (batch / "INDEX.md")
        try:
            report_text = final_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            report_text = index_text
        yield {"level": "report", "msg": report_text}

        if cancelled:
            yield _ev("warn", f"Harvest cancelled; partial collection is in {batch}")
            return

        distilled = sum(1 for entry in entries if entry.harvest == "agent")
        if distilled:
            yield _ev(
                "success",
                f"Harvest complete: {distilled}/{len(entries)} run(s) distilled "
                f"-> {batch}",
            )
        else:
            yield _ev(
                "error",
                f"Harvest produced no distilled notes (0/{len(entries)}); "
                f"report-only folders are in {batch}",
            )

    # ------------------------------------------------------------------ #

    def _run_agent(self, workspace: Path, prompt: str):
        """Drive one agent turn; yields its events, returns (status, prose)."""
        if self._stop.is_set():
            return "stopped", ""
        runner = None
        permissions_path = None
        try:
            run_config = replace(
                self.config,
                workspace=workspace,
                protocol_path=resolve_protocol_path(workspace),
            )
            permissions_path = self._prepare_agent_workspace(run_config, workspace)
            runner = create_runner(run_config)
            self._install_access_guard(run_config, workspace, runner)
            self.runner = runner
            for event in runner.start(prompt):
                if self._stop.is_set():
                    runner.stop()
                    return "stopped", ""
                yield event
            return "ok", runner.read_prose()
        except Exception as exc:
            logger.exception("Harvest agent failed in %s", workspace)
            return "error", str(exc)
        finally:
            self.runner = None
            if runner is not None:
                self._stop_runner(runner)
            self._cleanup_permissions(permissions_path)

    def _synthesize(self, batch: Path, prompt: str, entries: list[HarvestEntry]):
        """Write HARVEST.md from the per-run notes; yields events, returns bool."""
        usable = [
            entry
            for entry in entries
            if entry.harvest == "agent" and _has_text(entry.dest / "harvest.md")
        ]
        if not usable:
            return False
        target = batch / "HARVEST.md"
        if len(usable) == 1:
            try:
                shutil.copyfile(usable[0].dest / "harvest.md", target)
            except OSError as exc:
                yield _ev("warn", f"Cannot write {target}: {exc}")
                return False
            yield _ev("info", "HARVEST.md copied from the single run harvest")
            return True

        yield _ev(
            "info", f"Writing batch summary from {len(usable)} harvest note(s)",
        )
        status, prose = yield from self._run_agent(
            batch, synthesis_prompt(prompt, batch, usable),
        )
        if status == "stopped":
            return False
        if _has_text(target):
            yield _ev("info", "HARVEST.md written")
            return True
        if prose.strip():
            try:
                target.write_text(
                    f"# Harvest\n\n{prose.strip()}\n", encoding="utf-8",
                )
            except OSError as exc:
                yield _ev("warn", f"Cannot write {target}: {exc}")
                return False
            yield _ev(
                "warn", "Batch agent wrote no HARVEST.md; saved its reply instead",
            )
            return True
        yield _ev("warn", "Batch summary unavailable; INDEX.md still lists every run")
        return False

    # ------------------------------------------------------------------ #

    def _prepare_agent_workspace(self, run_config, workspace: Path) -> Path | None:
        """Pre-trust an opencode workspace; remember a config we created.

        The file is removed after the turn when this process created it, so a
        harvest does not leave project config in a finished run's workspace
        (or in the harvest folder itself).
        """
        if resolve_engine(run_config) != "opencode":
            return None
        try:
            written = WorkspaceGuard(workspace).ensure_agent_permissions(workspace)
        except Exception:
            logger.warning(
                "Could not prepare opencode permissions for %s",
                workspace,
                exc_info=True,
            )
            return None
        return Path(written) if written else None

    @staticmethod
    def _install_access_guard(run_config, workspace: Path, runner) -> None:
        """Apply the hard path policy to a harvest turn's runner."""
        from supervisor.security.access_policy import AccessPolicy
        from supervisor.security.engine_guards import install_access_guard

        policy = AccessPolicy.from_config(run_config)
        result = install_access_guard(
            workspace,
            policy,
            resolve_engine(run_config),
        )
        if result.failed:
            raise RuntimeError(
                f"access guard could not be installed: {'; '.join(result.warnings)}",
            )
        from supervisor.security.preflight import run_drill

        drill = run_drill(
            resolve_engine(run_config), workspace, policy, result,
        )
        if not drill.ok:
            raise RuntimeError(
                f"access policy drill failed: {drill.fail_text()}",
            )
        if result.flags:
            setattr(runner, "access_guard_flags", list(result.flags))
        if result.env:
            setattr(runner, "access_guard_env", dict(result.env))
            merged = dict(getattr(runner, "codex_extra_env", None) or {})
            merged.update(result.env)
            setattr(runner, "codex_extra_env", merged)

    @staticmethod
    def _cleanup_permissions(path: Path | None) -> None:
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
            parent = path.parent
            if parent.name == ".opencode" and not any(parent.iterdir()):
                parent.rmdir()
        except OSError:
            logger.warning("Could not clean up %s", path, exc_info=True)

    @staticmethod
    def _write_fallback(
        dest: Path, job_id: str, reason: str, prose: str,
    ) -> None:
        parts = [
            f"# Harvest fallback - {job_id}",
            "",
            "The harvest agent did not produce a distilled note for this run.",
            "",
            f"Reason: {reason}",
            "",
        ]
        if prose.strip():
            parts += ["## Agent reply", "", prose.strip(), ""]
        parts += [
            (
                "The run's stored final report is in `report.md`; provenance "
                "is in `run.json`."
            ),
            "",
        ]
        (dest / "harvest.md").write_text("\n".join(parts), encoding="utf-8")

    @staticmethod
    def _finish_meta(dest: Path, meta: dict, entry: HarvestEntry) -> None:
        meta = dict(meta)
        meta["harvest"] = entry.harvest
        if entry.detail:
            meta["harvest_detail"] = entry.detail
        meta["files"] = sorted(
            path.name
            for path in dest.iterdir()
            if path.is_file() and path.name != "run.json"
        )
        write_run_meta(dest, meta)

    def _stop_runner(self, runner=None) -> None:
        runner = runner if runner is not None else self.runner
        if runner is None:
            return
        try:
            runner.stop()
        except Exception:
            logger.warning("Could not stop the harvest agent", exc_info=True)


__all__ = ["HarvestLoop"]
