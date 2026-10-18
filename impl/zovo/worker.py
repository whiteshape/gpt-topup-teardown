"""Background order runner.

HTTP handlers must return fast; a top-up takes 10-180 seconds. The worker keeps
a bounded pool and exposes a job handle so the client can poll.

It deliberately keeps **no queue on disk**: if the process dies mid-order the
order stays `SUBMITTED` and the reconciliation path marks it `UNKNOWN` instead
of silently retrying a payment. Retrying a payment blindly is how you double
charge a card.
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional

from .orders import OrderEngine


class Worker:
    def __init__(self, engine: OrderEngine, size: int = 2):
        self.engine = engine
        self.pool = ThreadPoolExecutor(max_workers=max(1, size), thread_name_prefix="zovo-order")
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.lock = threading.Lock()

    def submit(self, **kwargs) -> str:
        job_id = "job_" + uuid.uuid4().hex[:16]
        with self.lock:
            self.jobs[job_id] = {
                "job_id": job_id,
                "state": "queued",
                "order_id": None,
                "result": None,
                "created_at": int(time.time()),
            }

        def on_created(order_id: int) -> None:
            with self.lock:
                self.jobs[job_id]["order_id"] = order_id
                self.jobs[job_id]["state"] = "running"

        def task() -> None:
            try:
                result = self.engine.run(on_order_created=on_created, **kwargs)
                state = "done" if result.get("ok") else "failed"
            except Exception as exc:  # noqa: BLE001
                result = {"ok": False, "code": "internal_error", "message": str(exc)}
                state = "failed"
            with self.lock:
                self.jobs[job_id].update({"state": state, "result": result,
                                          "finished_at": int(time.time())})

        self.pool.submit(task)
        return job_id

    def status(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            job = self.jobs.get(job_id)
            return dict(job) if job else None

    def shutdown(self) -> None:
        self.pool.shutdown(wait=False, cancel_futures=True)
