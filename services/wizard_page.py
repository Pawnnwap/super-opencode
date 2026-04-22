from __future__ import annotations

import contextlib
from pathlib import Path

import streamlit as st
from openai import OpenAI

from services.connectivity import test_opencode_connectivity, test_supervisor_connectivity
from services.opencode_config import fetch_opencode_models
from services.protocol_ui import (
    render_existing_protocol_banner,
    render_protocol_quality,
    save_protocol,
)
from services.settings import apply_api_config, save_settings
from services.workspace_cleanup import clean_workspace_artifacts
from supervisor.monitoring.session_tracker import estimate_tokens
from supervisor.protocols.protocol_wizard import ProtocolWizard
from supervisor.utils.text_utils import normalize_model_response


@contextlib.contextmanager
def _card():
    st.markdown('<div class="card">', unsafe_allow_html=True)
    yield
    st.markdown("</div>", unsafe_allow_html=True)


def _render_test_result(test_name: str, ok: bool, msg: str) -> None:
    (st.success if ok else st.error)(f"{'OK' if ok else 'X'} Test {test_name}: {msg}")


def _do_test(test_name: str, test_fn, state_key: str) -> None:
    with st.spinner(f"Testing {test_name}..."):
        ok, msg = test_fn()
    st.session_state[state_key] = ok
    _render_test_result(test_name, ok, msg)


def _render_model_selector(
    label: str,
    models: list[str],
    session_key: str,
    select_key: str,
    fallback_key: str,
    *,
    help_text: str | None = None,
    placeholder: str | None = None,
    show_warning: bool = True,
) -> str:
    if models:
        current = st.session_state.get(session_key, "")
        default_idx = models.index(current) if current in models else 0
        st.session_state[session_key] = st.selectbox(
            label,
            options=models,
            index=default_idx,
            key=select_key,
            help=help_text,
        )
    else:
        if show_warning:
            st.warning("No models returned by `opencode models`.")
        st.session_state[session_key] = st.text_input(
            label,
            key=fallback_key,
            value=st.session_state.get(session_key, ""),
            placeholder=placeholder,
        )
    return st.session_state[session_key]


def bound_text_input(
    label: str,
    session_key: str,
    *,
    placeholder: str | None = None,
    type: str = "default",
    help_text: str | None = None,
) -> str:
    st.session_state[session_key] = st.text_input(
        label,
        key=f"cfg_{session_key}",
        value=st.session_state.get(session_key, ""),
        placeholder=placeholder,
        type=type,
        help=help_text,
    )
    return st.session_state[session_key]


def _test_opencode() -> tuple[bool, str]:
    return test_opencode_connectivity(
        st.session_state.opencode_executable,
        st.session_state.opencode_model,
        st.session_state.opencode_model_backup,
    )


def _test_supervisor() -> tuple[bool, str]:
    return test_supervisor_connectivity(
        st.session_state.openai_key,
        st.session_state.supervisor_model or "gpt-4o",
        base_url=st.session_state.base_url or None,
    )


def _render_connectivity_tests() -> None:
    st.markdown("---")
    st.markdown("### Connectivity Tests")
    both_passed = st.session_state.opencode_test_passed and st.session_state.supervisor_test_passed
    if both_passed:
        st.success("Both opencode and supervisor connectivity tests passed.")
    else:
        st.info("Run tests below to verify opencode and supervisor are reachable.")

    col_t1, col_t2, col_t3 = st.columns(3)
    with col_t1:
        if st.button("Run Tests", type="primary", key="btn_run_tests"):
            if not st.session_state.workspace:
                st.error("Set workspace path before running tests.")
            else:
                _do_test("opencode", _test_opencode, "opencode_test_passed")
                _do_test("Supervisor", _test_supervisor, "supervisor_test_passed")
    with col_t2:
        if st.button("Test opencode", key="btn_test_opencode"):
            if not st.session_state.workspace:
                st.error("Set workspace path before testing.")
            else:
                _do_test("opencode", _test_opencode, "opencode_test_passed")
    with col_t3:
        if st.button("Test Supervisor", key="btn_test_supervisor"):
            if not st.session_state.openai_key:
                st.error("Set API key before testing.")
            else:
                _do_test("Supervisor", _test_supervisor, "supervisor_test_passed")


