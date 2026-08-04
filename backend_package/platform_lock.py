from __future__ import annotations

import contextlib
import fcntl
import os
import time

GLOBAL_TASK_LOCK_ENABLED = os.environ.get("GLOBAL_TASK_LOCK_ENABLED", "1") == "1"
GLOBAL_TASK_LOCK_PATH = os.environ.get(
    "GLOBAL_TASK_LOCK_PATH",
    "/var/lib/agent-runner/cognimoss-global-task.lock",
)
GLOBAL_TASK_LOCK_POLL_SECONDS = float(os.environ.get("GLOBAL_TASK_LOCK_POLL_SECONDS", "5"))


@contextlib.contextmanager
def global_task_lock(name: str):
    if not GLOBAL_TASK_LOCK_ENABLED:
        yield
        return

    os.makedirs(os.path.dirname(GLOBAL_TASK_LOCK_PATH), exist_ok=True)
    started = time.time()
    last_log = 0.0

    with open(GLOBAL_TASK_LOCK_PATH, "w", encoding="utf-8") as lock_file:
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                waited = time.time() - started
                if waited - last_log >= 30:
                    print(f"[INFO] Waiting for global task lock for {name}: {int(waited)}s", flush=True)
                    last_log = waited
                time.sleep(GLOBAL_TASK_LOCK_POLL_SECONDS)

        print(f"[INFO] Acquired global task lock for {name}", flush=True)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            print(f"[INFO] Released global task lock for {name}", flush=True)
