"""
Tests for the OpencodeRunner class.
"""

import unittest
from unittest.mock import patch, MagicMock
import sys

from supervisor.runners.opencode_runner import OpencodeRunner


class TestOpencodeRunner(unittest.TestCase):
    def setUp(self):
        self.workspace = MagicMock()
        self.runner = OpencodeRunner(workspace=self.workspace)

    @patch("supervisor.runners.opencode_runner.subprocess.run")
    @patch("supervisor.runners.opencode_runner.platform.system")
    def test_stop_kills_chocolatey_processes_windows(self, mock_system, mock_run):
        """Test that stop() kills chocolatey processes on Windows."""
        mock_system.return_value = "Windows"

        # Call stop method
        self.runner.stop()

        # Verify that subprocess.run was called with tasklist first
        mock_run.assert_any_call(
            ["tasklist", "/v", "/fo", "csv"], capture_output=True, text=True, timeout=10
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
            timeout=10,
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


if __name__ == "__main__":
    unittest.main()
