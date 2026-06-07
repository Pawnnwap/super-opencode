from __future__ import annotations

import re
import time
from pathlib import Path

import streamlit as st

from supervisor.utils.text_utils import sanitize_event_message

# Levels hidden from the log box unless the user opts in ("Show noise"). These
# are high-volume bookkeeping events: heartbeats, per-step progress pings, and
# the structured token events that feed the usage bar.
_DEFAULT_HIDDEN_LEVELS = ("heartbeat", "step_progress", "tokens")

# Small icons prepended to a few plain (non-block) levels in the log box.
_LEVEL_ICONS = {
    "tool": "🔧",
    "warn": "⚠️",
    "error": "❌",
    "success": "✅",
}

# Phase pipeline shown as a breadcrumb above the live log.
_PHASE_SEQUENCE = [
    ("plan", "planning"),
    ("code", "coding"),
    ("test", "testing"),
    ("review", "review"),
    ("done", "completing"),
]


def safe_logs(status: dict) -> list[dict]:
    return status.get("logs") or []


def esc(text) -> str:
    if text is None:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── pure helpers (unit-tested in tests/test_log_ui_helpers.py) ────────────────


def _fmt_ts(event: dict) -> str:
    """Format an event's logged timestamp as HH:MM:SS, or '' if absent."""
    ts = event.get("ts")
    if not isinstance(ts, (int, float)) or ts <= 0:
        return ""
    return time.strftime("%H:%M:%S", time.localtime(ts))


def _fmt_elapsed(ts, start_ts) -> str:
    """Format ts relative to start_ts as +Ns / +Mm SSs / +Hh MMm, or ''."""
    if not isinstance(ts, (int, float)) or not isinstance(start_ts, (int, float)):
        return ""
    if ts <= 0 or start_ts <= 0:
        return ""
    sec = int(max(0, ts - start_ts))
    if sec < 60:
        return f"+{sec}s"
    mins, secs = divmod(sec, 60)
    if mins < 60:
        return f"+{mins}m{secs:02d}s"
    hours, mins = divmod(mins, 60)
    return f"+{hours}h{mins:02d}m"


def _event_counts(events: list) -> dict:
    """Count error / warn / tool events for the health badge."""
    counts = {"error": 0, "warn": 0, "tool": 0}
    for event in events:
        if isinstance(event, dict):
            level = event.get("level")
            if level in counts:
                counts[level] += 1
    return counts


def _start_ts(events: list) -> float | None:
    """Earliest event timestamp (run start) for relative-time display."""
    start = None
    for event in events:
        if not isinstance(event, dict):
            continue
        ts = event.get("ts")
        if isinstance(ts, (int, float)) and ts > 0:
            start = ts if start is None else min(start, ts)
    return start


def _filter_events(events: list, search: str, skip: set | None) -> list[dict]:
    """Filter events by hidden levels (skip) and a case-insensitive search."""
    out: list[dict] = []
    needle = (search or "").lower()
    skip = skip or set()
    for event in events:
        if not isinstance(event, dict):
            continue
        if (event.get("level") or "info") in skip:
            continue
        if needle:
            msg = sanitize_event_message(event.get("msg") or "")
            if needle not in msg.lower():
                continue
        out.append(event)
    return out


def _latest_token_current(logs: list) -> int | None:
    """Latest structured token count from `tokens` events (None if none)."""
    current = None
    for event in logs:
        if not isinstance(event, dict) or event.get("level") != "tokens":
            continue
        value = event.get("current")
        if isinstance(value, (int, float)) and value > 0:
            current = int(value)
    return current


def _regex_token_current(logs: list, max_tokens: int) -> tuple[int, float] | None:
    """Fallback: scrape 'N / M tokens' out of context-usage warning strings."""
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
    return (latest_current, latest_fraction) if found else None


def _phase_breadcrumb(current_phase: str, completed_phases: list | None) -> str:
    """Markdown breadcrumb 'plan → code → test → review' with current bold."""
    current = (current_phase or "").lower()
    done = {str(p).lower() for p in (completed_phases or [])}
    parts: list[str] = []
    for label, name in _PHASE_SEQUENCE:
        if name == current:
            parts.append(f"**{label}**")
        elif name in done:
            parts.append(f"~~{label}~~")
        else:
            parts.append(label)
    return " → ".join(parts)


# ── rendering ─────────────────────────────────────────────────────────────────


_SUPERVISOR_LABELS = {
    "supervisor_response": "🧠 SUPERVISOR",
    "supervisor_read_files": "📂 SUPERVISOR READ FILES",
}


def _block_labels(engine: str) -> dict[str, str]:
    """Build block labels, naming the active execution engine (opencode|codex)."""
    name = engine or "opencode"
    return {
        "opencode_prompt": f"▶ PROMPT → {name}",
        "opencode_output": f"◀ OUTPUT ← {name}",
        **_SUPERVISOR_LABELS,
    }


