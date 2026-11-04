from __future__ import annotations

import json as _json
import logging
import os
import re
import time
from collections.abc import Callable
from pathlib import Path

from supervisor.monitoring.session_tracker import estimate_tokens, truncate_prompt
from supervisor.utils.filesystem.file_ops import safe_read_text
from supervisor.utils.filesystem.path_filters import is_candidate_file
from supervisor.utils.text_utils import head_tail_excerpt, normalize_model_response

logger = logging.getLogger(__name__)

# Protected-file injection bounds. ``read_protected_files`` walks
# ``.opencode/`` and ``read_protected_files_for_suggestions`` reads up to N
# workspace files WHOLE — both were injected verbatim into every judge and
# suggestion prompt with no cap. Measured on one study: a single suggestion
# call carried a 503 KB user message (median 147 KB over 216 calls, 86% of
# that run's 52.7 MB of prompts) because one selected markdown file had grown
# to hundreds of KB. A prompt that costs more than the decision it informs is
# waste; these caps keep the head and tail of each file.
_MAX_PROTECTED_FILE_CHARS = 8_000
_MAX_PROTECTED_TOTAL_CHARS = 24_000
_MAX_SUGGESTION_FILE_CHARS = 4_000
_MAX_SUGGESTION_TOTAL_CHARS = 12_000


def _age_label(mtime: float, now: float | None = None) -> str:
    """Human-scaled age of a file ("just edited", "42m ago", ...); '' if unknown.

    A raw epoch or an age in seconds is noise to a model; what it needs is
    "this moved in the turn I am judging" versus "this has been untouched for
    hours".
    """
    if not mtime:
        return ""
    seconds = max(0.0, (now if now is not None else time.time()) - mtime)
    if seconds < 90:
        return "just edited"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.0f}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.0f}h ago"
    return f"{hours / 24:.0f}d ago"


