"""Background analysis jobs.

A full analysis takes tens of seconds -- it scrapes several sources, backs off
politely when throttled, and runs the whole pipeline. That cannot happen inside
a page load, so the server queues the work and the page polls.

Deliberately a small thread pool rather than a task broker: the whole point of
this engine is that it runs anywhere with a Python interpreter and no services
to install.
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..db import utc_now


@dataclass
class Job:
    ticker: str
    market: str = ""
    state: str = "queued"          # queued | running | done | error
    queued_at: str = field(default_factory=utc_now)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    message: str = ""
    error: Optional[str] = None
    outcome: Any = None

    @property
    def finished(self) -> bool:
        return self.state in ("done", "error")


class JobRunner:
    """Serialises refreshes so concurrent page loads cannot stampede a source.

    One worker by default: every provider here is a shared, rate-limited public
    endpoint, and running four analyses in parallel is the fastest way to get
    the whole deployment throttled.
    """

    def __init__(self, worker: Callable[[str, str], Any], workers: int = 1):
        self._worker = worker
        self._jobs: Dict[str, Job] = {}
        self._queue: List[str] = []
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._stop = False
        self._threads = [
            threading.Thread(target=self._loop, daemon=True, name=f"analysis-{i}")
            for i in range(max(1, workers))
        ]
        for t in self._threads:
            t.start()

    def submit(self, ticker: str, market: str = "") -> Job:
        """Queue a refresh. An in-flight job for the same ticker is reused."""
        with self._wake:
            existing = self._jobs.get(ticker)
            if existing and not existing.finished:
                return existing
            job = Job(ticker=ticker, market=market, message="queued for analysis")
            self._jobs[ticker] = job
            self._queue.append(ticker)
            self._wake.notify()
            return job

    def get(self, ticker: str) -> Optional[Job]:
        with self._lock:
            return self._jobs.get(ticker)

    def active(self) -> List[Job]:
        with self._lock:
            return [j for j in self._jobs.values() if not j.finished]

    def shutdown(self) -> None:
        with self._wake:
            self._stop = True
            self._wake.notify_all()

    def _loop(self) -> None:
        while True:
            with self._wake:
                while not self._queue and not self._stop:
                    self._wake.wait(timeout=1.0)
                if self._stop and not self._queue:
                    return
                ticker = self._queue.pop(0)
                job = self._jobs.get(ticker)
                if job is None:
                    continue
                job.state = "running"
                job.started_at = utc_now()
                job.message = "collecting data and analysing"

            try:
                outcome = self._worker(job.ticker, job.market)
                with self._lock:
                    job.outcome = outcome
                    job.state = "done"
                    job.message = getattr(outcome, "status", "done")
            except Exception as exc:  # a bad source must never kill the worker
                with self._lock:
                    job.state = "error"
                    job.error = f"{type(exc).__name__}: {exc}"
                    job.message = "analysis failed"
                traceback.print_exc()
            finally:
                with self._lock:
                    job.finished_at = utc_now()
