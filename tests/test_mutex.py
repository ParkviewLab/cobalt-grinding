# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for cobalt_grinding.daemon.mutex — single-writer corpus mutex."""

from __future__ import annotations

import threading
import time

from cobalt_grinding.daemon.mutex import CorpusWriteMutex


def test_acquire_and_release() -> None:
    m = CorpusWriteMutex()
    assert m.locked is False
    with m.acquire("alice"):
        assert m.locked is True
        assert m.holder == "alice"
    assert m.locked is False
    assert m.holder is None


def test_concurrent_holders_serialize() -> None:
    """Two threads racing to enter the critical section must not overlap."""
    m = CorpusWriteMutex()
    timeline: list[tuple[str, str]] = []
    timeline_lock = threading.Lock()

    def holder(name: str) -> None:
        with m.acquire(name):
            with timeline_lock:
                timeline.append((name, "enter"))
            time.sleep(0.05)
            with timeline_lock:
                timeline.append((name, "exit"))

    t1 = threading.Thread(target=holder, args=("alice",))
    t2 = threading.Thread(target=holder, args=("bob",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Each holder's enter/exit must be adjacent in the timeline — i.e., the
    # other holder cannot enter while one is inside.
    assert timeline[0][1] == "enter"
    assert timeline[1][1] == "exit"
    assert timeline[2][1] == "enter"
    assert timeline[3][1] == "exit"
    assert timeline[0][0] == timeline[1][0]
    assert timeline[2][0] == timeline[3][0]


def test_holder_cleared_on_exception() -> None:
    m = CorpusWriteMutex()
    try:
        with m.acquire("alice"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert m.holder is None
    assert m.locked is False
