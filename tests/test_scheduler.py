# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Tests for cobalt_grinding.daemon.scheduler — submit, status, cancel, errors."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest

from cobalt_grinding.daemon.scheduler import CancelToken, Scheduler, Task, TaskCancelled, TaskStatus


@pytest.fixture
def scheduler() -> Iterator[Scheduler]:
    s = Scheduler(max_workers=4)
    yield s
    s.shutdown(wait=True, cancel_pending=True)


def _must_get(scheduler: Scheduler, task_id: str) -> Task:
    """`scheduler.get` returns Optional; in tests we know the task exists."""
    task = scheduler.get(task_id)
    assert task is not None, f"task {task_id} not found"
    return task


def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.01) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("predicate did not become true within timeout")


def test_submit_runs_function_and_records_result(scheduler: Scheduler) -> None:
    task_id = scheduler.submit(kind="add", fn=lambda a, b: a + b, args=(2, 3))
    _wait_for(lambda: _must_get(scheduler, task_id).status == TaskStatus.SUCCEEDED)
    task = _must_get(scheduler, task_id)
    assert task.status is TaskStatus.SUCCEEDED
    assert task.result == 5
    assert task.error is None
    assert task.started_at is not None
    assert task.finished_at is not None


def test_failing_function_records_error(scheduler: Scheduler) -> None:
    def boom() -> None:
        raise RuntimeError("explicit failure")

    task_id = scheduler.submit(kind="boom", fn=boom)
    _wait_for(lambda: _must_get(scheduler, task_id).status == TaskStatus.FAILED)
    task = _must_get(scheduler, task_id)
    assert task.status is TaskStatus.FAILED
    assert task.error and "explicit failure" in task.error
    assert task.result is None


def test_cancel_propagates_to_worker(scheduler: Scheduler) -> None:
    started = threading.Event()

    def long_task(*, cancel: CancelToken) -> str:
        started.set()
        for _ in range(100):
            cancel.raise_if_cancelled()
            time.sleep(0.01)
        return "done"

    task_id = scheduler.submit(kind="long", fn=long_task, accepts_cancel_token=True)
    started.wait(timeout=2)

    assert scheduler.cancel(task_id) is True
    _wait_for(lambda: _must_get(scheduler, task_id).status == TaskStatus.CANCELLED)
    task = _must_get(scheduler, task_id)
    assert task.status is TaskStatus.CANCELLED


def test_taskcancelled_exception_marks_cancelled(scheduler: Scheduler) -> None:
    """Workers raising TaskCancelled directly are recorded as cancelled, not failed."""

    def cancel_immediately(*, cancel: CancelToken) -> None:
        raise TaskCancelled()

    task_id = scheduler.submit(kind="x", fn=cancel_immediately, accepts_cancel_token=True)
    _wait_for(lambda: _must_get(scheduler, task_id).status == TaskStatus.CANCELLED)
    task = _must_get(scheduler, task_id)
    assert task.status is TaskStatus.CANCELLED
    assert task.error is None


def test_cancel_unknown_task_returns_false(scheduler: Scheduler) -> None:
    assert scheduler.cancel("does-not-exist") is False


def test_submit_rejects_caller_supplied_cancel_kwarg(scheduler: Scheduler) -> None:
    """If the caller passes a `cancel` kwarg, refuse — the scheduler injects
    one with that name and would otherwise silently overwrite the caller's."""

    def worker(*, cancel: CancelToken) -> None:
        return None

    with pytest.raises(ValueError, match="cancel"):
        scheduler.submit(
            kind="x",
            fn=worker,
            kwargs={"cancel": "user-supplied"},
            accepts_cancel_token=True,
        )


def test_submit_rejects_caller_supplied_progress_kwarg(scheduler: Scheduler) -> None:
    def worker(*, progress: object) -> None:
        return None

    with pytest.raises(ValueError, match="progress"):
        scheduler.submit(
            kind="x",
            fn=worker,
            kwargs={"progress": "user-supplied"},
            accepts_progress=True,
        )


def test_list_filters_by_kind_and_status(scheduler: Scheduler) -> None:
    a = scheduler.submit(kind="kind-a", fn=lambda: 1)
    b = scheduler.submit(kind="kind-b", fn=lambda: 2)
    _wait_for(lambda: all(_must_get(scheduler, t).status == TaskStatus.SUCCEEDED for t in (a, b)))

    by_kind = [t.id for t in scheduler.list_tasks(kind="kind-a")]
    assert by_kind == [a]

    by_status = scheduler.list_tasks(status=TaskStatus.SUCCEEDED)
    assert {t.id for t in by_status} >= {a, b}


def test_to_dict_contains_expected_fields(scheduler: Scheduler) -> None:
    task_id = scheduler.submit(kind="thing", fn=lambda: "ok")
    _wait_for(lambda: _must_get(scheduler, task_id).status == TaskStatus.SUCCEEDED)
    d = _must_get(scheduler, task_id).to_dict()
    assert d["id"] == task_id
    assert d["kind"] == "thing"
    assert d["status"] == "succeeded"
    assert d["result"] == "ok"
    assert isinstance(d["submitted_at"], str)
    assert isinstance(d["started_at"], str)
    assert isinstance(d["finished_at"], str)
