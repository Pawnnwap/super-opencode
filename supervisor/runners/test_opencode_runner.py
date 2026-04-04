"""
Tests for the OpencodeRunner class.
"""

import unittest
from unittest.mock import MagicMock, patch

from supervisor.runners.opencode_runner import OpencodeRunner


class TestOpencodeRunner(unittest.TestCase):
    def setUp(self):
        self.workspace = MagicMock()
        self.runner = OpencodeRunner(workspace=self.workspace, timeout=300)

    @patch("supervisor.runners.opencode_runner.subprocess.run")
    @patch("supervisor.runners.opencode_runner.platform.system")
    def test_stop_kills_chocolatey_processes_windows(self, mock_system, mock_run):
        """Test that stop() kills chocolatey processes on Windows."""
        mock_system.return_value = "Windows"

        # Call stop method
        self.runner.stop()

        # Verify that subprocess.run was called with tasklist first (without /v for speed)
        mock_run.assert_any_call(
            ["tasklist", "/fo", "csv"], capture_output=True, text=True, timeout=2
        )

    @patch("supervisor.runners.opencode_runner.subprocess.run")
    @patch("supervisor.runners.opencode_runner.platform.system")
    def test_stop_kills_chocolatey_processes_unix(self, mock_system, mock_run):
        """Test that stop() kills chocolatey processes on Linux/macOS."""
        mock_system.return_value = "Linux"  # or 'Darwin' for macOS

        # Call stop method
        self.runner.stop()

        # Verify that subprocess.run was called with pkill -f -i first
        mock_run.assert_any_call(
            ["pkill", "-f", "-i", "chocolatey"],
            capture_output=True,
            text=True,
            timeout=2,
        )

    @patch("supervisor.runners.opencode_runner.subprocess.run")
    @patch("supervisor.runners.opencode_runner.platform.system")
    @patch("supervisor.runners.opencode_runner.logger")
    def test_stop_handles_process_kill_errors(self, mock_logger, mock_system, mock_run):
        """Test that stop() handles errors when killing chocolatey processes gracefully."""
        mock_system.return_value = "Windows"
        mock_run.side_effect = Exception("Test error")

        # Call stop method - should not raise exception
        self.runner.stop()

        # Verify that logger.warning was called
        mock_logger.warning.assert_called()


class TestContinuationBehavior(unittest.TestCase):
    """Tests for the --continue session continuation feature."""

    def setUp(self):
        self.workspace = MagicMock()
        self.runner = OpencodeRunner(workspace=self.workspace, timeout=300)

    def test_continuation_disabled_by_default(self):
        """By default, --continue should not be enabled."""
        self.assertFalse(self.runner.is_continuation_enabled())

    def test_enable_continuation(self):
        """enable_continuation(True) should set the flag."""
        self.runner.enable_continuation(True)
        self.assertTrue(self.runner.is_continuation_enabled())

    def test_disable_continuation(self):
        """enable_continuation(False) should clear the flag."""
        self.runner.enable_continuation(True)
        self.runner.enable_continuation(False)
        self.assertFalse(self.runner.is_continuation_enabled())

    def test_mark_session_active(self):
        """mark_session_active should set _session_active."""
        self.assertFalse(self.runner._session_active)
        self.runner.mark_session_active()
        self.assertTrue(self.runner._session_active)

    def test_reset_session(self):
        """reset_session should clear both session_active and use_continue."""
        self.runner.enable_continuation(True)
        self.runner.mark_session_active()
        self.runner.reset_session()
        self.assertFalse(self.runner._session_active)
        self.assertFalse(self.runner.is_continuation_enabled())

    def test_reset_context_counter(self):
        """reset_context_counter should zero out _chars_exchanged."""
        self.runner._chars_exchanged = 1000
        self.runner.reset_context_counter()
        self.assertEqual(self.runner._chars_exchanged, 0)

    def test_stop_clears_session_active(self):
        """stop() should clear _session_active."""
        self.runner.mark_session_active()
        with (
            patch(
                "supervisor.runners.opencode_runner.platform.system",
                return_value="Linux",
            ),
            patch("supervisor.runners.opencode_runner.subprocess.run"),
        ):
            self.runner.stop()
        self.assertFalse(self.runner._session_active)

    @patch("supervisor.runners.opencode_runner.find_opencode", return_value="opencode")
    @patch("supervisor.runners.opencode_runner.subprocess.Popen")
    def test_build_cmd_with_continue_flag(self, mock_popen, mock_find):
        """When continuation is enabled, --continue should appear in the command."""
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ("", "")
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        self.runner.enable_continuation(True)
        self.runner._run_prompt("test prompt")

        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        self.assertIn("--continue", cmd)

    @patch("supervisor.runners.opencode_runner.find_opencode", return_value="opencode")
    @patch("supervisor.runners.opencode_runner.subprocess.Popen")
    def test_build_cmd_without_continue_flag(self, mock_popen, mock_find):
        """When continuation is disabled, --continue should NOT appear in the command."""
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ("", "")
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        self.runner.enable_continuation(False)
        self.runner._run_prompt("test prompt")

        call_args = mock_popen.call_args
        cmd = call_args[0][0]
        self.assertNotIn("--continue", cmd)


if __name__ == "__main__":
    unittest.main()
