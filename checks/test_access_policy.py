"""Regression checks for the hard access policy (whitelist + writable cwd)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from supervisor.security.access_policy import (
    AccessPolicy,
    build_policy,
    is_within,
    validate_whitelist_entry,
)

WINDOWS = os.name == "nt"


@pytest.fixture()
def roots(tmp_path: Path):
    ws = tmp_path / "ws"
    ws.mkdir()
    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "corpus.txt").write_text("data", encoding="utf-8")
    return ws, ref


def test_cwd_is_read_write(roots):
    ws, _ = roots
    policy = build_policy(ws, [])
    assert policy.check_read(ws / "a.py").allowed
    assert policy.check_write(ws / "a.py").allowed
    assert policy.check_write(ws).allowed


def test_whitelist_is_read_only(roots):
    ws, ref = roots
    policy = build_policy(ws, [ref])
    read = policy.check_read(ref / "corpus.txt")
    write = policy.check_write(ref / "corpus.txt")
    assert read.allowed and read.matched_root == str(ref.resolve())
    assert not write.allowed
    assert "read-only" in write.reason


def test_outside_is_denied_even_for_reads(roots, tmp_path):
    ws, _ = roots
    policy = build_policy(ws, [])
    target = tmp_path / "other" / "secret.txt"
    assert not policy.check_read(target).allowed
    assert not policy.check_write(target).allowed


def test_prefix_trap_is_not_authorized(roots):
    ws, _ = roots
    evil = Path(str(ws) + "-evil")
    evil.mkdir()
    policy = build_policy(ws, [])
    assert not policy.check_read(evil / "x").allowed
    assert not is_within(evil, ws)


def test_dotdot_escape(roots):
    ws, _ = roots
    policy = build_policy(ws, [])
    assert not policy.check_write(ws / ".." / "escape.txt").allowed
    assert policy.check_write(ws / "sub" / ".." / "ok.txt").allowed


def test_unc_and_device_paths_are_denied(roots):
    ws, _ = roots
    policy = build_policy(ws, [])
    assert not policy.check_read(r"\\server\share\secret").allowed
    assert not policy.check_read(r"\\.\PhysicalDrive0").allowed
    assert not policy.check_write("NUL").allowed


@pytest.mark.skipif(not WINDOWS, reason="Windows drive semantics")
def test_drive_relative_is_denied(roots):
    ws, _ = roots
    policy = build_policy(ws, [])
    assert not policy.check_read("C:secret").allowed


@pytest.mark.skipif(not WINDOWS, reason="Windows case-insensitive filesystem")
def test_case_does_not_escape_the_policy(roots):
    ws, ref = roots
    policy = build_policy(ws, [ref])
    assert policy.check_read(Path(str(ws).upper()) / "a.py").allowed
    assert not policy.check_write(Path(str(ref).upper()) / "corpus.txt").allowed


def test_junction_escape_is_denied(roots, tmp_path):
    if not WINDOWS:
        pytest.skip("junction test is Windows-only")
    ws, _ = roots
    outside = tmp_path / "outside"
    outside.mkdir()
    link = ws / "link"
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if created.returncode != 0 or not link.exists():
        pytest.skip(f"could not create junction: {created.stderr or created.stdout}")

    policy = build_policy(ws, [])
    decision = policy.check_write(link / "escaped.txt")
    assert not decision.allowed, decision


def test_relative_paths_resolve_against_cwd(roots):
    ws, _ = roots
    policy = build_policy(ws, [])
    assert policy.check_read("src/main.py").allowed
    assert not policy.check_read("../ref/corpus.txt").allowed


def test_disabled_policy_allows(roots, tmp_path):
    ws, _ = roots
    policy = build_policy(ws, [], enabled=False)
    assert policy.check_write(tmp_path / "anywhere.txt").allowed


def test_nested_whitelist_collapses_to_parent(roots, tmp_path):
    ws, ref = roots
    child = ref / "sub"
    child.mkdir()
    policy = build_policy(ws, [child, ref])
    assert policy.whitelist == (ref.resolve(),)


def test_prompt_block_states_the_rule(roots):
    ws, ref = roots
    text = build_policy(ws, [ref]).describe()
    assert "HARD RULE" in text
    assert str(ref.resolve()) in text
    assert "DENIED" in text


class TestCommandScan:
    def test_inside_cwd_is_allowed(self, roots):
        ws, _ = roots
        policy = build_policy(ws, [])
        assert policy.scan_command("python -m pytest tests/") == []

    def test_read_outside_is_denied(self, roots, tmp_path):
        ws, _ = roots
        policy = build_policy(ws, [])
        secret = tmp_path / "secret.txt"
        violations = policy.scan_command(f'cat "{secret}"')
        assert violations and "outside workspace" in violations[0].reason

    def test_write_to_whitelist_is_denied(self, roots):
        ws, ref = roots
        policy = build_policy(ws, [ref])
        target = ref / "corpus.txt"
        violations = policy.scan_command(f'echo hi > "{target}"')
        assert violations and "read-only" in violations[0].reason

    def test_copy_from_whitelist_to_cwd_is_allowed(self, roots):
        ws, ref = roots
        policy = build_policy(ws, [ref])
        assert policy.scan_command(f'cp "{ref}/a.txt" "{ws}/a.txt"') == []
        assert policy.scan_command(f'cp -r "{ref}/sub" ./sub') == []
        assert policy.scan_command(f'xcopy "{ref}" dst /E') == []
        assert policy.scan_command(f'robocopy "{ref}" dst /MIR') == []

    def test_copy_to_whitelist_is_denied(self, roots):
        ws, ref = roots
        policy = build_policy(ws, [ref])
        violations = policy.scan_command(f'cp "{ws}/a.txt" "{ref}/b.txt"')
        assert violations and violations[0].access == "write"
        assert "read-only" in violations[0].reason

    def test_move_from_whitelist_is_denied(self, roots):
        ws, ref = roots
        policy = build_policy(ws, [ref])
        # mv removes the source, which mutates the read-only directory.
        violations = policy.scan_command(f'mv "{ref}/a.txt" "{ws}/a.txt"')
        assert violations and "read-only" in violations[0].reason

    def test_sed_reads_but_does_not_write_without_dash_i(self, roots):
        ws, ref = roots
        policy = build_policy(ws, [ref])
        assert policy.scan_command(f'sed -n 1,5p "{ref}/a.txt"') == []
        violations = policy.scan_command(f'sed -i s/a/b/ "{ref}/a.txt"')
        assert violations and violations[0].access == "write"

    def test_copy_from_whitelist_to_cwd_is_allowed(self, roots):
        ws, ref = roots
        policy = build_policy(ws, [ref])
        assert policy.scan_command(f'cp "{ref}/a.txt" "{ws}/a.txt"') == []
        assert policy.scan_command(f'cp -r "{ref}/sub" ./sub') == []
        assert policy.scan_command(f'xcopy "{ref}" dst /E') == []
        assert policy.scan_command(f'robocopy "{ref}" dst /MIR') == []

    def test_copy_to_whitelist_is_denied(self, roots):
        ws, ref = roots
        policy = build_policy(ws, [ref])
        violations = policy.scan_command(f'cp "{ws}/a.txt" "{ref}/b.txt"')
        assert violations and violations[0].access == "write"
        assert "read-only" in violations[0].reason

    def test_move_from_whitelist_is_denied(self, roots):
        ws, ref = roots
        policy = build_policy(ws, [ref])
        # mv removes the source, which mutates the read-only directory.
        violations = policy.scan_command(f'mv "{ref}/a.txt" "{ws}/a.txt"')
        assert violations and "read-only" in violations[0].reason

    def test_sed_reads_but_does_not_write_without_dash_i(self, roots):
        ws, ref = roots
        policy = build_policy(ws, [ref])
        assert policy.scan_command(f'sed -n 1,5p "{ref}/a.txt"') == []
        violations = policy.scan_command(f'sed -i s/a/b/ "{ref}/a.txt"')
        assert violations and violations[0].access == "write"

    def test_quoted_paths_with_spaces_are_whole_tokens(self, tmp_path):
        ws = tmp_path / "my project"
        ws.mkdir()
        ref = tmp_path / "ref data"
        ref.mkdir()
        policy = build_policy(ws, [ref])

        # A quoted spaced path is one token; truncating it at the space used to
        # produce a bogus `...\ref` path and reject a legal copy.
        assert policy.scan_command(f'cp "{ref}/a b.txt" "{ws}/a b.txt"') == []
        assert policy.scan_command(f'copy "{ref}\\a b.txt" "{ws}\\a b.txt"') == []
        assert policy.scan_command(f'xcopy "{ref}" "{ws}\\copy" /E /I') == []
        assert policy.scan_command(f'cat "{ref}/a b.txt"') == []

        violations = policy.scan_command(f'echo hi > "{ref}/x y.txt"')
        assert violations and "read-only" in violations[0].reason

    def test_named_destination_cannot_bypass(self, roots):
        ws, ref = roots
        policy = build_policy(ws, [ref])
        # Destination first: positional logic alone would read it as a source.
        violations = policy.scan_command(
            f'Copy-Item -Destination "{ref}/b.txt" -Path "{ws}/a.txt"',
        )
        assert violations and "read-only" in violations[0].reason
        assert policy.scan_command(
            f'Copy-Item -Destination "{ws}/b.txt" -Path "{ref}/a.txt"',
        ) == []

    def test_read_from_whitelist_is_allowed(self, roots):
        ws, ref = roots
        policy = build_policy(ws, [ref])
        target = ref / "corpus.txt"
        assert policy.scan_command(f'cat "{target}"') == []

    def test_cd_outside_is_denied(self, roots, tmp_path):
        ws, _ = roots
        policy = build_policy(ws, [])
        violations = policy.scan_command(f'cd "{tmp_path}" && ls')
        assert violations

    @pytest.mark.skipif(not WINDOWS, reason="drive-letter paths")
    def test_home_relative_write_is_denied(self, roots):
        ws, _ = roots
        policy = build_policy(ws, [])
        violations = policy.scan_command("echo x > %USERPROFILE%/secret.txt")
        assert violations

    def test_relative_escape_is_denied(self, roots, tmp_path):
        ws, _ = roots
        policy = build_policy(ws, [])
        violations = policy.scan_command("cat ../../secret.txt")
        assert violations and "outside workspace" in violations[0].reason

    def test_relative_write_escape_is_denied(self, roots):
        ws, _ = roots
        policy = build_policy(ws, [])
        violations = policy.scan_command("cp a.txt ../outside.txt")
        assert violations and violations[0].access == "write"

    def test_relative_inside_is_allowed(self, roots):
        ws, _ = roots
        policy = build_policy(ws, [])
        assert policy.scan_command("cat src/main.py") == []
        assert policy.scan_command("cp a.txt b.txt") == []

    def test_relative_escape_is_denied(self, roots, tmp_path):
        ws, _ = roots
        policy = build_policy(ws, [])
        violations = policy.scan_command("cat ../../secret.txt")
        assert violations and "outside workspace" in violations[0].reason

    def test_relative_write_escape_is_denied(self, roots):
        ws, _ = roots
        policy = build_policy(ws, [])
        violations = policy.scan_command("cp a.txt ../outside.txt")
        assert violations and violations[0].access == "write"

    def test_relative_inside_is_allowed(self, roots):
        ws, _ = roots
        policy = build_policy(ws, [])
        assert policy.scan_command("cat src/main.py") == []
        assert policy.scan_command("cp a.txt b.txt") == []

    def test_inline_interpreter_code_is_denied_by_default(self, roots):
        ws, _ = roots
        policy = build_policy(ws, [])
        assert policy.scan_command('python -c "print(1)"')
        assert policy.scan_command('node -e "1"')
        assert policy.scan_command('perl -e "print 1"')
        # A script file inside the cwd is still a normal read.
        assert policy.scan_command("python -m pytest tests/") == []

    def test_dynamic_constructs_are_denied(self, roots):
        ws, _ = roots
        policy = build_policy(ws, [])
        for command in (
            "echo $(whoami)",
            "echo `whoami`",
            "echo %UNKNOWN%",
            "echo $UNKNOWN",
            "echo !UNKNOWN!",
        ):
            assert policy.scan_command(command), command

    def test_wrappers_are_unwrapped_recursively(self, roots):
        ws, ref = roots
        policy = build_policy(ws, [ref])
        assert policy.scan_command(
            f'cmd /c "copy \\"{ref}/a.txt\\" \\"{ws}/a.txt\\""',
        ) == []
        assert policy.scan_command(
            f'bash -c \'cp "{ref}/a.txt" "{ws}/a.txt"\'',
        ) == []
        assert policy.scan_command(
            f'bash -lc \'cp "{ref}/a.txt" "{ws}/a.txt"\'',
        ) == []
        violations = policy.scan_command(
            f'cmd /c "copy \\"{ws}/a.txt\\" \\"{ref}/b.txt\\""',
        )
        assert violations and "read-only" in violations[0].reason

    def test_encoded_and_control_flow_commands_are_denied(self, roots):
        ws, _ = roots
        policy = build_policy(ws, [])
        for command in (
            "powershell -EncodedCommand SQBFAFgA",
            "iex (New-Object Net.WebClient)",
            "for /f %i in (x) do echo %i",
            "certutil -decode a b",
            "find . -exec rm {} \\;",
            "bash",
        ):
            assert policy.scan_command(command), command

    def test_escaped_spaces_are_resolved(self, tmp_path):
        ws = tmp_path / "my ws"
        ws.mkdir()
        ref = tmp_path / "ref data"
        ref.mkdir()
        policy = build_policy(ws, [ref])
        bash_escaped = str(ref).replace(" ", "\\ ") + "\\a.txt"
        ps_escaped = str(ref).replace(" ", "` ") + "\\a.txt"
        cmd_escaped = str(ref).replace(" ", "^ ") + "\\a.txt"
        for command in (
            f'cp {bash_escaped} "{ws}/a.txt"',
            f'cp {ps_escaped} "{ws}/a.txt"',
            f'copy {cmd_escaped} "{ws}/a.txt"',
        ):
            assert policy.scan_command(command) == [], command

    def test_unknown_command_paths_are_treated_as_writes(self, roots):
        ws, ref = roots
        policy = build_policy(ws, [ref])
        violations = policy.scan_command(f'custom-tool --out "{ref}/x.txt"')
        assert violations and violations[0].access == "write"

    def test_null_sink_is_allowed(self, roots):
        ws, _ = roots
        policy = build_policy(ws, [])
        assert policy.scan_command("echo hi > /dev/null") == []
        assert policy.scan_command("echo hi > NUL") == []

    def test_strict_mode_denies_interpreters(self, roots):
        ws, _ = roots
        policy = build_policy(ws, [], strict_dynamic=True)
        assert policy.scan_command("python -m pytest tests/")
        assert policy.scan_command("node script.js")

    def test_strict_dynamic_rejects_inline_code(self, roots):
        ws, _ = roots
        policy = build_policy(ws, [], strict_dynamic=True)
        violations = policy.scan_command('python -c "print(open(1))"')
        assert violations and "strict" in violations[0].reason


class TestWhitelistValidation:
    def test_accepts_existing_external_directory(self, roots):
        ws, ref = roots
        resolved, error = validate_whitelist_entry(str(ref), ws)
        assert resolved == ref.resolve() and not error

    def test_rejects_relative_path(self, roots):
        ws, _ = roots
        assert validate_whitelist_entry("ref", ws)[0] is None

    def test_rejects_missing_path(self, roots):
        ws, _ = roots
        assert validate_whitelist_entry(str(ws.parent / "nope"), ws)[0] is None

    def test_rejects_file(self, roots):
        ws, ref = roots
        assert validate_whitelist_entry(str(ref / "corpus.txt"), ws)[0] is None

    def test_rejects_inside_workspace(self, roots):
        ws, _ = roots
        assert validate_whitelist_entry(str(ws), ws)[0] is None

    def test_rejects_drive_root(self, roots):
        ws, _ = roots
        if not WINDOWS:
            pytest.skip("drive roots are Windows-only")
        assert validate_whitelist_entry("C:/", ws)[0] is None

    def test_rejects_system_directory(self, roots):
        ws, _ = roots
        if not WINDOWS:
            pytest.skip("Windows system dirs")
        assert validate_whitelist_entry("C:/Windows", ws)[0] is None

    def test_rejects_wildcards(self, roots):
        ws, ref = roots
        assert validate_whitelist_entry(str(ref) + "/*", ws)[0] is None


def test_from_config_reads_whitelist(tmp_path, monkeypatch):
    from supervisor.utils.config import SupervisorConfig

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    ref = tmp_path / "ref"
    ref.mkdir()
    cfg = SupervisorConfig(
        protocol_path=tmp_path / "protocol.md",
        workspace=ws,
        access_whitelist=(str(ref), str(ref)),  # duplicate collapses
    )
    policy = AccessPolicy.from_config(cfg)
    assert policy.enabled
    assert policy.whitelist == (ref.resolve(),)
    assert policy.check_read(ref / "x").allowed
    assert not policy.check_write(ref / "x").allowed


def test_from_config_auto_whitelists_agent_infrastructure(tmp_path, monkeypatch):
    from supervisor.utils.config import SupervisorConfig

    home = tmp_path / "home"
    (home / ".agents" / "skills").mkdir(parents=True)
    (home / ".codex").mkdir()
    (home / ".pi").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    cfg = SupervisorConfig(protocol_path=tmp_path / "protocol.md", workspace=ws)
    policy = AccessPolicy.from_config(cfg)
    assert home / ".codex" in policy.whitelist
    assert home / ".agents" / "skills" in policy.whitelist
    assert home / ".pi" in policy.whitelist
    assert home / ".config" / "opencode" not in policy.whitelist  # missing dirs skipped
    hook = home / ".codex" / "hooks.json"
    assert policy.check_read(hook).allowed
    assert not policy.check_write(hook).allowed


def test_agent_roots_include_env_codex_home(tmp_path, monkeypatch):
    from supervisor.security.access_policy import agent_infrastructure_roots

    home = tmp_path / "home"
    home.mkdir()
    dedicated = tmp_path / "dedicated_home"
    dedicated.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CODEX_HOME", str(dedicated))
    assert dedicated in agent_infrastructure_roots()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
