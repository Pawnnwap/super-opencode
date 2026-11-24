from __future__ import annotations

import contextvars
import json
import logging
import os
import random
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from openai import (
    APIConnectionError,
    APIError,
    APITimeoutError,
    BadRequestError,
    InternalServerError,
    OpenAIError,
    RateLimitError,
)

from supervisor.core.failure_class import classify_failure
from supervisor.core.llm_support.history_compaction import build_context_messages
from supervisor.core.llm_support.judge_gate import JudgeCancelled, judge_slot
from supervisor.core.llm_support.models import (
    SupervisorVerdict,
    _is_token_limit_error,
    flag_contradicted_criteria,
    parse_skill_requests,
    parse_verdict_structure_ex,
)
from supervisor.monitoring.session_tracker import (
    estimate_request_tokens,
    truncate_with_fallback,
    warn_if_exceeds_limit,
)
from supervisor.monitoring.token_estimator import should_truncate
from supervisor.security.redaction import redact_data
from supervisor.security.secrets import open_json, seal_json
from supervisor.utils.text_utils import normalize_model_response

logger = logging.getLogger(__name__)

_JUDGE_CONTEXT: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "judge_context",
    default=None,
)
_HEADER_WHITELIST = ("server", "content-type", "x-request-id", "via", "date")
# A gateway 502/503/504 is transient far more often than the old fail-fast
# policy assumed: the 09:49 outage returned 502s for ~30 s while the box kept
# serving other requests. Four calls over ~50 s of jittered backoff cover
# that window; a dead endpoint still fails in well under two minutes.
_SERVER_RETRY_WAITS = (5.0, 15.0, 30.0)


@contextmanager
def judge_phase(phase: str, round: int | None = None, total: int | None = None):
    """Name the phase a judge call belongs to for failure diagnostics."""
    token = _JUDGE_CONTEXT.set({"phase": phase, "round": round, "total": total})
    try:
        yield
    finally:
        _JUDGE_CONTEXT.reset(token)


_CHECKPOINT_META: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "judge_checkpoint_meta",
    default=None,
)


@contextmanager
def judge_checkpoint(meta: dict):
    """Persist the exact request before this judge call; clear it on verdict.

    A plan phase can spend an hour producing the output that is judged next;
    a provider outage at that point used to discard the turn because replay
    restarted the plan from scratch. The checkpoint lets a replay re-issue
    just the judge call.
    """
    token = _CHECKPOINT_META.set(dict(meta))
    try:
        yield
    finally:
        _CHECKPOINT_META.reset(token)


def _response_snapshot(exc: BaseException) -> dict:
    """Status, request id, safe headers and body excerpt of an API error."""
    response = getattr(exc, "response", None)
    headers: dict[str, str] = {}
    body = ""
    if response is not None:
        try:
            for name in _HEADER_WHITELIST:
                value = response.headers.get(name)
                if value:
                    headers[name] = str(value)
        except Exception:  # diagnostics must not mask the error
            logger.debug("Could not read response headers", exc_info=True)
        try:
            body = response.text or ""
        except Exception:  # noqa: BLE001
            body = ""
    raw_body = getattr(exc, "body", None)
    if not body and raw_body is not None:
        try:
            body = (
                raw_body
                if isinstance(raw_body, str)
                else json.dumps(
                    raw_body,
                    ensure_ascii=False,
                )
            )
        except Exception:  # noqa: BLE001
            body = repr(raw_body)
    return redact_data(
        {
            "status_code": getattr(exc, "status_code", None),
            "request_id": getattr(exc, "request_id", None),
            "headers": headers,
            "body_excerpt": body[:500],
            "message": str(exc)[:500],
        }
    )


def _error_detail(exc: BaseException) -> str:
    """Compact status/request-id/body suffix for retry log lines."""
    snapshot = _response_snapshot(exc)
    bits: list[str] = []
    if snapshot.get("status_code") is not None:
        bits.append(f"status={snapshot['status_code']}")
    if snapshot.get("request_id"):
        bits.append(f"request_id={snapshot['request_id']}")
    if snapshot.get("body_excerpt"):
        bits.append(f"body={snapshot['body_excerpt'][:200]!r}")
    return f" [{', '.join(bits)}]" if bits else ""


