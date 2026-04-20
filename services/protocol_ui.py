from __future__ import annotations

from pathlib import Path

import streamlit as st

from supervisor.protocols.protocol_analyzer import ProtocolAnalyzer, Severity


def render_quality_metrics(analysis) -> None:
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Overall", f"{analysis.overall_score:.0%}")
    with col2:
        st.metric("INPUT", f"{analysis.input_score.overall:.0%}")
    with col3:
        st.metric("TARGET", f"{analysis.target_score.overall:.0%}")
    with col4:
        st.metric("RESTRICTIONS", f"{analysis.restrictions_score.overall:.0%}")


def render_protocol_quality(text: str, detailed: bool = False) -> None:
    analyzer = ProtocolAnalyzer()
    try:
        analysis = analyzer.analyze_text(text)
    except Exception as exc:
        st.caption(
            "Complete all three sections to see quality scores."
            if not detailed
            else f"Cannot analyze protocol: {exc}"
        )
        return

    render_quality_metrics(analysis)
    if detailed:
        rating_colors = {
            "excellent": "🟢",
            "good": "🟡",
            "fair": "🟠",
            "poor": "🔴",
        }
        color = rating_colors.get(analysis.quality_rating, "⚪")
        st.caption(f"{color} Quality: {analysis.quality_rating}")

    if not analysis.issues:
        return

    if not detailed:
        st.caption(f"Found {len(analysis.issues)} issue(s)")
        for issue in analysis.issues[:5]:
            icon = {"error": "❌", "warning": "⚠️", "info": "ℹ️"}[issue.severity.value]
            st.caption(f"{icon} [{issue.section}] {issue.message}")
        return

    errors = [issue for issue in analysis.issues if issue.severity == Severity.ERROR]
    warnings = [
        issue for issue in analysis.issues if issue.severity == Severity.WARNING
    ]
    infos = [issue for issue in analysis.issues if issue.severity == Severity.INFO]

    if errors:
        st.error(f"{len(errors)} error(s) found")
        for issue in errors:
            st.caption(f"❌ [{issue.section}] {issue.message}")
            if issue.suggestion:
                st.caption(f"   → {issue.suggestion}")
    if warnings:
        st.warning(f"{len(warnings)} warning(s)")
        for issue in warnings:
            st.caption(f"⚠️ [{issue.section}] {issue.message}")
    if infos:
        with st.expander(f"{len(infos)} suggestion(s)"):
            for issue in infos:
                st.caption(f"ℹ️ [{issue.section}] {issue.message}")
                if issue.suggestion:
                    st.caption(f"   → {issue.suggestion}")


def render_existing_protocol_banner(
    proto_path: Path,
    state_key: str,
    reuse_label: str = "♻️  Use existing protocol.md",
    on_reuse=None,
) -> bool:
    if not (proto_path.exists() and not st.session_state.get(state_key)):
        return False

    existing_text = proto_path.read_text(encoding="utf-8")
    fname = proto_path.name
    st.info(f"📄 An existing `{fname}` was found.")
    col_reuse, col_ignore, _ = st.columns([1, 1, 3])
    with col_reuse:
        if st.button(reuse_label, type="primary", key=f"btn_reuse_{state_key}"):
            if on_reuse:
                on_reuse(existing_text)
            else:
                st.session_state[state_key] = existing_text
                st.rerun()
    with col_ignore:
        st.button("✏️  Write new one", key=f"btn_ignore_{state_key}")
    with st.expander(f"Preview existing {fname}"):
        st.code(existing_text[:1500], language="markdown")
    st.markdown("---")
    return True


def save_protocol(workspace: Path, protocol_md: str) -> Path:
    workspace.mkdir(parents=True, exist_ok=True)
    proto_path = workspace / "protocol.md"
    proto_path.write_text(protocol_md, encoding="utf-8")
    return proto_path
