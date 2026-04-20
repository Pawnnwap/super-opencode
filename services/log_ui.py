from __future__ import annotations

import time
from pathlib import Path

import streamlit as st

from supervisor.utils.text_utils import sanitize_event_message


def safe_logs(status: dict) -> list[dict]:
    return status.get("logs") or []


def esc(text) -> str:
    if text is None:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_BLOCK_LABELS = {
    "opencode_prompt": "▶ PROMPT → opencode",
    "opencode_output": "◀ OUTPUT ← opencode",
    "supervisor_response": "🧠 SUPERVISOR",
    "supervisor_read_files": "📂 SUPERVISOR READ FILES",
}


def render_events(
    events: list[dict],
    empty_msg: str,
    skip: set | None = None,
    show_verbose: bool = True,
    page_key: str = "default",
) -> None:
    events = events or []
    skip = skip or set()
    verbose = st.session_state.get("verbose_log", True)

    if show_verbose:
        st.session_state.verbose_log = st.toggle(
            "Verbose log",
            value=verbose,
            key=f"vtoggle_{page_key}",
        )
        verbose = st.session_state.verbose_log

    if not events:
        st.markdown(
            f'<div class="log-box"><span class="log-info">{esc(empty_msg)}</span></div>',
            unsafe_allow_html=True,
        )
        return

    lines_html: list[str] = []
    for event in events[-600:]:
        if not isinstance(event, dict):
            continue
        level = event.get("level") or "info"
        if level in skip:
            continue
        msg = sanitize_event_message(event.get("msg") or "")

        if level in _BLOCK_LABELS:
            header = _BLOCK_LABELS[level]
            if not verbose:
                preview = esc(str(msg)[:120].replace("\n", " "))
                lines_html.append(
                    f'<span class="log-block-hdr">{header}</span>'
                    f'<span class="log-info" style="opacity:0.6"> {preview}…</span>\n'
                )
            else:
                lines_html.append(
                    f'<span class="log-rule">{"─" * 60}</span>\n'
                    f'<span class="log-block-hdr">{header}</span>\n'
                    f'<span class="log-{esc(level)}">{esc(msg)}</span>\n'
                )
        else:
            lines_html.append(
                f'<span class="log-{esc(level)}">{esc(msg)}</span>\n'
            )

    st.markdown(
        f'<div class="log-box">{"".join(lines_html)}</div>',
        unsafe_allow_html=True,
    )


def render_token_usage_bar(logs: list[dict], max_tokens: int) -> None:
    import re

    latest_current, latest_fraction, found = 0, 0.0, False
    for event in logs:
        if not isinstance(event, dict):
            continue
        msg = event.get("msg") or ""
        if "context usage" not in msg.lower():
            continue
        match = re.search(r"(\d[\d,]*)\s*/\s*(\d[\d,]*)\s*tokens", msg)
        if not match:
            continue
        current = int(match.group(1).replace(",", ""))
        max_t = int(match.group(2).replace(",", ""))
        fraction = current / max_t if max_t > 0 else 0
        if fraction >= latest_fraction:
            latest_fraction, latest_current, found = fraction, current, True

    if found:
        color = "🔴" if latest_fraction > 0.9 else "🟡" if latest_fraction > 0.7 else "🟢"
        st.progress(
            min(latest_fraction, 1.0),
            text=f"{color} {latest_current:,} / {max_tokens:,} tokens",
        )