def _atomic_write_json(path: Path, payload: dict, *, seal: bool = False) -> None:
    payload = redact_data(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            if seal:
                handle.write(seal_json(payload))
            else:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _checkpoint_path(supervisor) -> Path | None:
    workspace = getattr(supervisor, "_workspace", None)
    if workspace is None:
        return None
    return Path(workspace) / ".opencode" / "judge_pending.json"


def _write_judge_checkpoint(
    supervisor,
    messages: list[dict],
    record_content: str,
    user_content: str,
    should_record_user: bool,
    meta: dict,
) -> None:
    path = _checkpoint_path(supervisor)
    if path is None:
        return
    payload = {
        "schema": 1,
        **{key: value for key, value in meta.items() if key != "messages"},
        "messages": messages,
        "history": [
            dict(message)
            for message in getattr(supervisor, "_history", [])
            if isinstance(message, dict)
        ],
        "record_content": record_content,
        "user_content": user_content,
        "should_record_user": bool(should_record_user),
        "created_at": datetime.now().astimezone().isoformat(),
    }
    try:
        _atomic_write_json(path, payload, seal=True)
    except Exception:  # the judge call is worth making even if the dump fails
        logger.warning("Could not write judge checkpoint %s", path, exc_info=True)


def _clear_judge_checkpoint(supervisor) -> None:
    path = _checkpoint_path(supervisor)
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not clear judge checkpoint %s", path, exc_info=True)


def load_judge_checkpoint(workspace) -> dict | None:
    """Read a pending plan judge; a corrupt file is ignored, not fatal."""
    path = Path(workspace) / ".opencode" / "judge_pending.json"
    try:
        text = path.read_text(encoding="utf-8")
        try:
            payload = open_json(text)
        except Exception:  # noqa: BLE001 - legacy plaintext checkpoints remain readable
            payload = json.loads(text)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring unreadable judge checkpoint %s: %s", path, exc)
        return None
    if not isinstance(payload, dict) or payload.get("schema") != 1:
        logger.warning("Ignoring unsupported judge checkpoint %s", path)
        return None
    if not isinstance(payload.get("messages"), list) or not payload["messages"]:
        logger.warning("Ignoring judge checkpoint without messages: %s", path)
        return None
    return payload


def resume_chat(supervisor, payload: dict) -> SupervisorVerdict:
    """Re-issue a checkpointed judge call without re-running the agent turn."""
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("judge checkpoint has no messages")
    record_content = str(payload.get("record_content") or "")
    user_content = str(payload.get("user_content") or "")
    should_record_user = bool(payload.get("should_record_user", True))
    history = payload.get("history")
    if isinstance(history, list):
        supervisor._history = [
            dict(message) for message in history if isinstance(message, dict)
        ]
    verdict = chat_with_retry(
        supervisor,
        messages,
        record_content,
        should_record_user,
    )
    supervisor._last_judged_content = user_content
    supervisor._last_verdict = verdict
    _clear_judge_checkpoint(supervisor)
    return verdict


def _failure_payload(
    supervisor,
    exc: BaseException,
    *,
    model: str,
    extra_body: dict | None,
    messages: list[dict],
    attempt: int,
    elapsed: float,
    total_elapsed: float,
) -> dict:
    ctx = _JUDGE_CONTEXT.get() or {}
    try:
        prompt_chars = sum(len(str(m.get("content") or "")) for m in messages)
    except Exception:  # noqa: BLE001
        prompt_chars = 0
    return {
        "ts": datetime.now().astimezone().isoformat(),
        "failure_class": classify_failure(exc),
        "phase": ctx.get("phase"),
        "round": ctx.get("round"),
        "total_rounds": ctx.get("total"),
        "model": model,
        "extra_body": extra_body,
        "attempt": attempt,
        "elapsed_s": round(elapsed, 3),
        "total_elapsed_s": round(total_elapsed, 3),
        "prompt_chars": prompt_chars,
        "message_count": len(messages),
        "exc_type": type(exc).__name__,
        **_response_snapshot(exc),
        "messages": messages,
    }


def _record_judge_failure(
    supervisor,
    exc: BaseException,
    *,
    model: str,
    extra_body: dict | None,
    messages: list[dict],
    attempt: int,
    elapsed: float,
    total_elapsed: float,
) -> None:
    """Attach the failing request context and persist a replayable sidecar."""
    payload = _failure_payload(
        supervisor,
        exc,
        model=model,
        extra_body=extra_body,
        messages=messages,
        attempt=attempt,
        elapsed=elapsed,
        total_elapsed=total_elapsed,
    )
    try:
        exc.failure_context = payload
    except Exception:  # diagnostics must not mask the error
        logger.debug("Could not attach failure context", exc_info=True)
    logger.error(
        "Judge call failed [%s] phase=%s model=%s attempt=%d elapsed=%.1fs "
        "total=%.1fs status=%s request_id=%s headers=%s body=%r",
        payload["failure_class"],
        payload["phase"],
        model,
        attempt,
        elapsed,
        total_elapsed,
        payload["status_code"],
        payload["request_id"],
        payload["headers"],
        payload["body_excerpt"][:200],
    )
    workspace = getattr(supervisor, "_workspace", None)
    if workspace is None:
        return
    try:
        out_dir = Path(workspace) / ".opencode" / "judge_failures"
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S-%f")
        _atomic_write_json(
            out_dir / f"{stamp}-{payload['failure_class']}.json",
            payload,
        )
    except Exception:
        logger.debug("Could not write judge failure sidecar", exc_info=True)


def chat(
    supervisor,
    user_content: str,
    is_intermediate: bool = False,
    history_content: str | None = None,
    omit_history: bool = False,
) -> SupervisorVerdict:
    """One judge-model call.

    ``omit_history`` sends the system prompt plus this request only. Side
    calls whose answer never feeds a run decision (advisory suggestions,
    protocol analysis) use it: replaying the judge's whole conversation into
    them was 62% of each suggestion call's payload — measured 78.8 KB median
    of history against 49.3 KB of actual content.
    """
    should_record_user = (
        not is_intermediate
        or not supervisor._compact_intermediate_steps
        or supervisor.should_record_turn(user_content, "user")
    )
    if (
        should_record_user
        and supervisor._last_opencode_output is not None
        and user_content.strip() == supervisor._last_opencode_output.strip()
    ):
        should_record_user = False

    if (
        supervisor._reuse_identical_verdicts
        and supervisor._last_verdict is not None
        and user_content == supervisor._last_judged_content
    ):
        supervisor._verdict_reuses += 1
        logger.info(
            "Reused prior verdict (%d): identical judge input (%d chars), no LLM call",
            supervisor._verdict_reuses,
            len(user_content),
        )
        return supervisor._last_verdict

    record_content = history_content if history_content is not None else user_content
    conv_text = "\n".join(
        msg["content"] for msg in supervisor._history if msg.get("content")
    )
    estimate = estimate_request_tokens(supervisor._system, conv_text, user_content)

    warning_msgs = warn_if_exceeds_limit(estimate, supervisor._max_tokens)
    for msg in warning_msgs:
        supervisor._token_warnings.append(msg)

    if should_truncate(estimate, supervisor._max_tokens):
        # Sizes only: dumping the full over-limit prompt to stderr every turn
        # once bloated the shared app error log by hundreds of MB (the raw
        # prompts are already archived in .opencode/supervisor_prompts.log).
        logger.warning(
            "Judge prompt exceeds budget: total=%d system=%d history=%d user=%d "
            "(max_tokens=%d, available=%d)",
            estimate.total,
            estimate.system_prompt,
            estimate.conversation_history,
            estimate.user_input,
            supervisor._max_tokens,
            int(supervisor._max_tokens * 0.75),
        )

    if supervisor._truncation_enabled and should_truncate(
        estimate,
        supervisor._max_tokens,
    ):
        original_total = estimate.total
        user_content = fit_request_to_budget(supervisor, user_content)
        new_estimate = estimate_request_tokens(
            supervisor._system,
            "\n".join(m["content"] for m in supervisor._history if m.get("content")),
            user_content,
        )
        trunc_warning = (
            f"Prompt truncated to fit within token limit. "
            f"Original: {original_total} tokens, New: {new_estimate.total} tokens, "
            f"Max available: {int(supervisor._max_tokens * (1.0 - 0.25))} tokens"
        )
        logger.warning(trunc_warning)
        supervisor._token_warnings.append(trunc_warning)

    messages = [{"role": "system", "content": supervisor._system}]
    if not omit_history:
        messages.extend(
            build_context_messages(
                supervisor._history,
                max_tokens=supervisor._max_tokens,
                verbatim_turns=supervisor._history_verbatim_turns,
                budget_fraction=supervisor._history_budget_fraction,
            ),
        )
    messages.append({"role": "user", "content": user_content})

    supervisor._log_prompt("Supervisor Chat", messages)
    checkpoint_meta = _CHECKPOINT_META.get()
    if checkpoint_meta is not None:
        _write_judge_checkpoint(
            supervisor,
            messages,
            record_content,
            user_content,
            should_record_user,
            checkpoint_meta,
        )
    verdict = chat_with_retry(
        supervisor,
        messages,
        record_content,
        should_record_user,
    )
    if checkpoint_meta is not None:
        _clear_judge_checkpoint(supervisor)
    supervisor._last_judged_content = user_content
    supervisor._last_verdict = verdict
    return verdict


def _sleep_or_cancel(seconds: float, cancel_event) -> None:
    if cancel_event is None:
        time.sleep(seconds)
        return
    if cancel_event.wait(seconds):
        raise JudgeCancelled("judge retry cancelled")


def _create_completion(supervisor, create_kwargs: dict, cancel_event):
    create = supervisor._client.chat.completions.create
    if cancel_event is None:
        with judge_slot():
            return create(**create_kwargs)
    result: dict[str, object] = {}

    def _run() -> None:
        try:
            with judge_slot(cancel_event=cancel_event):
                result["response"] = create(**create_kwargs)
        except Exception as exc:  # noqa: BLE001 - propagated on the calling thread
            result["error"] = exc

    thread = threading.Thread(target=_run, name="judge-http", daemon=True)
    thread.start()
    while thread.is_alive():
        if cancel_event.wait(0.1):
            raise JudgeCancelled("judge request cancelled")
    thread.join()
    if "error" in result:
        raise result["error"]
    return result["response"]


def chat_with_retry(
    supervisor,
    messages: list[dict],
    record_content: str,
    should_record_user: bool,
) -> SupervisorVerdict:
    max_retries = 5
    empty_choices_retries = 0
    max_empty_choices_retries = 3
    attempt = 0
    transient_attempt = 0
    max_transient_retries = 4
    # Endpoint-unreachable class: a dead endpoint does not recover inside a
    # backoff ladder — one quick retry, then fail the turn fast.
    conn_attempt = 0
    max_conn_retries = 1
    # Transient 5xx ladder (gateway blips). Independent from the token-limit
    # and backup counters so neither policy can reset the other.
    server_retries = 0
    max_server_retries = len(_SERVER_RETRY_WAITS)
    working_messages = list(messages)
    using_backup = False
    token_limit_retry = 0
    max_token_limit_retries = 3
    cancel_event = getattr(supervisor, "_cancel_event", None)

    call_index = 0
    total_started = time.monotonic()
    attempt_started = total_started
    current_model = supervisor._model
    create_kwargs: dict = {}
    try:
        while attempt <= max_retries:
            if cancel_event is not None and cancel_event.is_set():
                raise JudgeCancelled("judge call cancelled")
            current_model = (
                supervisor._model_backup if using_backup else supervisor._model
            )
            try:
                create_kwargs = {
                    "model": current_model,
                    "messages": working_messages,
                }
                # Thinking-effort control for real runs; None = provider default.
                if getattr(supervisor, "_reasoning_body", None):
                    create_kwargs["extra_body"] = supervisor._reasoning_body
                call_index += 1
                attempt_started = time.monotonic()
                response = _create_completion(supervisor, create_kwargs, cancel_event)
                if not response.choices:
                    if empty_choices_retries >= max_empty_choices_retries:
                        raise OpenAIError(
                            f"LLM response returned no choices after {max_empty_choices_retries} retries",
                        )
                    wait = 15 * (2**empty_choices_retries)
                    logger.warning(
                        "LLM response returned no choices, retry %d/%d after %ds",
                        empty_choices_retries + 1,
                        max_empty_choices_retries,
                        wait,
                    )
                    _sleep_or_cancel(wait, cancel_event)
                    empty_choices_retries += 1
                    continue
                empty_choices_retries = 0
                reply = normalize_model_response(
                    response.choices[0].message.content,
                    "supervisor response",
                )
                break
            except RateLimitError as exc:
                if transient_attempt >= max_transient_retries:
                    logger.error(
                        "Transient API error after %d retries, giving up: %s",
                        max_transient_retries,
                        exc,
                    )
                    raise
                wait = min(60, 5 * (2**transient_attempt))
                logger.warning(
                    "Transient API error %s (attempt %d/%d), retrying after %ds",
                    type(exc).__name__,
                    transient_attempt + 1,
                    max_transient_retries,
                    wait,
                )
                time.sleep(wait)
                transient_attempt += 1
                continue
            except (APIConnectionError, APITimeoutError) as exc:
                if conn_attempt >= max_conn_retries:
                    logger.error(
                        "Connection error after %d quick retry, failing fast: %s",
                        max_conn_retries,
                        exc,
                    )
                    raise
                conn_attempt += 1
                logger.warning(
                    "Connection error %s — one quick retry after 2s, then fast fail",
                    type(exc).__name__,
                )
                _sleep_or_cancel(2, cancel_event)
                continue
            except BadRequestError as exc:
                if _is_token_limit_error(exc):
                    if token_limit_retry >= max_token_limit_retries:
                        logger.error(
                            "BadRequestError: input length exceeds model limit after %d retries",
                            max_token_limit_retries,
                        )
                        raise
                    current_limit = supervisor._extract_token_limit_from_error(
                        exc
                    ) or supervisor._get_model_limit_for_model(current_model)
                    old_count = len(working_messages)
                    working_messages = truncate_messages_for_limit(
                        working_messages,
                        current_limit,
                    )
                    new_count = len(working_messages)
                    if new_count >= old_count:
                        working_messages = truncate_older_turns(working_messages)
                        new_count = len(working_messages)
                    token_limit_retry += 1
                    logger.warning(
                        "BadRequestError (input length) retry %d/%d: truncated messages from %d to %d for limit %d",
                        token_limit_retry,
                        max_token_limit_retries,
                        old_count,
                        new_count,
                        current_limit,
                    )
                    continue
                raise
            except (APIError, OpenAIError) as exc:
                if isinstance(exc, InternalServerError):
                    status = getattr(exc, "status_code", None)
                    if status == 511 or _is_token_limit_error(exc):
                        # Token-limit surfaced as 5xx: truncating the prompt is
                        # the only useful response. A backup call against the
                        # same endpoint just repeats the oversized request.
                        if attempt >= max_retries:
                            logger.error(
                                "Prompt still exceeds token limit after %d truncation attempts",
                                max_retries,
                            )
                            raise
                        old_count = len(working_messages)
                        working_messages = truncate_older_turns(working_messages)
                        new_count = len(working_messages)
                        removed = old_count - new_count
                        attempt += 1
                        logger.warning(
                            "Token-limit retry %d/%d: removed %d older message(s), now %d messages in request",
                            attempt,
                            max_retries,
                            removed,
                            new_count,
                        )
                        continue
                    if server_retries >= max_server_retries:
                        logger.error(
                            "Endpoint 5xx (status %s) after %d retries: %s%s",
                            status,
                            max_server_retries,
                            exc,
                            _error_detail(exc),
                        )
                        raise
                    wait = _SERVER_RETRY_WAITS[server_retries] * random.uniform(
                        0.8,
                        1.2,
                    )
                    server_retries += 1
                    logger.warning(
                        "Transient 5xx (status %s, attempt %d/%d), retrying after %.1fs: %s%s",
                        status,
                        server_retries,
                        max_server_retries,
                        wait,
                        exc,
                        _error_detail(exc),
                    )
                    _sleep_or_cancel(wait, cancel_event)
                    continue
                if not using_backup and supervisor._model_backup:
                    logger.warning(
                        "Primary model %s failed (%s), falling back to backup %s",
                        supervisor._model,
                        exc,
                        supervisor._model_backup,
                    )
                    using_backup = True
                    attempt = 0
                    continue
                raise

    except JudgeCancelled:
        raise
    except Exception as exc:
        _record_judge_failure(
            supervisor,
            exc,
            model=current_model,
            extra_body=create_kwargs.get("extra_body") if create_kwargs else None,
            messages=working_messages,
            attempt=max(call_index, 1),
            elapsed=max(0.0, time.monotonic() - attempt_started),
            total_elapsed=max(0.0, time.monotonic() - total_started),
        )
        raise
    if should_record_user:
        supervisor._history.append({"role": "user", "content": record_content})
    supervisor._history.append({"role": "assistant", "content": reply})

    supervisor._extract_and_store_opencode_output(record_content, should_record_user)

    if len(supervisor._history) > supervisor._max_history_turns * 2:
        if supervisor._compact_intermediate_steps:
            supervisor.compact_history()
        else:
            supervisor._history = supervisor._history[:2] + supervisor._history[4:]

    structured_done, criteria, next_action, evidence_requests, structured = (
        parse_verdict_structure_ex(reply)
    )
    skill_requests = parse_skill_requests(reply)
    criteria, validation_notes = flag_contradicted_criteria(criteria)
    if structured_done is not None:
        all_met = structured_done and not validation_notes
    else:
        # No structured block: fail closed. The old fallback matched phrases
        # like "task complete" in free text, so a judge that ignored the
        # verdict format could end a run on prose alone. One repair attempt
        # asks for the block again; if that also fails, the verdict stays
        # "not done" and the caller sees unstructured=True.
        repaired = _request_structured_verdict(supervisor, working_messages, reply)
        if repaired is not None:
            reply = repaired
            structured_done, criteria, next_action, evidence_requests, structured = (
                parse_verdict_structure_ex(reply)
            )
            skill_requests = parse_skill_requests(reply)
            criteria, validation_notes = flag_contradicted_criteria(criteria)
        all_met = bool(structured_done) and not validation_notes
    return SupervisorVerdict(
        raw=reply,
        all_targets_met=all_met,
        feedback=reply,
        criteria_results=criteria,
        next_action=next_action,
        evidence_requests=evidence_requests,
        skill_requests=skill_requests,
        validation_notes=validation_notes,
        unstructured=not structured,
    )


def _request_structured_verdict(
    supervisor,
    messages: list[dict],
    reply: str,
) -> str | None:
    """Ask once for the structured verdict block; None when it still fails.

    The judge is told exactly what was missing, so a model that simply forgot
    the format recovers. A model that cannot produce it is not trusted to
    declare completion, which is why the caller keeps ``all_met=False``.
    """
    from supervisor.prompts import VERDICT_FORMAT

    repair_messages = [
        *messages,
        {"role": "assistant", "content": reply},
        {
            "role": "user",
            "content": (
                "Your reply had no DONE: line, so it cannot be parsed as a "
                "verdict. Re-emit ONLY the structured block, exactly in this "
                "format:\n\n" + VERDICT_FORMAT
            ),
        },
    ]
    body = None
    repair_started = time.monotonic()
    try:
        repair_kwargs = {
            "model": supervisor._model,
            "messages": repair_messages,
        }
        # Mechanical re-emit of a fixed block: use the measured cheapest level
        # rather than the judge's configured effort. Falling back to the
        # provider default (the old behaviour) could mean minutes of reasoning
        # for a one-line format fix.
        mechanical = getattr(supervisor, "mechanical_reasoning_body", None)
        body = mechanical() if callable(mechanical) else None
        if body:
            repair_kwargs["extra_body"] = body
        with judge_slot():
            response = supervisor._client.chat.completions.create(**repair_kwargs)
    except Exception as exc:  # noqa: BLE001 - a failed repair is not a verdict
        logger.warning("structured-verdict repair call failed: %s", exc)
        _record_judge_failure(
            supervisor,
            exc,
            model=supervisor._model,
            extra_body=body if body else None,
            messages=repair_messages,
            attempt=1,
            elapsed=max(0.0, time.monotonic() - repair_started),
            total_elapsed=max(0.0, time.monotonic() - repair_started),
        )
        return None
    if not response.choices:
        return None
    repaired = normalize_model_response(
        response.choices[0].message.content,
        "supervisor response",
    )
    _done, _criteria, _next, _requests, structured = parse_verdict_structure_ex(
        repaired,
    )
    if not structured:
        logger.warning(
            "judge reply still has no structured DONE line after one repair "
            "attempt; treating the verdict as not done",
        )
        return None
    return repaired


def fit_request_to_budget(supervisor, user_content: str) -> str:
    """Shrink history and user content to fit within token budget.

    When the agent's evidence (user content) must be cut, a visible
    truncation receipt is appended so the judge knows it is ruling on a
    partial excerpt — verdicts must treat everything outside the excerpt
    as UNSEEN instead of confidently marking targets [MET] on evidence
    that never reached the model.
    """
    from supervisor.monitoring.token_estimator import estimate_tokens, truncate_prompt

    available = int(supervisor._max_tokens * 0.75)
    system_tokens = estimate_tokens(supervisor._system)
    # Even a pathologically large system prompt must leave the evidence a
    # real slice: floor at a quarter of the available budget, not 1/8 of
    # the raw max (which once shredded a 98k-token agent output to 16k).
    budget = max(available - system_tokens, available // 4)

    def _msg_tokens(msg: dict) -> int:
        return estimate_tokens(msg.get("content", ""))

    history_tokens = sum(_msg_tokens(m) for m in supervisor._history)
    user_tokens = estimate_tokens(user_content)
    if history_tokens + user_tokens <= budget:
        return user_content

    dropped_any = False
    while supervisor._history and history_tokens + user_tokens > budget:
        if len(supervisor._history) <= 2:
            break
        removed = supervisor._history.pop(0)
        history_tokens -= _msg_tokens(removed)
        dropped_any = True
        if (
            removed.get("role") == "user"
            and supervisor._history
            and supervisor._history[0].get("role") == "assistant"
        ):
            removed2 = supervisor._history.pop(0)
            history_tokens -= _msg_tokens(removed2)

    per_msg_cap = max(budget // 4, 2000)
    if history_tokens + user_tokens > budget:
        for msg in supervisor._history:
            if _msg_tokens(msg) > per_msg_cap:
                original = msg.get("content", "")
                msg["content"] = truncate_prompt(original, per_msg_cap)
        history_tokens = sum(_msg_tokens(m) for m in supervisor._history)

    if history_tokens + user_tokens > budget:
        remaining_for_user = max(budget - history_tokens, budget // 4)
        original_chars = len(user_content)
        original_tokens = user_tokens
        user_content = truncate_prompt(user_content, remaining_for_user)
        if len(user_content) < original_chars:
            user_content += (
                "\n\n[SYSTEM NOTE — PARTIAL EVIDENCE: the coding agent's output "
                f"above was truncated from ~{original_tokens} to "
                f"~{estimate_tokens(user_content)} tokens to fit the judge-model "
                "budget. Everything NOT shown must be treated as UNSEEN: never "
                "mark a target [MET] on the strength of evidence outside this "
                "excerpt — state what evidence you need instead.]"
            )

    if dropped_any:
        logger.warning(
            "Proactively trimmed history to fit budget (%d tokens, %d messages left)",
            budget,
            len(supervisor._history),
        )
    return user_content


def truncate_older_turns(messages: list[dict]) -> list[dict]:
    if len(messages) <= 2:
        return messages

    result = [messages[0]]
    remaining = messages[1:]
    skip = 0
    if remaining and remaining[0].get("role") == "user":
        skip = 1
        if len(remaining) > 1 and remaining[1].get("role") == "assistant":
            skip = 2
    elif remaining and remaining[0].get("role") == "assistant":
        skip = 1

    result.extend(remaining[skip:])
    return result


def truncate_messages_for_limit(
    messages: list[dict],
    model_limit: int,
) -> list[dict]:
    """Truncate messages to fit within specific model token limit."""
    from supervisor.monitoring.token_estimator import estimate_tokens

    if len(messages) <= 1:
        return messages

    result = [messages[0]]
    remaining = messages[1:]

    system_tokens = estimate_tokens(messages[0].get("content", ""))
    target_budget = int(model_limit * 0.75) - system_tokens
    if target_budget <= 0:
        logger.warning(
            "System prompt exceeds 75% of model limit, returning minimal messages",
        )
        if len(messages) > 1:
            last_user = None
            for msg in reversed(messages):
                if msg.get("role") == "user":
                    last_user = msg
                    break
            if last_user:
                truncated_content = truncate_with_fallback(
                    last_user.get("content", ""),
                    model_limit // 2,
                )
                return [
                    messages[0],
                    {"role": "user", "content": truncated_content},
                ]
        return messages[:1]

    current_tokens = 0
    preserved = []
    for msg in reversed(remaining):
        msg_tokens = estimate_tokens(msg.get("content", ""))
        if current_tokens + msg_tokens <= target_budget:
            preserved.insert(0, msg)
            current_tokens += msg_tokens

    result.extend(preserved)
    return result
