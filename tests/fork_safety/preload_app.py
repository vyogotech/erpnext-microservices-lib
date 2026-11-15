"""
Minimal WSGI app for the Gunicorn --preload / embedded RQ fork-safety smoke test.

Uses BackgroundTaskMixin directly (no Frappe site / DB needed) and mirrors what
signup-service does: it enqueues a job *during module import*, i.e. in the
Gunicorn master under --preload. Driven by run_preload_rq_smoke.sh.
"""

import importlib
import logging
import os

from flask import Flask, jsonify
from redis import Redis

from frappe_microservice.background import BackgroundTaskMixin

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s pid=%(process)d %(name)s %(message)s"
)

QUEUE = "fork-safety-smoke"
REDIS_URL = os.environ["REDIS_URL"]


def record_pid(tag):
    """RQ job: record which process consumed it, and do a lazy import in the
    worker thread (the operation that deadlocked forked workers before)."""
    importlib.import_module("email.mime.multipart")
    Redis.from_url(REDIS_URL).hset("fork-smoke:jobs", tag, os.getpid())


class SmokeService(BackgroundTaskMixin):
    def __init__(self):
        self.name = QUEUE
        self.logger = logging.getLogger(QUEUE)
        self._maybe_start_rq_worker()


svc = SmokeService()

# Runs in the Gunicorn master under --preload (like signup-service's startup backfill).
Redis.from_url(REDIS_URL).set("fork-smoke:master_pid", os.getpid())
svc._rq_queue.enqueue(record_pid, "enqueued-in-master")

app = Flask(__name__)


@app.get("/pid")
def pid():
    t = svc._rq_worker_thread
    return jsonify(pid=os.getpid(), rq_thread_alive=bool(t and t.is_alive()))


@app.post("/enqueue/<tag>")
def enqueue(tag):
    svc._rq_queue.enqueue(record_pid, tag)
    return jsonify(ok=True)
