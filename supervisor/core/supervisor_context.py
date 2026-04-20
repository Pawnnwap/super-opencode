from __future__ import annotations

import json as _json
import logging
import os
import re
from pathlib import Path

from supervisor.monitoring.session_tracker import estimate_tokens, truncate_prompt
from supervisor.utils.file_ops import safe_read_text
from supervisor.utils.text_utils import normalize_model_response

logger = logging.getLogger(__name__)


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

    def read_protected_files(self) -> dict[str, str]:
        protected_contents: dict[str, str] = {}
        workspace = self._workspace.resolve()

        opencoderc = workspace / ".opencoderc"
        if opencoderc.is_file():
            try:
                rel = opencoderc.relative_to(workspace)
                protected_contents[str(rel)] = safe_read_text(opencoderc)
            except (OSError, UnicodeDecodeError):
                pass

        opencode_file = workspace / ".opencode"
        if opencode_file.is_file():
            try:
                rel = opencode_file.relative_to(workspace)
                protected_contents[str(rel)] = safe_read_text(opencode_file)
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
                    if fname.endswith(".log"):
                        continue
                    fpath = Path(dirpath) / fname
                    try:
                        rel = fpath.relative_to(workspace)
                        protected_contents[str(rel)] = safe_read_text(fpath)
                    except (OSError, UnicodeDecodeError):
                        pass

        return protected_contents

    def should_skip_file(self, path: str) -> bool:
        protected_markers = (".opencoderc", ".opencode/")
        if any(path.startswith(marker) or path == marker.rstrip("/") for marker in protected_markers):
            return False
        return self._ignore_matcher.matches(path)

    def llm_select_files(self, file_meta: dict[str, int], top_k: int) -> list[str]:
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
        listing = "\n".join(
            f"{path} ({size} bytes)" for path, size in sorted(filtered_meta.items())
        )
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
            return sorted(filtered_meta)[:top_k]

    def read_protected_files_for_suggestions(self) -> tuple[dict[str, str], list[str]]:
        workspace = self._workspace.resolve()
        file_meta: dict[str, int] = {}
        try:
            for path in workspace.rglob("*"):
                if not path.is_file():
                    continue
                rel_parts = path.relative_to(workspace).parts
                if any(part.startswith(".") for part in rel_parts):
                    continue
                if path.name in self._generated_markdown_files:
                    continue
                try:
                    file_meta[str(path.relative_to(workspace))] = path.stat().st_size
                except OSError:
                    pass
        except OSError as exc:
            logger.warning("Could not walk workspace for file selection: %s", exc)

        if not file_meta:
            return {}, []

        chosen_paths = self.llm_select_files(
            file_meta,
            self._max_protected_files_for_suggestions,
        )

        result: dict[str, str] = {}
        for rel_str in chosen_paths:
            try:
                result[rel_str] = safe_read_text(workspace / rel_str)
            except (OSError, UnicodeDecodeError) as exc:
                logger.warning("Could not read selected file %s: %s", rel_str, exc)
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
            from supervisor.utils.experience_tracker import read_experience_capped

            experience_text = read_experience_capped(self._workspace, max_chars=10000)
            if not experience_text.strip():
                return ""

            budget = int(self._max_tokens * 0.1)
            if estimate_tokens(experience_text) > budget:
                experience_text = truncate_prompt(
                    experience_text,
                    budget,
                    preserve_end_ratio=0.0,
                )

            return f"--- Previous Experience ---\n{experience_text}\n--- end ---\n\n"
        except Exception as exc:
            logger.warning("Failed to build experience context: %s", exc)
            return ""
