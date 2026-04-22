"""supervisor/llm_supervisor.py - OpenAI-powered judge."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from openai import OpenAI

from supervisor.core.llm_support.chat import (
    chat as _chat_impl,
    chat_with_retry as _chat_with_retry_impl,
    fit_request_to_budget as _fit_request_to_budget_impl,
    truncate_messages_for_limit as _truncate_messages_for_limit_impl,
    truncate_older_turns as _truncate_older_turns_impl,
)
from supervisor.core.llm_support.history import (
    compact_history as _compact_history_impl,
    estimate_current_tokens as _estimate_current_tokens_impl,
    extract_and_store_opencode_output as _extract_and_store_opencode_output_impl,
    log_prompt as _log_prompt_impl,
    should_omit_step_context as _should_omit_step_context_impl,
    should_record_turn as _should_record_turn_impl,
    update_system_prompt as _update_system_prompt_impl,
)
from supervisor.core.llm_support.judgement import (
    analyze_protocol as _analyze_protocol_impl,
    ask_for_compaction_instructions as _ask_for_compaction_instructions_impl,
    ask_for_deletion_permission as _ask_for_deletion_permission_impl,
    generate_suggestions as _generate_suggestions_impl,
    judge as _judge_impl,
    judge_plan as _judge_plan_impl,
    judge_with_step_context as _judge_with_step_context_impl,
    report_final_status as _report_final_status_impl,
    verify_protocol_alignment as _verify_protocol_alignment_impl,
)
from supervisor.core.llm_support.models import (
    _DONE_PHRASES,
    _OPENCODE_GENERATED_MD,
    _SKIP_DIR_PREFIXES,
    _SKIP_DIRS,
    _check_completion_phrases,
    _get_model_token_limit,
    _is_token_limit_error,
    StepContext,
    SupervisorVerdict,
)
from supervisor.core.llm_support.context import SupervisorContextManager
from supervisor.protocols.protocol import Protocol
from supervisor.protocols.alignment import AlignmentResult
from supervisor.protocols.protocol_analyzer import ProtocolAnalysis
from supervisor.workspace.ignore_patterns import IgnoreMatcher

logger = logging.getLogger(__name__)


class LLMSupervisor:
    """Wraps OpenAI chat client. Protocol is system prompt; history is memory."""

    _INTERMEDIATE_STEP_MARKER = "_is_intermediate_step"

    def __init__(
        self,
        protocol: Protocol,
        workspace: Path,
        model: str,
        extra_system: str = "",
        read_external_feedback: bool = False,
        max_tokens: int = 128_000,
        max_protected_files_for_suggestions: int = 5,
        truncation_enabled: bool = True,
        max_history_turns: int = 40,
        compact_intermediate_steps: bool = False,
        model_backup: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ):
        client_kwargs: dict[str, str] = {}
        if api_key:
            client_kwargs["api_key"] = api_key
        if base_url:
            client_kwargs["base_url"] = base_url

        self._client = OpenAI(**client_kwargs)
        self._model = model
        self._model_backup = model_backup
        self._workspace = workspace
        self._system_base = protocol.as_system_prompt(workspace)
        self._system = self._system_base
        if extra_system:
            self._system += f"\n\n{extra_system}"

        self._protocol_target = protocol.target_section
        self._history: list[dict] = []
        self._read_external_feedback = read_external_feedback
        self._model_input_limit = _get_model_token_limit(model)
        self._max_tokens = min(max_tokens, self._model_input_limit)
        self._token_warnings: list[str] = []
        self._max_protected_files_for_suggestions = (
            max_protected_files_for_suggestions
        )
        self._truncation_enabled = truncation_enabled
        self._max_history_turns = max_history_turns
        self._compact_intermediate_steps = compact_intermediate_steps
        self._ignore_matcher = IgnoreMatcher(workspace)
        self._ignore_matcher.load_from_workspace(workspace)
        self._last_opencode_output: str | None = None
        self._context = SupervisorContextManager(
            workspace=workspace,
            max_tokens=self._max_tokens,
            max_protected_files_for_suggestions=self._max_protected_files_for_suggestions,
            protocol_target=self._protocol_target,
            model=self._model,
            client=self._client,
            ignore_matcher=self._ignore_matcher,
            log_prompt=self._log_prompt,
            skip_dirs=_SKIP_DIRS,
            skip_dir_prefixes=_SKIP_DIR_PREFIXES,
            generated_markdown_files=_OPENCODE_GENERATED_MD,
        )

    def _get_model_limit_for_model(self, model: str) -> int:
        return _get_model_token_limit(model)

    @staticmethod
    def _extract_token_limit_from_error(exc: Exception) -> int | None:
        text = str(exc)
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            err = body.get("error") if isinstance(body.get("error"), dict) else body
            if isinstance(err, dict):
                text = f"{text} {err.get('message', '')}"
        match = re.search(r"(\d{4,7})", text)
        if not match:
            return None
        try:
            value = int(match.group(1))
        except ValueError:
            return None
        if 1024 <= value <= 10_000_000:
            return value
        return None

    def read_protected_files(self) -> dict[str, str]:
        return self._context.read_protected_files()

    def _should_skip_file(self, path: str) -> bool:
        return self._context.should_skip_file(path)

    def _llm_select_files(self, file_meta: dict[str, int], top_k: int) -> list[str]:
        return self._context.llm_select_files(file_meta, top_k)

    def _read_protected_files_for_suggestions(self) -> tuple[dict[str, str], list[str]]:
        return self._context.read_protected_files_for_suggestions()

    def _find_feedback_file(self) -> Path | None:
        feedback_file = self._context.find_feedback_file()
        workspace = self._workspace.resolve()
        if feedback_file is not None:
            try:
                rel = feedback_file.relative_to(workspace)
            except ValueError:
                rel = feedback_file
            if feedback_file.suffix.lower() == ".md":
                logger.info("Using external feedback file: %s", rel)
            else:
                logger.info(
                    "No non-generated .md found; falling back to latest file: %s",
                    rel,
                )
        return feedback_file

    def _read_feedback_content(self, feedback_file: Path) -> str:
        return self._context.read_feedback_content(feedback_file)

    def _get_evaluation_context(self) -> tuple[str, str]:
        return self._context.get_evaluation_context(
            read_external_feedback=self._read_external_feedback,
        )

    def _build_experience_context(self) -> str:
        return self._context.build_experience_context()

    def judge(self, opencode_output: str) -> SupervisorVerdict:
        return _judge_impl(self, opencode_output)

    def judge_with_step_context(
        self,
        opencode_output: str,
        step_context: StepContext,
    ) -> SupervisorVerdict:
        return _judge_with_step_context_impl(self, opencode_output, step_context)

    def judge_plan(
        self,
        opencode_output: str,
        plan_round: int,
        total_plan_rounds: int,
        step_context: StepContext | None = None,
    ) -> SupervisorVerdict:
        return _judge_plan_impl(
            self,
            opencode_output,
            plan_round,
            total_plan_rounds,
            step_context,
        )

    def ask_for_compaction_instructions(self) -> SupervisorVerdict:
        return _ask_for_compaction_instructions_impl(self)

    def ask_for_deletion_permission(
        self,
        candidates: list[str],
        workspace: Path,
    ) -> SupervisorVerdict:
        return _ask_for_deletion_permission_impl(self, candidates, workspace)

    def report_final_status(
        self,
        reason: str,
        opencode_output: str,
    ) -> str:
        return _report_final_status_impl(self, reason, opencode_output)

    def generate_suggestions(
        self,
        opencode_output: str,
        current_summary: str = "",
        step_context: StepContext | None = None,
    ) -> tuple[str, list[str]]:
        return _generate_suggestions_impl(
            self,
            opencode_output,
            current_summary,
            step_context,
        )

    def analyze_protocol(
        self,
        protocol: Protocol,
        use_llm: bool = False,
    ) -> ProtocolAnalysis:
        return _analyze_protocol_impl(self, protocol, use_llm)

    def verify_protocol_alignment(
        self,
        opencode_output: str,
        protocol: Protocol,
    ) -> AlignmentResult:
        return _verify_protocol_alignment_impl(opencode_output, protocol)

    def get_token_warnings(self) -> list[str]:
        return list(self._token_warnings)

    def clear_token_warnings(self) -> None:
        self._token_warnings.clear()

    def should_record_turn(self, content: str, role: str = "user") -> bool:
        return _should_record_turn_impl(self, content, role)

    def compact_history(self) -> None:
        _compact_history_impl(self)

    def estimate_current_tokens(self) -> int:
        return _estimate_current_tokens_impl(self)

    def _log_prompt(self, title: str, messages: list[dict]) -> None:
        _log_prompt_impl(self, title, messages)

    def _chat(
        self,
        user_content: str,
        is_intermediate: bool = False,
        history_content: str | None = None,
    ) -> SupervisorVerdict:
        return _chat_impl(self, user_content, is_intermediate, history_content)

    def _chat_with_retry(
        self,
        messages: list[dict],
        record_content: str,
        should_record_user: bool,
    ) -> SupervisorVerdict:
        return _chat_with_retry_impl(
            self,
            messages,
            record_content,
            should_record_user,
        )

    def _should_omit_step_context(
        self,
        opencode_output: str,
        experience_context: str,
        feedback_context: str,
        protected_context: str,
    ) -> bool:
        return _should_omit_step_context_impl(
            self,
            opencode_output,
            experience_context,
            feedback_context,
            protected_context,
        )

    def _extract_and_store_opencode_output(
        self,
        content: str,
        should_record: bool,
    ) -> None:
        _extract_and_store_opencode_output_impl(self, content, should_record)

    def update_system_prompt(self, new_preamble: str) -> None:
        _update_system_prompt_impl(self, new_preamble)

    def _fit_request_to_budget(self, user_content: str) -> str:
        return _fit_request_to_budget_impl(self, user_content)

    @staticmethod
    def _truncate_older_turns(messages: list[dict]) -> list[dict]:
        return _truncate_older_turns_impl(messages)

    def _truncate_messages_for_limit(
        self,
        messages: list[dict],
        model_limit: int,
    ) -> list[dict]:
        return _truncate_messages_for_limit_impl(messages, model_limit)


__all__ = ["LLMSupervisor", "StepContext", "SupervisorVerdict", "_DONE_PHRASES"]