def render_step_progress(
    logs: list[dict],
    run_state: str,
    is_evolution: bool = False,
) -> None:
    logs = logs or []
    step_events = [
        event
        for event in logs
        if isinstance(event, dict)
        and event.get("level") in ("step", "phase_transition")
    ]
    progress_events = [
        event
        for event in logs
        if isinstance(event, dict) and event.get("level") == "step_progress"
    ]
    heartbeat_events = [
        event
        for event in logs
        if isinstance(event, dict) and event.get("level") == "heartbeat"
    ]
    process_label = "Evolution process active" if is_evolution else "Background process active"

    if run_state == "RUNNING":
        col1, col2, col3 = st.columns([3, 1, 1])
        with col1:
            st.markdown(f"🟢 **{process_label}**")
        with col2:
            st.caption(f"💓 {len(heartbeat_events)} heartbeat(s)")
        with col3:
            st.caption(f"🧭 {len(step_events)} step(s)")
        if progress_events:
            with st.expander("📊 Progress"):
                st.caption(progress_events[-1].get("msg") or "")
        return

    if not progress_events:
        return

    last_progress = progress_events[-1]
    col1, col2, col3 = st.columns([3, 1, 1])
    with col1:
        st.caption(f"📊 {last_progress.get('msg') or ''}")
    with col2:
        st.caption(f"🧭 {len(step_events)} step(s)")
    with col3:
        if heartbeat_events:
            st.caption("🟢 active")

    progress_val = 0.0
    if "percentage" in last_progress and last_progress["percentage"] is not None:
        try:
            progress_val = float(last_progress["percentage"])
        except (TypeError, ValueError):
            pass
    else:
        for part in (last_progress.get("msg") or "").split():
            candidate = part.replace("%", "").replace(".", "")
            if not candidate.isdigit():
                continue
            try:
                progress_val = float(part.replace("%", ""))
                break
            except ValueError:
                pass

    if progress_val > 0:
        col1, _ = st.columns([4, 1])
        with col1:
            st.progress(progress_val / 100.0, text=f"{progress_val:.0f}% complete")

    if step_events:
        with st.expander("📍 Step History", expanded=False):
            for event in step_events[-5:]:
                if event.get("level") == "step":
                    st.caption(f"• {(event.get('msg') or '')[:80]}")
                elif event.get("level") == "phase_transition":
                    st.caption(f"⚡ {event.get('msg') or ''}")


def format_status_pill(state: str, pill_map: dict[str, str]) -> str:
    return pill_map.get(state, f'<span class="pill pill-idle">{state}</span>')


def render_job_card(
    *,
    job_manager,
    job: dict,
    pill_map: dict[str, str],
    query_param: str,
) -> None:
    job_id = job["id"]
    status = job["status"]
    state = status.get("state", "UNKNOWN")
    config = status.get("config", {})
    workspace = config.get("workspace", "")

    logs = safe_logs(status)
    last_event_msg = ""
    for event in reversed(logs):
        if not isinstance(event, dict):
            continue
        if event.get("level") == "heartbeat":
            continue
        msg = (event.get("msg") or "").strip().splitlines()[0:1]
        if msg:
            last_event_msg = msg[0][:120]
            break

    started_at = status.get("updated_at") or 0
    elapsed_txt = ""
    if started_at and state == "RUNNING":
        elapsed = max(0, time.time() - started_at)
        mins, secs = divmod(int(elapsed), 60)
        elapsed_txt = f"⏱ {mins}m {secs:02d}s"

    col1, col2, col3 = st.columns([3, 2, 1])
    with col1:
        pill = format_status_pill(state, pill_map)
        st.markdown(f"`{job_id}` {pill} ", unsafe_allow_html=True)
        meta_bits = []
        if workspace:
            meta_bits.append(f"📁 `{Path(workspace).name}`")
        if elapsed_txt:
            meta_bits.append(elapsed_txt)
        if meta_bits:
            st.caption(" · ".join(meta_bits))
        if last_event_msg:
            st.caption(f"› {last_event_msg}")
    with col2:
        if state == "RUNNING":
            if st.button("Stop", key=f"stop_card_{job_id}"):
                job_manager.cancel_job(job_id)
                st.rerun()
        elif state in ("SUCCESS", "FAILED", "CANCELLED"):
            if st.button("Delete", key=f"delete_card_{job_id}"):
                job_manager.store.delete_job(job_id)
                st.rerun()
    with col3:
        if st.button("View", key=f"view_card_{job_id}"):
            st.query_params[query_param] = job_id
            st.rerun()