def _meta_html(event: dict, start_ts) -> str:
    """Leading 'HH:MM:SS +elapsed' span for a log line ('' if no timestamp)."""
    ts = _fmt_ts(event)
    if not ts:
        return ""
    html = f'<span class="log-ts">{ts}</span>'
    elapsed = _fmt_elapsed(event.get("ts"), start_ts)
    if elapsed:
        html += f'<span class="log-elapsed"> {elapsed}</span>'
    return html + "  "


def render_events(
    events: list[dict],
    empty_msg: str,
    skip: set | None = None,
    show_verbose: bool = True,
    page_key: str = "default",
    engine: str = "opencode",
) -> None:
    events = events or []
    block_labels = _block_labels(engine)

    verbose = st.session_state.get("verbose_log", True)
    search = ""
    newest_first = False
    show_noise = False

    if show_verbose:
        top1, top2 = st.columns([3, 2])
        with top1:
            search = st.text_input(
                "Search logs",
                key=f"vsearch_{page_key}",
                label_visibility="collapsed",
                placeholder="🔍 Search logs…",
            )
        with top2:
            counts = _event_counts(events)
            st.caption(
                f"🔴 {counts['error']}  ·  🟡 {counts['warn']}  ·  🔧 {counts['tool']}",
            )
        ctrl1, ctrl2, ctrl3, ctrl4 = st.columns(4)
        with ctrl1:
            st.session_state.verbose_log = st.toggle(
                "Verbose", value=verbose, key=f"vtoggle_{page_key}",
            )
            verbose = st.session_state.verbose_log
        with ctrl2:
            newest_first = st.toggle("Newest first", value=False, key=f"vnewest_{page_key}")
        with ctrl3:
            show_noise = st.toggle(
                "Show noise",
                value=False,
                key=f"vnoise_{page_key}",
                help="Heartbeats, step-progress pings, and token events.",
            )
        with ctrl4:
            if events:
                dump = "\n".join(
                    f"[{_fmt_ts(e) or '--:--:--'}] {e.get('level', 'info')}: "
                    f"{sanitize_event_message(e.get('msg') or '')}"
                    for e in events
                    if isinstance(e, dict)
                )
                st.download_button(
                    "⬇ Log",
                    data=dump,
                    file_name=f"log_{page_key}.txt",
                    mime="text/plain",
                    key=f"dl_{page_key}",
                    use_container_width=True,
                )

    hidden = set(skip or set())
    if not show_noise:
        hidden |= set(_DEFAULT_HIDDEN_LEVELS)

    filtered = _filter_events(events, search, hidden)
    if not filtered:
        st.markdown(
            f'<div class="log-box"><span class="log-info">{esc(empty_msg)}</span></div>',
            unsafe_allow_html=True,
        )
        return

    start_ts = _start_ts(events)
    show = filtered[-600:]
    if newest_first:
        show = list(reversed(show))

    lines_html: list[str] = []
    for event in show:
        level = event.get("level") or "info"
        msg = sanitize_event_message(event.get("msg") or "")
        meta = _meta_html(event, start_ts)

        if level in block_labels:
            header = block_labels[level]
            if not verbose:
                preview = esc(str(msg)[:120].replace("\n", " "))
                lines_html.append(
                    f'{meta}<span class="log-block-hdr">{header}</span>'
                    f'<span class="log-info" style="opacity:0.6"> {preview}…</span>\n'
                )
            else:
                # Collapsible block (native <details>, no JS) so big
                # PROMPT/OUTPUT dumps can be folded away.
                lines_html.append(
                    f'<span class="log-rule">{"─" * 60}</span>\n'
                    f'<details class="log-details" open>'
                    f'<summary>{meta}<span class="log-block-hdr">{header}</span></summary>'
                    f'<span class="log-{esc(level)}">{esc(msg)}</span>'
                    f"</details>\n"
                )
        else:
            icon = _LEVEL_ICONS.get(level, "")
            icon_html = f"{icon} " if icon else ""
            lines_html.append(
                f'{meta}<span class="log-{esc(level)}">{icon_html}{esc(msg)}</span>\n',
            )

    st.markdown(
        f'<div class="log-box">{"".join(lines_html)}</div>',
        unsafe_allow_html=True,
    )


def render_token_usage_bar(logs: list[dict], max_tokens: int) -> None:
    current = _latest_token_current(logs)
    fraction = None
    if current is not None and max_tokens > 0:
        fraction = current / max_tokens
    else:
        fallback = _regex_token_current(logs, max_tokens)
        if fallback is not None:
            current, fraction = fallback

    if current is None or fraction is None:
        return

    color = "🔴" if fraction > 0.9 else "🟡" if fraction > 0.7 else "🟢"
    st.progress(
        min(fraction, 1.0),
        text=f"{color} {current:,} / {max_tokens:,} tokens",
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

    last_progress = progress_events[-1] if progress_events else None
    if last_progress is not None:
        st.caption(
            _phase_breadcrumb(
                last_progress.get("phase", ""),
                last_progress.get("completed_phases"),
            ),
        )

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
    error_count = 0
    for event in logs:
        if isinstance(event, dict) and event.get("level") == "error":
            error_count += 1
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
        if error_count:
            meta_bits.append(f"❌ {error_count}")
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
