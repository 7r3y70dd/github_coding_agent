from __future__ import annotations

import html
import json
import os
import re
import shutil
import time
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import boto3

from platform_lock import global_task_lock
from repo_indexing import normalize_ref, normalize_repo, run, to_dynamo_value
from worker import get_ready_cognee_index, search_issue_context_scoped


AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("BEDROCK_REGION", "us-east-1"))
DEBUG_QUEUE_URL = os.environ["DEBUG_QUEUE_URL"]
RUNS_TABLE = os.environ.get("RUNS_TABLE", "agent_runs")

DEBUG_WORKDIR = os.environ.get("DEBUG_WORKDIR", "/var/lib/debug-runner/work")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "5"))
DEBUG_SQS_VISIBILITY_TIMEOUT = int(os.environ.get("DEBUG_SQS_VISIBILITY_TIMEOUT", "14400"))
DEBUG_TEST_TIMEOUT = int(os.environ.get("DEBUG_TEST_TIMEOUT", "900"))
DEBUG_MAX_FAILURE_OUTPUT_CHARS = int(os.environ.get("DEBUG_MAX_FAILURE_OUTPUT_CHARS", "12000"))

DEBUG_INSTALL_PYTEST = os.environ.get("DEBUG_INSTALL_PYTEST", "1") == "1"
DEBUG_INSTALL_REQUIREMENTS = os.environ.get("DEBUG_INSTALL_REQUIREMENTS", "0") == "1"
DEBUG_VENV_DIR = os.environ.get("DEBUG_VENV_DIR", ".venv")

EVENT_LOG = os.environ.get("DEBUG_EVENT_LOG", os.path.join(DEBUG_WORKDIR, "debug-events.jsonl"))

