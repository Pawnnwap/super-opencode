"""A single oversized stdout line must not reach the classifier unbounded.

The consumer parses and rewrites each line on one thread while every other job
in the worker waits for the GIL. One runaway line (2026-11-10: a long URL path
through a catastrophic regex) froze the whole farm, so the reader caps lines at
a size no legitimate event approaches.
"""

import queue

from supervisor.runners.stream_driver import (
    _MAX_LINE_CHARS,
    _STDOUT_DONE,
    _read_stdout,
)


class _FakeStream:
    def __init__(self, lines):
        self._lines = lines

    def __iter__(self):
        return iter(self._lines)


def test_oversized_line_is_capped_before_the_queue():
    huge = "x" * (_MAX_LINE_CHARS + 4096)
    out_q: queue.Queue = queue.Queue()
    _read_stdout(_FakeStream([huge, "small line\n"]), out_q)
    assert len(out_q.get_nowait()) == _MAX_LINE_CHARS
    assert out_q.get_nowait() == "small line\n"
    assert out_q.get_nowait() is _STDOUT_DONE


def test_normal_lines_pass_through_untouched():
    lines = ["a\n", '{"type":"turn.completed"}\n']
    out_q: queue.Queue = queue.Queue()
    _read_stdout(_FakeStream(lines), out_q)
    assert [out_q.get_nowait(), out_q.get_nowait()] == lines
    assert out_q.get_nowait() is _STDOUT_DONE