def _save_protocol() -> None:
    proto_path = save_protocol(Path(st.session_state.workspace), st.session_state.protocol_md)
    st.session_state.protocol_saved_path = str(proto_path)


def page_wizard() -> None:
    from supervisor.runners.opencode_runner import find_opencode

    if st.session_state.get("_redirect_warning"):
        st.warning(st.session_state.pop("_redirect_warning"))

    st.markdown("# Protocol Wizard")
    st.markdown(
        "Fill each section in plain language. Supervisor LLM will refine "
        "them into clean, unambiguous `protocol.md`.",
    )

    try:
        find_opencode()
    except FileNotFoundError as exc:
        st.error(str(exc))
        st.stop()

    with st.expander("Configuration", expanded=st.session_state.wizard_step == 0):
        col1, col2 = st.columns(2)
        with col1:
            bound_text_input("API Key", "openai_key", placeholder="sk-...", type="password")
            bound_text_input(
                "Base URL (leave blank for OpenAI)",
                "base_url",
                placeholder="e.g. http://localhost:11434/v1",
            )
            bound_text_input(
                "Workspace path (absolute)",
                "workspace",
                placeholder="/home/user/myproject",
            )
            if st.session_state.workspace != st.session_state.get("_last_workspace", ""):
                st.session_state.protected_files = []
                st.session_state._last_workspace = st.session_state.workspace
                st.session_state["_artifact_clean_done"] = False
            if not st.session_state.get("_artifact_clean_done"):
                workspace_raw = st.session_state.get("workspace", "")
                if workspace_raw:
                    clean_workspace_artifacts(Path(workspace_raw))
                st.session_state["_artifact_clean_done"] = True
            bound_text_input(
                "Supervisor / wizard model",
                "supervisor_model",
                placeholder="e.g. gpt-4o, claude-3-5-sonnet, mistral-large",
            )
            bound_text_input(
                "Supervisor model backup",
                "supervisor_model_backup",
                placeholder="e.g. gpt-4o-mini (used when primary fails)",
            )

        with col2:
            models = st.session_state.get("opencode_models", [])
            _render_model_selector(
                "Model",
                models,
                "opencode_model",
                "cfg_opencode_model_select",
                "cfg_opencode_model_fallback",
                help_text="Models returned by `opencode models`",
            )

            backup_models = [model for model in models if model != st.session_state.opencode_model] if models else []
            _render_model_selector(
                "opencode model backup",
                backup_models,
                "opencode_model_backup",
                "cfg_opencode_model_backup_select",
                "cfg_opencode_model_backup",
                help_text="Fallback model used when primary model fails",
                placeholder="e.g. /my-provider/backup-model",
                show_warning=False,
            )

            def _refresh_models():
                st.session_state["opencode_models"] = fetch_opencode_models()

            st.button("Refresh models", key="btn_refresh_models", on_click=_refresh_models)

            st.session_state.max_retries = st.number_input(
                "Max retries",
                key="cfg_max_retries",
                min_value=1,
                max_value=20,
                value=int(st.session_state.max_retries),
            )
            st.session_state.context_threshold = st.slider(
                "Context compaction threshold (%)",
                20,
                95,
                key="cfg_ctx_threshold",
                value=int(st.session_state.context_threshold),
            )
            st.session_state.max_tokens = st.number_input(
                "Max tokens (model context window)",
                key="cfg_max_tokens",
                min_value=1000,
                max_value=1000000,
                value=int(st.session_state.max_tokens),
                step=1000,
            )
            st.session_state.timeout = st.number_input(
                "Timeout (min)",
                key="cfg_timeout",
                min_value=1,
                max_value=999,
                value=min(max(int(st.session_state.timeout), 1), 999),
            )
            st.session_state.enable_python_scanner = st.toggle(
                "Enable Python scanner",
                key="cfg_enable_python_scanner",
                value=bool(st.session_state.enable_python_scanner),
                help="Run Python vulnerability scanner before each live run",
            )

        with st.expander("Protected Files", expanded=False):
            st.caption("Files opencode cannot modify or delete")
            protected = st.session_state.get("protected_files", [])
            if not isinstance(protected, list):
                protected = []
            workspace_path = Path(st.session_state.workspace) if st.session_state.workspace else None
            all_files = []
            if workspace_path and workspace_path.exists():
                try:
                    from supervisor.utils.path_filters import should_skip_path

                    all_files = sorted(
                        [
                            str(path.relative_to(workspace_path)).replace("\\", "/")
                            for path in workspace_path.rglob("*")
                            if path.is_file() and not should_skip_path(path, extra_dirs=["debug"])
                        ],
                    )
                except Exception:
                    pass

            available_files = [path for path in all_files if path not in set(protected)]
            st.markdown("**Add protected files:**")
            selected_to_add = st.multiselect(
                "Select files to protect",
                options=available_files,
                key="protected_files_multiselect",
                label_visibility="collapsed",
                placeholder="Choose files from workspace...",
            )
            if selected_to_add:
                st.session_state.protected_files = list(set(protected) | set(selected_to_add))
                st.rerun()
            if protected:
                st.success(f"{len(protected)} file(s) protected")
                for protected_file in protected:
                    col_pf1, col_pf2 = st.columns([4, 1])
                    with col_pf1:
                        st.code(protected_file, language=None)
                    with col_pf2:
                        if st.button("Remove", key=f"remove_pf_{protected_file}"):
                            st.session_state.protected_files = [item for item in protected if item != protected_file]
                            st.rerun()

        with st.expander("Ignore Patterns (.opencodeignore)", expanded=False):
            from supervisor.workspace.ignore_patterns import IGNORE_FILE, write_ignore_file

            st.caption("Files matching these patterns will be excluded from context retrieval")
            workspace_path = Path(st.session_state.workspace) if st.session_state.workspace else None
            if not workspace_path or not workspace_path.exists():
                st.warning("Set valid workspace path to edit .opencodeignore")
            else:
                ignore_file_path = workspace_path / IGNORE_FILE
                current_ignore_content = ""
                if ignore_file_path.exists():
                    try:
                        current_ignore_content = ignore_file_path.read_text(encoding="utf-8")
                    except Exception:
                        pass
                new_ignore_content = st.text_area(
                    "Ignore patterns",
                    value=current_ignore_content,
                    height=200,
                    key="ignore_patterns_editor",
                    placeholder=(
                        "# Patterns to ignore (one per line)\n"
                        "# Examples:\n"
                        "# *.pyc\n"
                        "# debug*\n"
                        "# *test.py\n"
                        "# build/\n"
                        "# **/*.log\n"
                    ),
                    label_visibility="collapsed",
                )
                if new_ignore_content != current_ignore_content:
                    if st.button("Save Ignore Patterns", key="save_ignore_patterns"):
                        try:
                            write_ignore_file(workspace_path, new_ignore_content)
                            st.success(f"Saved {IGNORE_FILE}")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"Failed to save: {exc}")
                if ignore_file_path.exists():
                    st.caption(f"Found existing {IGNORE_FILE} with {len(current_ignore_content.splitlines())} patterns")

                st.markdown("---")
                suggest_disabled = not (st.session_state.workspace and Path(st.session_state.workspace).exists())
                if st.button("Suggest and Apply Ignore Patterns", key="btn_suggest_ignore", disabled=suggest_disabled):
                    try:
                        workspace_root = Path(st.session_state.workspace)
                        all_entries = sorted(
                            [
                                str(path.relative_to(workspace_root)).replace("\\", "/")
                                for path in workspace_root.rglob("*")
                                if str(path.relative_to(workspace_root)) != ".opencodeignore"
                            ],
                        )
                        file_list_str = "\n".join(all_entries)
                        truncation_note = ""
                        if estimate_tokens(file_list_str) > 100000:
                            all_entries = all_entries[:1000]
                            file_list_str = "\n".join(all_entries)
                            truncation_note = (
                                "Note: file list was truncated to first 1,000 entries due to token limits."
                            )
                        client = OpenAI(
                            api_key=st.session_state.openai_key,
                            base_url=st.session_state.base_url or None,
                        )
                        model = st.session_state.supervisor_model or "gpt-4o"
                        system_msg = (
                            "Given a list of files and directories in a workspace, "
                            "generate a .opencodeignore file that ignores common build "
                            "artifacts, dependency directories, cache files, and other "
                            "files that should not be modified by an autonomous coding agent. "
                            "Patterns should be in gitignore format. Output patterns only."
                        )
                        user_msg = (
                            "The workspace contains the following files and directories:\n\n"
                            f"{file_list_str}"
                            + (f"\n\n{truncation_note}" if truncation_note else "")
                        )
                        response = client.chat.completions.create(
                            model=model,
                            messages=[
                                {"role": "system", "content": system_msg},
                                {"role": "user", "content": user_msg},
                            ],
                        )
                        generated_patterns = normalize_model_response(
                            response.choices[0].message.content,
                            "generated ignore patterns response",
                        )
                        st.text_area(
                            "Generated .opencodeignore patterns",
                            value=generated_patterns,
                            height=300,
                            key="generated_ignore_patterns",
                            disabled=True,
                        )
                        ignore_file_path.write_text(generated_patterns, encoding="utf-8")
                        st.toast("Ignore patterns generated and saved to .opencodeignore.")
                    except Exception as exc:
                        st.error(f"Failed to generate ignore patterns: {exc}")

    save_settings()
    _render_connectivity_tests()

    workspace_path = Path(st.session_state.workspace) if st.session_state.workspace else None
    if workspace_path:

        def _on_reuse_protocol(text: str):
            st.session_state.protocol_md = text
            st.session_state.wizard_step = 1
            st.rerun()

        render_existing_protocol_banner(
            workspace_path / "protocol.md",
            "protocol_md",
            on_reuse=_on_reuse_protocol,
        )

    st.markdown("### Draft your protocol")
    for section_key, label, height, placeholder in [
        (
            "raw_input",
            "**INPUT** - what already exists / what agent starts with",
            120,
            "e.g. A Python repo is at ./src. Main entry point is main.py.",
        ),
        (
            "raw_target",
            "**TARGET** - concrete, testable deliverables",
            140,
            "e.g.\n1. Build FastAPI server in src/main.py with GET /health and POST /echo\n"
            "2. Add requirements.txt\n"
            "3. All tests in ./tests/ must pass",
        ),
        (
            "raw_restrictions",
            "**RESTRICTIONS** - hard rules agent must not break",
            100,
            "e.g.\n- Don't touch files outside ./src\n- No system package installs\n- Keep code under 300 lines",
        ),
    ]:
        with _card():
            st.markdown(label)
            st.text_area(
                section_key,
                key=section_key,
                height=height,
                placeholder=placeholder,
                label_visibility="collapsed",
            )

    if any(st.session_state.get(key, "").strip() for key in ("raw_input", "raw_target", "raw_restrictions")):
        with st.expander("Protocol Quality Preview"):
            render_protocol_quality(
                f"## INPUT\n\n{st.session_state.raw_input}\n\n"
                f"## TARGET\n\n{st.session_state.raw_target}\n\n"
                f"## RESTRICTIONS\n\n{st.session_state.raw_restrictions}\n",
            )

    if st.button("Refine with AI", type="primary"):
        missing = [
            label
            for key, label in [
                ("openai_key", "OpenAI API Key"),
                ("workspace", "Workspace path"),
                ("raw_input", "INPUT section"),
                ("raw_target", "TARGET section"),
                ("raw_restrictions", "RESTRICTIONS section"),
            ]
            if not st.session_state.get(key, "").strip()
        ]
        if missing:
            st.error(f"Please fill in: {', '.join(missing)}")
        else:
            apply_api_config()
            with st.spinner("Asking supervisor to refine your protocol..."):
                wizard = ProtocolWizard(model=st.session_state.supervisor_model)
                try:
                    refined_md, _ = wizard.refine(
                        st.session_state.raw_input,
                        st.session_state.raw_target,
                        st.session_state.raw_restrictions,
                    )
                    st.session_state.protocol_md = refined_md
                    st.session_state.wizard_step = 1
                    st.rerun()
                except Exception as exc:
                    st.error(f"Refinement failed: {exc}")

    if st.session_state.wizard_step == 1 and st.session_state.protocol_md:
        st.markdown("---")
        st.markdown("### Refined `protocol.md`")
        st.markdown("*Review and edit below, then accept.*")
        st.text_area("proto_edit", key="protocol_md", height=300, label_visibility="collapsed")
        with st.expander("Protocol Quality Analysis"):
            render_protocol_quality(st.session_state.protocol_md, detailed=True)
        col_a, col_b, _ = st.columns([1, 1, 3])
        with col_a:
            if st.button("Accept & Save", type="primary"):
                _save_protocol()
                st.success("protocol.md saved to workspace.")
        with col_b:
            if st.button("Re-refine"):
                st.session_state.wizard_step = 0
                st.rerun()