sqs = boto3.client("sqs", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
table = dynamodb.Table(RUNS_TABLE)


def now_ms() -> int:
    return int(time.time() * 1000)


def emit_event(event: dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(EVENT_LOG), exist_ok=True)
        event = dict(event)
        event["ts"] = time.time()
        with open(EVENT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def update_debug_status(
    debug_run_id: str,
    status: str,
    message: str,
    extra: dict[str, Any] | None = None,
) -> None:
    values: dict[str, Any] = {
        ":status": status,
        ":updated_at": Decimal(now_ms()),
        ":message": message,
    }
    names = {"#status": "status"}
    sets = ["#status = :status", "updated_at = :updated_at", "status_message = :message"]

    for i, (k, v) in enumerate((extra or {}).items()):
        nk = f"#k{i}"
        vk = f":v{i}"
        names[nk] = k
        values[vk] = to_dynamo_value(v)
        sets.append(f"{nk} = {vk}")

    table.update_item(
        Key={"pk": f"DEBUG_RUN#{debug_run_id}", "sk": "META"},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def put_debug_event(
    debug_run_id: str,
    event_type: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> None:
    ts = now_ms()

    table.put_item(
        Item=to_dynamo_value(
            {
                "pk": f"DEBUG_RUN#{debug_run_id}",
                "sk": f"EVENT#{ts}",
                "debug_run_id": debug_run_id,
                "ts": ts,
                "event_type": event_type,
                "message": message,
                "payload": payload or {},
            }
        )
    )

    emit_event(
        {
            "type": event_type,
            "debug_run_id": debug_run_id,
            "message": message,
            "payload": payload or {},
        }
    )


def sanitize_repo_for_path(repo: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", repo)


def clone_repo(repo: str, ref: str, debug_run_id: str) -> str:
    task_dir = os.path.join(DEBUG_WORKDIR, sanitize_repo_for_path(repo), debug_run_id)

    if os.path.exists(task_dir):
        shutil.rmtree(task_dir)

    os.makedirs(os.path.dirname(task_dir), exist_ok=True)

    run(["gh", "repo", "clone", repo, task_dir], timeout=900)

    if ref:
        try:
            run(["git", "checkout", ref], cwd=task_dir, timeout=900)
        except Exception:
            run(["git", "fetch", "origin", ref], cwd=task_dir, timeout=900)
            run(["git", "checkout", "FETCH_HEAD"], cwd=task_dir, timeout=900)

    return task_dir


def prepare_pytest_env(repo_path: str) -> str:
    py = os.path.join(repo_path, DEBUG_VENV_DIR, "bin", "python")

    if not os.path.exists(py):
        run(["python3", "-m", "venv", DEBUG_VENV_DIR], cwd=repo_path, timeout=300)

    if DEBUG_INSTALL_PYTEST:
        run([py, "-m", "pip", "install", "-U", "pip"], cwd=repo_path, timeout=600)
        run([py, "-m", "pip", "install", "pytest"], cwd=repo_path, timeout=600)

    if DEBUG_INSTALL_REQUIREMENTS and os.path.exists(os.path.join(repo_path, "requirements.txt")):
        run([py, "-m", "pip", "install", "-r", "requirements.txt"], cwd=repo_path, timeout=1800)

    return py


def discover_pytest_suites(repo_path: str) -> list[str]:
    tests_dir = Path(repo_path) / "tests"

    if not tests_dir.is_dir():
        return []

    suites: list[str] = []

    for p in sorted(tests_dir.rglob("*.py")):
        name = p.name
        if name.startswith("test_") or name.endswith("_test.py"):
            suites.append(str(p.relative_to(repo_path)).replace("\\", "/"))

    return suites


def run_suite(repo_path: str, py: str, suite: str) -> dict[str, Any]:
    try:
        out = run([py, "-m", "pytest", "-q", suite], cwd=repo_path, timeout=DEBUG_TEST_TIMEOUT)
        return {
            "suite": suite,
            "status": "passed",
            "exit_code": 0,
            "output": out[-DEBUG_MAX_FAILURE_OUTPUT_CHARS:],
        }
    except Exception as e:
        return {
            "suite": suite,
            "status": "failed",
            "exit_code": 1,
            "output": str(e)[-DEBUG_MAX_FAILURE_OUTPUT_CHARS:],
        }


def gh_create_issue(repo: str, title: str, body: str, labels: list[str] | None = None) -> str:
    args = ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body]

    for label in labels or []:
        args.extend(["--label", label])

    try:
        return run(args, timeout=900).strip()
    except Exception:
        return run(
            ["gh", "issue", "create", "--repo", repo, "--title", title, "--body", body],
            timeout=900,
        ).strip()


def create_failure_issue(repo: str, ref: str, result: dict[str, Any], context: str) -> str:
    suite = result["suite"]
    title = f"Debug: {suite} failing"
    output = html.escape(result.get("output", ""))

    body = f"""## Debug suite failure

Repository: `{repo}`

Ref: `{ref}`

Suite: `{suite}`

This issue was generated by the Cognimoss Repo Debugger. It groups failures by test suite/file, not by individual test case.

## Failure output

<details>
<summary>Click to expand pytest output</summary>

<pre>
{output}
</pre>

</details>

## Relevant Cognee context

{context if context else "No Cognee context was available for this suite."}
"""

    return gh_create_issue(repo, title, body, labels=["debug", "test-failure", "cognimoss"])


def process_debug_job(job: dict[str, Any]) -> None:
    debug_run_id = str(job.get("debug_run_id") or job.get("run_id") or uuid.uuid4())
    repo = normalize_repo(str(job.get("repo") or ""))
    ref = normalize_ref(str(job.get("ref") or "main"))
    scope_hash = str(job.get("cognee_scope_hash") or job.get("scope_hash") or "")

    put_debug_event(
        debug_run_id,
        "debug_start",
        "Starting debug run.",
        {"repo": repo, "ref": ref, "scope_hash": scope_hash},
    )
    update_debug_status(debug_run_id, "running", "Debug worker started.")

    task_dir = clone_repo(repo, ref, debug_run_id)
    py = prepare_pytest_env(task_dir)
    suites = discover_pytest_suites(task_dir)

    if not suites:
        update_debug_status(debug_run_id, "success", "No pytest suites found.", {"suite_count": 0})
        put_debug_event(debug_run_id, "debug_done", "No pytest suites found.")
        return

    ready_index = get_ready_cognee_index(repo, scope_hash)
    results: list[dict[str, Any]] = []
    issue_urls: list[str] = []

    for suite in suites:
        put_debug_event(debug_run_id, "suite_start", f"Running {suite}.", {"suite": suite})

        result = run_suite(task_dir, py, suite)
        results.append(result)

        put_debug_event(debug_run_id, "suite_done", f"{suite}: {result['status']}", result)

        if result["status"] == "failed":
            context = ""

            if ready_index:
                try:
                    context = search_issue_context_scoped(
                        f"Test suite failing: {suite}",
                        result.get("output", ""),
                        [suite],
                        repo=repo,
                        index_record=ready_index,
                    )
                except Exception as e:
                    context = f"Cognee context lookup failed: {e}"

            url = create_failure_issue(repo, ref, result, context)
            issue_urls.append(url)

    failed = [r for r in results if r["status"] == "failed"]

    update_debug_status(
        debug_run_id,
        "failed" if failed else "success",
        f"Debug run complete: {len(failed)} failing suites out of {len(results)}.",
        {
            "suite_count": len(results),
            "failed_suite_count": len(failed),
            "issue_urls": issue_urls,
        },
    )

    put_debug_event(
        debug_run_id,
        "debug_done",
        "Debug run completed.",
        {
            "issue_urls": issue_urls,
            "failed_suite_count": len(failed),
        },
    )


def single_job_mode_enabled() -> bool:
    return str(os.environ.get("DEBUG_WORKER_SINGLE_JOB", "1")).lower() in {"1", "true", "yes", "on"}


def main() -> None:
    run(["gh", "auth", "status"], timeout=900)
    os.makedirs(DEBUG_WORKDIR, exist_ok=True)

    print("Debug worker started. Waiting for SQS jobs...", flush=True)

    while True:
        response = sqs.receive_message(
            QueueUrl=DEBUG_QUEUE_URL,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
            VisibilityTimeout=DEBUG_SQS_VISIBILITY_TIMEOUT,
        )

        messages = response.get("Messages", [])

        if not messages:
            time.sleep(POLL_SECONDS)
            continue

        for msg in messages:
            receipt = msg["ReceiptHandle"]
            job: dict[str, Any] = {}

            try:
                job = json.loads(msg["Body"])

                with global_task_lock("debug-worker"):
                    process_debug_job(job)

                sqs.delete_message(QueueUrl=DEBUG_QUEUE_URL, ReceiptHandle=receipt)
                print("[INFO] Completed debug SQS job.", flush=True)

                if single_job_mode_enabled():
                    print("[INFO] DEBUG_WORKER_SINGLE_JOB enabled; exiting.", flush=True)
                    return

            except Exception as e:
                print(f"[ERROR] Debug job failed: {e}", flush=True)

                debug_run_id = str(job.get("debug_run_id") or job.get("run_id") or "")

                if debug_run_id:
                    try:
                        update_debug_status(debug_run_id, "error", str(e), {"job": job})
                        put_debug_event(debug_run_id, "error", "Debug worker failed.", {"error": str(e)})
                    except Exception:
                        pass

                sqs.delete_message(QueueUrl=DEBUG_QUEUE_URL, ReceiptHandle=receipt)


if __name__ == "__main__":
    main()
