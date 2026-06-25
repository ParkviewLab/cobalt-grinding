# SPDX-FileCopyrightText: 2026 Gary Frattarola <garyf@parkviewlab.ai>
#
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Task scheduler + worker pool for the daemon.

Long-running daemon operations (M2 indexer runs, M3+ ingestions, M6+
research / cogitate / curate runs) are submitted to the scheduler, which
runs them on a `ThreadPoolExecutor` and tracks their state in `Task`
objects. MCP tool handlers expose this state via `wiki.task_status`,
`wiki.task_list`, `wiki.task_cancel`.

**Why threads, not asyncio tasks for the work itself.** The heavy work
inside the daemon is in C extensions (fastembed ONNX, lancedb Rust,
pyarrow) and network I/O — all of which release the GIL. Threads get
real parallelism. The asyncio event loop is reserved for the MCP server
and request dispatch; submitting tasks to a thread pool keeps the loop
responsive while work happens off-thread.

**Cancellation model.** Python doesn't support hard thread cancellation,
so cancellation is cooperative: the worker function takes a
`CancelToken` and is expected to check it periodically. Tasks queued
but not yet started can be cancelled hard (just removed before the
worker picks them up).
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)


# ---- task model ----


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CancelToken:
    """Cooperative cancellation signal handed to worker functions."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def request(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise TaskCancelled()


class TaskCancelled(Exception):
    """Raised by worker code when it observes the cancel token."""


@dataclass
class Task:
    id: str
    kind: str
    status: TaskStatus = TaskStatus.QUEUED
    submitted_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    started_at: datetime | None = None
    finished_at: datetime | None = None
    progress: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status.value,
            "submitted_at": self.submitted_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
        }


# ---- scheduler ----


class Scheduler:
    """Runs submitted callables on a thread pool, tracks them as `Task`s."""

    def __init__(self, *, max_workers: int = 8) -> None:
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="cgworker")
        self._tasks: dict[str, Task] = {}
        self._futures: dict[str, Future[Any]] = {}
        self._cancels: dict[str, CancelToken] = {}
        self._lock = threading.Lock()

    # ---- submit ----

    def submit(
        self,
        *,
        kind: str,
        fn: Callable[..., Any],
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        accepts_cancel_token: bool = False,
        accepts_progress: bool = False,
    ) -> str:
        """Submit a callable for background execution.

        If `accepts_cancel_token` is True, the function gets a kwarg
        `cancel: CancelToken` (cooperative cancellation).
        If `accepts_progress` is True, the function gets a kwarg
        `progress: Callable[[dict[str, Any]], None]` that the worker
        can call at logical points to publish structured progress.
        Each call is reflected in the task's `progress` field, which
        MCP clients see via `wiki.task_status`.
        """
        kwargs = dict(kwargs or {})
        task_id = uuid.uuid4().hex[:12]
        task = Task(id=task_id, kind=kind)
        token = CancelToken()

        if accepts_cancel_token:
            if "cancel" in kwargs:
                raise ValueError(
                    f"submit(kind={kind!r}, accepts_cancel_token=True) was given a "
                    "`cancel` kwarg by the caller; the scheduler injects this name "
                    "and would overwrite the caller's value"
                )
            kwargs["cancel"] = token

        if accepts_progress:
            if "progress" in kwargs:
                raise ValueError(
                    f"submit(kind={kind!r}, accepts_progress=True) was given a "
                    "`progress` kwarg by the caller; the scheduler injects this "
                    "name and would overwrite the caller's value"
                )
            kwargs["progress"] = lambda update: self.update_progress(task_id, update)

        with self._lock:
            self._tasks[task_id] = task
            self._cancels[task_id] = token

        def _run() -> Any:
            return self._run_task(task_id, fn, args, kwargs)

        future = self._executor.submit(_run)
        with self._lock:
            self._futures[task_id] = future
        return task_id

    def _run_task(
        self,
        task_id: str,
        fn: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        with self._lock:
            task = self._tasks[task_id]
            token = self._cancels[task_id]

        # If cancellation was requested before the worker started, exit immediately.
        if token.cancelled:
            with self._lock:
                task.status = TaskStatus.CANCELLED
                task.finished_at = datetime.now(UTC)
            return None

        with self._lock:
            task.status = TaskStatus.RUNNING
            task.started_at = datetime.now(UTC)

        try:
            result = fn(*args, **kwargs)
        except TaskCancelled:
            with self._lock:
                task.status = TaskStatus.CANCELLED
                task.finished_at = datetime.now(UTC)
            return None
        except Exception as e:
            logger.exception("task %s (%s) failed", task_id, task.kind)
            with self._lock:
                task.status = TaskStatus.FAILED
                task.error = f"{type(e).__name__}: {e}"
                task.finished_at = datetime.now(UTC)
            return None

        with self._lock:
            task.status = TaskStatus.SUCCEEDED
            task.result = result
            task.finished_at = datetime.now(UTC)
        return result

    # ---- query / control ----

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list_tasks(
        self, *, kind: str | None = None, status: TaskStatus | None = None
    ) -> list[Task]:
        with self._lock:
            tasks = list(self._tasks.values())
        if kind:
            tasks = [t for t in tasks if t.kind == kind]
        if status:
            tasks = [t for t in tasks if t.status == status]
        return sorted(tasks, key=lambda t: t.submitted_at, reverse=True)

    def cancel(self, task_id: str) -> bool:
        """Request cancellation of a task. Returns True if the task exists.

        For QUEUED tasks: cancels immediately (the worker will see the
        token set before it starts and exit). For RUNNING tasks:
        cooperative — the worker observes the token at its next checkpoint.
        For already-finished tasks: no-op.
        """
        with self._lock:
            task = self._tasks.get(task_id)
            token = self._cancels.get(task_id)
        if task is None or token is None:
            return False
        if task.status in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            return True
        token.request()
        return True

    def update_progress(self, task_id: str, progress: dict[str, Any]) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.progress = dict(progress)

    # ---- shutdown ----

    def shutdown(self, *, wait: bool = True, cancel_pending: bool = True) -> None:
        if cancel_pending:
            with self._lock:
                for token in self._cancels.values():
                    token.request()
        self._executor.shutdown(wait=wait, cancel_futures=cancel_pending)