class SupervisorContextManager:
    def __init__(
        self,
        *,
        workspace: Path,
        max_tokens: int,
        max_protected_files_for_suggestions: int,
        protocol_target: str,
        model: str,
        client,
        ignore_matcher,
        log_prompt,
        skip_dirs: set[str],
        skip_dir_prefixes,
        generated_markdown_files: set[str],
        reasoning_body: dict | None = None,
    ):
        self._workspace = workspace
        self._max_tokens = max_tokens
        self._max_protected_files_for_suggestions = max_protected_files_for_suggestions
        self._protocol_target = protocol_target
        self._model = model
        self._client = client
        self._ignore_matcher = ignore_matcher
        self._log_prompt = log_prompt
        self._skip_dirs = skip_dirs
        self._skip_dir_prefixes = skip_dir_prefixes
        self._generated_markdown_files = generated_markdown_files
        # Thinking-effort extra_body shared with the judge chat path.
        self._reasoning_body = reasoning_body
        # Optional providers (wired by set_change_providers) that tell the
        # file-selection listing what changed since the previous turn.
        self._delta_provider: Callable[[], dict[str, str]] = dict
        self._mtime_provider: Callable[[], dict[str, float]] = dict

    def delta_provider(self) -> dict[str, str]:
        try:
            return self._delta_provider() or {}
        except Exception:
            logger.warning("delta provider failed", exc_info=True)
            return {}

    def mtime_provider(self) -> dict[str, float]:
        try:
            return self._mtime_provider() or {}
        except Exception:
            logger.warning("mtime provider failed", exc_info=True)
            return {}

    def read_protected_files(self) -> dict[str, str]:
        protected_contents: dict[str, str] = {}
        workspace = self._workspace.resolve()

        def _add(rel: str, path) -> None:
            if sum(len(v) for v in protected_contents.values()) >= (
                _MAX_PROTECTED_TOTAL_CHARS
            ):
                return
            text = safe_read_text(path)
            if len(text) > _MAX_PROTECTED_FILE_CHARS:
                text = head_tail_excerpt(
                    text, _MAX_PROTECTED_FILE_CHARS, end_ratio=0.3,
                )
            protected_contents[rel] = text

        opencoderc = workspace / ".opencoderc"
        if opencoderc.is_file():
            try:
                rel = opencoderc.relative_to(workspace)
                _add(str(rel), opencoderc)
            except (OSError, UnicodeDecodeError):
                pass

        opencode_file = workspace / ".opencode"
        if opencode_file.is_file():
            try:
                rel = opencode_file.relative_to(workspace)
                _add(str(rel), opencode_file)
            except (OSError, UnicodeDecodeError):
                pass

        opencode_dir = workspace / ".opencode"
        if opencode_dir.is_dir():
            for dirpath, dirnames, filenames in os.walk(opencode_dir):
                dirnames[:] = [
                    d
                    for d in dirnames
                    if d not in self._skip_dirs
                    and not any(d.startswith(prefix) for prefix in self._skip_dir_prefixes)
                ]
                for fname in filenames:
                    # ``.log`` alone missed the rotated ``.log.1`` backup: it
                    # was re-read and injected on every single judge turn
                    # (up to 64 MB per rotation on the long studies).
                    if fname.endswith(".log") or ".log." in fname:
                        continue
                    fpath = Path(dirpath) / fname
                    try:
                        rel = fpath.relative_to(workspace)
                        _add(str(rel), fpath)
                    except (OSError, UnicodeDecodeError):
                        pass

        return protected_contents

    def should_skip_file(self, path: str) -> bool:
        protected_markers = (".opencoderc", ".opencode/")
        if any(path.startswith(marker) or path == marker.rstrip("/") for marker in protected_markers):
            return False
        return self._ignore_matcher.matches(path)

    def llm_select_files(
        self,
        file_meta: dict[str, int],
        top_k: int,
        *,
        mtimes: dict[str, float] | None = None,
    ) -> list[str]:
        """Pick which files the advisory pass should read.

        The listing leads with what changed since the previous turn and with
        recency, because that is what makes verification meaningful: a file
        rewritten this turn is where the evidence is, and a static
        alphabetical listing cannot express that. Each row is
        ``<path> (<size>) <delta> <age>``; ``delta`` is ``+new``,
        ``~+12/-3``, ``-deleted`` or blank when unchanged.

        ``mtimes`` lets the caller supply timestamps it already stat'ed;
        anything missing is stat'ed here so recency ordering still works when
        no provider is wired.
        """
        if not file_meta:
            return []

        filtered_meta = {
            path: size
            for path, size in file_meta.items()
            if not self.should_skip_file(path)
        }
        if not filtered_meta:
            return []

        response_budget = max(256, top_k * 50)
        available_tokens = max(1000, self._max_tokens - response_budget - 200)
        deltas = self.delta_provider()
        known_mtimes = dict(self.mtime_provider())
        known_mtimes.update(mtimes or {})
        entries = [
            self._listing_entry(
                path,
                size,
                deltas.get(path, ""),
                known_mtimes.get(path) or self._stat_mtime(path),
            )
            for path, size in filtered_meta.items()
        ]
        # Changed files first (recency-decided inside each group), then the
        # rest newest-first: the judge reads the top of this list.
        entries.sort(key=lambda e: (0 if e[0] else 1, -e[1], e[2]))
        listing = "\n".join(entry[3] for entry in entries)
        if estimate_tokens(listing) > available_tokens:
            listing = truncate_prompt(
                listing,
                available_tokens,
                preserve_end_ratio=0.0,
            )

        from supervisor.prompts import FILE_SELECTION_PROMPT

        prompt = FILE_SELECTION_PROMPT.format(
            top_k=top_k,
            protocol_target=self._protocol_target,
            total_candidates=len(filtered_meta),
            listing=listing,
        )

        try:
            kwargs = {
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
            }
            if self._model.startswith(("o1", "o3")):
                kwargs["max_completion_tokens"] = response_budget
            else:
                kwargs["max_tokens"] = response_budget
                kwargs["temperature"] = 0.0
            if getattr(self, "_reasoning_body", None):
                kwargs["extra_body"] = self._reasoning_body

            self._log_prompt("LLM Select Files", kwargs["messages"])
            response = self._client.chat.completions.create(**kwargs)
            raw = normalize_model_response(
                response.choices[0].message.content,
                "file selection response",
            )

            try:
                match = re.search(r"\[.*\]", raw, re.DOTALL)
                if match:
                    selected = _json.loads(match.group(0))
                else:
                    selected = _json.loads(raw)
                if not isinstance(selected, list):
                    selected = []
            except Exception:
                matches = re.findall(r'["\']([^"\']+)["\']', raw)
                selected = [match for match in matches if match]

            if not selected:
                raise ValueError("Could not extract any paths from LLM response")

            real_paths: list[str] = []
            for path in selected:
                path_str = str(path).strip()
                if not path_str:
                    continue

                if path_str in filtered_meta:
                    if path_str not in real_paths:
                        real_paths.append(path_str)
                    continue

                path_norm = path_str.replace("\\", "/").strip("/")
                best_match = None
                for candidate in filtered_meta:
                    candidate_norm = candidate.replace("\\", "/").strip("/")
                    if (
                        candidate_norm.endswith("/" + path_norm)
                        or path_norm.endswith("/" + candidate_norm)
                        or candidate_norm == path_norm
                    ):
                        best_match = candidate
                        break

                if not best_match:
                    path_name = path_norm.split("/")[-1]
                    for candidate in filtered_meta:
                        candidate_name = candidate.replace("\\", "/").strip("/").split("/")[-1]
                        if candidate_name == path_name:
                            best_match = candidate
                            break

                if best_match and best_match not in real_paths:
                    real_paths.append(best_match)

            return real_paths[:top_k]
        except Exception as exc:
            logger.warning(
                "LLM file selection failed (%s); falling back to first %d files.",
                exc,
                top_k,
            )
            # Fallback keeps the same priority as the rendered listing:
            # changed files first, then newest — never a blind alphabetical cut.
            ranked = sorted(
                entries,
                key=lambda e: (0 if e[0] else 1, -e[1], e[2]),
            )
            return [entry[2] for entry in ranked[:top_k]]

    def set_change_providers(
        self,
        *,
        deltas: Callable[[], dict[str, str]] | None = None,
        mtimes: Callable[[], dict[str, float]] | None = None,
    ) -> None:
        """Wire the loop's per-turn change knowledge into file selection.

        The loop already diffs its cached snapshot against the fresh one every
        judged turn; these providers let the selection listing reuse that
        knowledge instead of re-deriving it. Both are optional so the manager
        still works standalone (tests, tooling) with an empty listing.
        """
        self._delta_provider = deltas
        self._mtime_provider = mtimes

    def _stat_mtime(self, rel_path: str) -> float:
        try:
            return (self._workspace / rel_path).stat().st_mtime
        except OSError:
            return 0.0

    def _listing_entry(
        self,
        path: str,
        size: int,
        delta: str,
        mtime: float,
    ) -> tuple[bool, float, str, str]:
        """(changed, mtime, path, rendered row) for one candidate file."""
        age = _age_label(mtime)
        parts = [f"{path} ({size} bytes)"]
        if delta:
            parts.append(delta)
        if age:
            parts.append(age)
        return (bool(delta), mtime, path, "  ".join(parts))

    def read_protected_files_for_suggestions(self) -> tuple[dict[str, str], list[str]]:
        workspace = self._workspace.resolve()
        file_meta: dict[str, int] = {}
        mtimes: dict[str, float] = {}
        try:
            for path in workspace.rglob("*"):
                if path.name in self._generated_markdown_files:
                    continue
                # Candidate filter: cache dirs (__pycache__, node_modules,
                # build outputs, ...), binary extensions, and files whose
                # content sniffs as binary or cannot be read. Without it the
                # LLM's selection pool filled with byte-compiled junk, and a
                # picked binary burned a selection slot on safe_read_text("") .
                if not is_candidate_file(path):
                    continue
                try:
                    rel = str(path.relative_to(workspace))
                    stat = path.stat()
                    file_meta[rel] = stat.st_size
                    mtimes[rel] = stat.st_mtime
                except OSError:
                    pass
        except OSError as exc:
            logger.warning("Could not walk workspace for file selection: %s", exc)

        if not file_meta:
            return {}, []

        chosen_paths = self.llm_select_files(
            file_meta,
            self._max_protected_files_for_suggestions,
            mtimes=mtimes,
        )

        result: dict[str, str] = {}
        total = 0
        for rel_str in chosen_paths:
            if total >= _MAX_SUGGESTION_TOTAL_CHARS:
                break
            try:
                text = safe_read_text(workspace / rel_str)
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("Could not read selected file %s: %s", rel_str, exc)
                continue
            if len(text) > _MAX_SUGGESTION_FILE_CHARS:
                text = head_tail_excerpt(
                    text, _MAX_SUGGESTION_FILE_CHARS, end_ratio=0.3,
                )
            result[rel_str] = text
            total += len(text)
        return result, chosen_paths

    def find_feedback_file(self) -> Path | None:
        workspace = self._workspace.resolve()
        if not workspace.is_dir():
            return None

        latest_md: Path | None = None
        latest_md_mtime = -1.0
        latest_any: Path | None = None
        latest_any_mtime = -1.0

        for dirpath, dirnames, filenames in os.walk(workspace):
            dirnames[:] = [
                d
                for d in dirnames
                if d not in self._skip_dirs
                and not any(d.startswith(prefix) for prefix in self._skip_dir_prefixes)
            ]
            for fname in filenames:
                fpath = Path(dirpath) / fname
                try:
                    mtime = fpath.stat().st_mtime
                except OSError:
                    continue

                if mtime > latest_any_mtime:
                    latest_any_mtime = mtime
                    latest_any = fpath

                if (
                    fpath.suffix.lower() == ".md"
                    and fname not in self._generated_markdown_files
                    and mtime > latest_md_mtime
                ):
                    latest_md_mtime = mtime
                    latest_md = fpath

        return latest_md or latest_any

    def read_feedback_content(self, feedback_file: Path) -> str:
        try:
            content = safe_read_text(feedback_file)
            logger.info(
                "Read %d chars from feedback file: %s",
                len(content),
                feedback_file.relative_to(self._workspace.resolve()),
            )
            return content
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("Failed to read feedback file %s: %s", feedback_file, exc)
            return ""

    def get_evaluation_context(
        self,
        *,
        read_external_feedback: bool,
    ) -> tuple[str, str]:
        protected_files = self.read_protected_files()
        protected_context = ""
        if protected_files:
            sections = [
                f"--- {path} ---\n{content}\n--- end {path} ---"
                for path, content in protected_files.items()
            ]
            protected_context = (
                "\n\n## Current Protected Files State\n"
                + "\n\n".join(sections)
                + "\n\n"
            )

        feedback_context = ""
        if read_external_feedback:
            feedback_file = self.find_feedback_file()
            if feedback_file is not None:
                feedback_content = self.read_feedback_content(feedback_file)
                if feedback_content:
                    feedback_context = (
                        f"\n\n## External Feedback (from {feedback_file.name})\n"
                        f"{feedback_content}\n"
                        "Use this external feedback as the primary evaluation input.\n\n"
                    )
        return protected_context, feedback_context

    def build_experience_context(self) -> str:
        try:
            from supervisor.utils.experience_tracker import get_experience_context

            experience_text = get_experience_context(
                self._workspace,
                goal=self._protocol_target,
            )
            if not experience_text.strip():
                return ""

            budget = int(self._max_tokens * 0.1)
            if estimate_tokens(experience_text) > budget:
                experience_text = truncate_prompt(
                    experience_text,
                    budget,
                    preserve_end_ratio=0.0,
                )

            return experience_text + "\n--- end retrieved memory ---\n\n"
        except Exception as exc:
            logger.warning("Failed to build experience context: %s", exc)
            return ""
