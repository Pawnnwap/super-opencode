"""supervisor/monitoring/context_monitor.py — compatibility shim.

ContextMonitor was merged into SessionTracker. This module re-exports
SessionTracker as ContextMonitor so existing imports keep working.
"""

from supervisor.monitoring.session_tracker import SessionTracker as ContextMonitor

__all__ = ["ContextMonitor"]
