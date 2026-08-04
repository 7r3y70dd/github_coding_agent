from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from decimal import Decimal
from typing import Any
from uuid import UUID

import boto3
from boto3.dynamodb.conditions import Key

from cognee_adapter import (
    cognee_status,
    forget_repo_scope,
    repo_scope_datasets,
    repo_scope_snapshot_key,
    seed_repo_scope_files,
    shutdown_cognee_runner,
)
from repo_indexing import (
    dataset_name_for_scope,
    decimal_now_ms,
    file_data_id,
    file_pk,
    file_sk,
    index_pk,
    index_sk,
    normalize_ref,
    normalize_repo,
    normalize_scope_paths,
    run,
    scope_hash,
    selected_files,
    to_dynamo_value,
)
from platform_lock import global_task_lock
from repo_context_snapshot import save_chat_context_snapshot

AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("BEDROCK_REGION", "us-east-1"))
COGNEE_QUEUE_URL = os.environ["COGNEE_QUEUE_URL"]
COGNEE_INDEX_TABLE = os.environ.get("COGNEE_INDEX_TABLE", os.environ.get("RUNS_TABLE", "agent_runs"))
WORKDIR = os.environ.get("COGNEE_WORKDIR", "/var/lib/cognee-indexer/work")
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "5"))
COMMAND_TIMEOUT = int(os.environ.get("COMMAND_TIMEOUT", "900"))
SQS_VISIBILITY_TIMEOUT = int(os.environ.get("COGNEE_SQS_VISIBILITY_TIMEOUT", "14400"))
MAX_INDEX_FILES = int(os.environ.get("COGNEE_INDEX_MAX_FILES", "5000"))
COGNEE_ADD_BATCH_SIZE = int(os.environ.get("COGNEE_ADD_BATCH_SIZE", "20"))
EVENT_LOG = os.environ.get("EVENT_LOG", os.path.join(WORKDIR, "cognee-indexer-events.jsonl"))

sqs = boto3.client("sqs", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
table = dynamodb.Table(COGNEE_INDEX_TABLE)


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


def chunks(items: list[Any], size: int) -> list[list[Any]]:
    size = max(1, size)
    return [items[i : i + size] for i in range(0, len(items), size)]


def put_job_event(job_id: str, event_type: str, message: str, payload: dict[str, Any] | None = None) -> None:
    table.put_item(
        Item=to_dynamo_value(
            {
                "pk": f"COGNEE_JOB#{job_id}",
                "sk": f"EVENT#{now_ms()}",
                "job_id": job_id,
                "ts": now_ms(),
                "event_type": event_type,
                "message": message,
                "payload": payload or {},
            }
        )
    )


def update_job_status(job_id: str, status: str, message: str = "", payload: dict[str, Any] | None = None) -> None:
    table.update_item(
        Key={"pk": f"COGNEE_JOB#{job_id}", "sk": "META"},
        UpdateExpression="SET #status = :status, updated_at = :updated_at, status_message = :message, payload = :payload",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues=to_dynamo_value(
            {
                ":status": status,
                ":updated_at": now_ms(),
                ":message": message,
                ":payload": payload or {},
            }
        ),
    )
    put_job_event(job_id, status, message, payload)


def get_index_meta(repo: str, scope: str) -> dict[str, Any] | None:
    resp = table.get_item(Key={"pk": index_pk(repo), "sk": index_sk(scope)})
    item = resp.get("Item")
    return item if isinstance(item, dict) else None


def update_index_status(
    repo: str,
    scope: str,
    status: str,
    message: str,
    extra: dict[str, Any] | None = None,
) -> None:
    extra = extra or {}
    names = {"#status": "status"}
    values: dict[str, Any] = {
        ":status": status,
        ":updated_at": now_ms(),
        ":message": message,
    }
    set_parts = ["#status = :status", "updated_at = :updated_at", "status_message = :message"]

    for i, (key, value) in enumerate(extra.items()):
        name_key = f"#k{i}"
        value_key = f":v{i}"
        names[name_key] = key
        values[value_key] = value
        set_parts.append(f"{name_key} = {value_key}")

    table.update_item(
        Key={"pk": index_pk(repo), "sk": index_sk(scope)},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=to_dynamo_value(values),
    )


def query_all(pk: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"KeyConditionExpression": Key("pk").eq(pk)}
    while True:
        resp = table.query(**kwargs)
        out.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return out


def load_file_records(repo: str, scope: str) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for item in query_all(file_pk(repo, scope)):
        path = str(item.get("path") or "")
        if path:
            records[path] = item
    return records


def delete_all_file_records(repo: str, scope: str) -> None:
    pk = file_pk(repo, scope)
    items = query_all(pk)
    if not items:
        return
    with table.batch_writer() as batch:
        for item in items:
            batch.delete_item(Key={"pk": item["pk"], "sk": item["sk"]})


def put_file_records(
    repo: str,
    scope: str,
    datasets: dict[str, str],
    snapshot_key: str,
    files: list[dict[str, Any]],
    file_data_ids: dict[str, list[dict[str, str]]],
) -> None:
    if not files:
        return
    ts = decimal_now_ms()
    with table.batch_writer() as batch:
        for f in files:
            rel = str(f["path"])
            data_ids = file_data_ids.get(rel, [])
            batch.put_item(
                Item=to_dynamo_value(
                    {
                        "pk": file_pk(repo, scope),
                        "sk": file_sk(rel),
                        "repo": repo,
                        "scope_hash": scope,
                        "dataset_name": datasets.get("dataset_name", ""),
                        "code_dataset_name": datasets.get("code_dataset_name", ""),
                        "rules_dataset_name": datasets.get("rules_dataset_name", ""),
                        "snapshot_key": snapshot_key,
                        "path": rel,
                        "sha256": f["sha256"],
                        "size": int(f.get("size") or 0),
                        "data_ids": data_ids,
                        "data_ids_supported": bool(data_ids),
                        "updated_at": ts,
                    }
                )
            )


def delete_file_records(repo: str, scope: str, paths: list[str]) -> None:
    if not paths:
        return
    with table.batch_writer() as batch:
        for p in paths:
            batch.delete_item(Key={"pk": file_pk(repo, scope), "sk": file_sk(p)})


def clone_repo(repo: str, ref: str, job_id: str) -> tuple[str, str]:
    repo_fragment = repo.replace("/", "_")
    task_dir = os.path.join(WORKDIR, repo_fragment, job_id)
    if os.path.exists(task_dir):
        shutil.rmtree(task_dir)
    os.makedirs(os.path.dirname(task_dir), exist_ok=True)

    run(["gh", "repo", "clone", repo, task_dir], timeout=COMMAND_TIMEOUT)

    if ref:
        try:
            run(["git", "checkout", ref], cwd=task_dir, timeout=COMMAND_TIMEOUT)
        except Exception:
            run(["git", "fetch", "origin", ref], cwd=task_dir, timeout=COMMAND_TIMEOUT)
            run(["git", "checkout", "FETCH_HEAD"], cwd=task_dir, timeout=COMMAND_TIMEOUT)

    head_sha = run(["git", "rev-parse", "HEAD"], cwd=task_dir, timeout=COMMAND_TIMEOUT)
    return task_dir, head_sha


def make_data_item(full_path: str, rel_path: str, data_id: str, repo: str, scope: str) -> Any:
    try:
        from cognee.tasks.ingestion.data_item import DataItem

        metadata = {"repo": repo, "scope_hash": scope, "path": rel_path}
        try:
            return DataItem(
                data=full_path,
                label=rel_path,
                external_metadata=metadata,
                data_id=UUID(data_id),
            )
        except TypeError:
            return DataItem(data=full_path, label=rel_path, external_metadata=metadata)
    except Exception:
        return full_path


async def cognee_forget_dataset(dataset_name: str) -> None:
    import cognee

    try:
        await cognee.forget(dataset=dataset_name)
    except Exception as e:
        # A first-time full seed may not have an existing dataset yet.
        if "not found" not in str(e).lower() and "does not exist" not in str(e).lower():
            raise


async def cognee_forget_data_item(dataset_name: str, data_id: str) -> None:
    import cognee

    try:
        await cognee.forget(dataset=dataset_name, data_id=UUID(data_id))
    except Exception as e:
        if "not found" not in str(e).lower() and "does not exist" not in str(e).lower():
            raise


async def cognee_add_files(dataset_name: str, repo: str, scope: str, files: list[dict[str, Any]]) -> None:
    import cognee

    for batch in chunks(files, COGNEE_ADD_BATCH_SIZE):
        data = [
            make_data_item(
                full_path=str(f["full_path"]),
                rel_path=str(f["path"]),
                data_id=file_data_id(repo, scope, str(f["path"])),
                repo=repo,
                scope=scope,
            )
            for f in batch
        ]
        await cognee.add(
            data=data,
            dataset_name=dataset_name,
            incremental_loading=True,
            data_per_batch=len(batch),
        )


async def cognee_cognify_dataset(dataset_name: str) -> None:
    import cognee

    await cognee.cognify(datasets=[dataset_name], incremental_loading=True)


def file_has_data_ids(record: dict[str, Any]) -> bool:
    data_ids = record.get("data_ids")
    return isinstance(data_ids, list) and any(isinstance(x, dict) and x.get("data_id") for x in data_ids)


def collect_forget_entries(old_by_path: dict[str, dict[str, Any]], paths: list[str]) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for rel in paths:
        record = old_by_path.get(rel) or {}
        data_ids = record.get("data_ids") or []
        if isinstance(data_ids, list):
            for entry in data_ids:
                if isinstance(entry, dict) and entry.get("dataset_name") and entry.get("data_id"):
                    entries.append({"dataset_name": str(entry["dataset_name"]), "data_id": str(entry["data_id"])})
    return entries


def process_index_job(job: dict[str, Any]) -> None:
    job_id = str(job.get("index_job_id") or job.get("job_id") or "")
    if not job_id:
        raise ValueError("Cognee index job missing index_job_id.")

    repo = normalize_repo(str(job.get("repo") or ""))
    ref = normalize_ref(str(job.get("ref") or "main"))
    include_paths = normalize_scope_paths(job.get("include_paths") or [""])
    exclude_paths = normalize_scope_paths(job.get("exclude_paths") or [])
    requested_mode = str(job.get("mode") or "auto").lower()
    if requested_mode not in {"auto", "full", "incremental"}:
        requested_mode = "auto"

    scope = str(job.get("scope_hash") or scope_hash(repo, ref, include_paths, exclude_paths))
    datasets = repo_scope_datasets(repo, scope)
    snapshot_key = repo_scope_snapshot_key(repo=repo, ref=ref, scope_hash=scope)

    emit_event({"type": "index_job_start", "job_id": job_id, "repo": repo, "scope_hash": scope, "cognee": cognee_status()})
    update_job_status(job_id, "running", "Cognee indexer started.", {"repo": repo, "scope_hash": scope})
    update_index_status(
        repo,
        scope,
        "indexing",
        "Cognee indexer is cloning and analyzing the repository.",
        {
            "repo": repo,
            "ref": ref,
            "scope_hash": scope,
            "snapshot_key": snapshot_key,
            "dataset_name": datasets.get("dataset_name", ""),
            "code_dataset_name": datasets.get("code_dataset_name", ""),
            "rules_dataset_name": datasets.get("rules_dataset_name", ""),
            "include_paths": include_paths,
            "exclude_paths": exclude_paths,
            "last_job_id": job_id,
        },
    )

    task_dir, head_sha = clone_repo(repo, ref, job_id)
    files = selected_files(task_dir, include_paths, exclude_paths)

    if len(files) > MAX_INDEX_FILES:
        raise RuntimeError(
            f"Selected scope has {len(files)} indexable files, above COGNEE_INDEX_MAX_FILES={MAX_INDEX_FILES}. "
            "Narrow the scope or raise the limit."
        )

    old_records = load_file_records(repo, scope)
    old_by_path = old_records
    current_by_path = {str(f["path"]): f for f in files}

    changed_or_new: list[dict[str, Any]] = []
    unchanged_count = 0
    for rel, current in current_by_path.items():
        old = old_by_path.get(rel)
        if not old or str(old.get("sha256") or "") != str(current.get("sha256") or ""):
            changed_or_new.append(current)
        else:
            unchanged_count += 1

    removed_paths = sorted(set(old_by_path.keys()) - set(current_by_path.keys()))
    changed_paths = [str(f["path"]) for f in changed_or_new]
    changed_existing_paths = [p for p in changed_paths if p in old_by_path]
    paths_requiring_forget = removed_paths + changed_existing_paths
    missing_data_id_paths = [p for p in paths_requiring_forget if not file_has_data_ids(old_by_path.get(p, {}))]

    if requested_mode == "full":
        effective_mode = "full"
        reason = "User requested full reseed."
    elif not old_records:
        effective_mode = "full"
        reason = "No previous file manifest exists for this scope."
    elif missing_data_id_paths:
        effective_mode = "full"
        reason = "Existing records do not have per-file Cognee data IDs, so the selected scope will be refreshed safely."
    else:
        effective_mode = "incremental"
        reason = "Existing file manifest found; processing only changed and removed files."

    update_job_status(
        job_id,
        "running",
        f"Index plan ready: {effective_mode} seed.",
        {
            "effective_mode": effective_mode,
            "reason": reason,
            "selected_file_count": len(files),
            "changed_or_new_count": len(changed_or_new),
            "removed_count": len(removed_paths),
            "unchanged_count": unchanged_count,
            "head_sha": head_sha,
            "snapshot_key": snapshot_key,
        },
    )

    if effective_mode == "full":
        forget_repo_scope(repo, scope)
        delete_all_file_records(repo, scope)
        seed_result = seed_repo_scope_files(
            repo_root=task_dir,
            repo=repo,
            scope_hash=scope,
            snapshot_key=snapshot_key,
            files=files,
            reset=False,
        )
        put_file_records(repo, scope, datasets, snapshot_key, files, seed_result.get("file_data_ids", {}))
        changed_count = len(files)
        removed_count = len(old_records)
    else:
        forget_entries = collect_forget_entries(old_by_path, paths_requiring_forget)
        seed_result = seed_repo_scope_files(
            repo_root=task_dir,
            repo=repo,
            scope_hash=scope,
            snapshot_key=snapshot_key,
            files=changed_or_new,
            reset=False,
            forget_data_ids=forget_entries,
        )
        delete_file_records(repo, scope, removed_paths)
        put_file_records(repo, scope, datasets, snapshot_key, changed_or_new, seed_result.get("file_data_ids", {}))
        changed_count = len(changed_or_new)
        removed_count = len(removed_paths)

    data_ids_supported = bool(seed_result.get("data_ids_supported", True))
    code_payload_count = int(seed_result.get("code_payload_count") or 0)
    rules_payload_count = int(seed_result.get("rules_payload_count") or 0)

    completed_at = now_ms()
    chat_snapshot = save_chat_context_snapshot(
        repo=repo,
        scope_hash=scope,
        snapshot_key=snapshot_key,
        head_sha=head_sha,
        datasets=datasets,
        files=files,
    )

    update_index_status(
        repo,
        scope,
        "ready",
        f"Cognee index ready: {effective_mode} completed.",
        {
            "repo": repo,
            "ref": ref,
            "head_sha": head_sha,
            "snapshot_key": snapshot_key,
            "scope_hash": scope,
            "dataset_name": datasets.get("dataset_name", ""),
            "code_dataset_name": datasets.get("code_dataset_name", ""),
            "rules_dataset_name": datasets.get("rules_dataset_name", ""),
            "include_paths": include_paths,
            "exclude_paths": exclude_paths,
            "effective_mode": effective_mode,
            "selected_file_count": len(files),
            "changed_count": changed_count,
            "removed_count": removed_count,
            "unchanged_count": unchanged_count,
            "code_payload_count": code_payload_count,
            "rules_payload_count": rules_payload_count,
            "data_ids_supported": data_ids_supported,
            "chat_snapshot": chat_snapshot,
            "last_indexed_at": completed_at,
        },
    )
    update_job_status(
        job_id,
        "success",
        f"Cognee index ready: {effective_mode} completed.",
        {
            "repo": repo,
            "scope_hash": scope,
            "snapshot_key": snapshot_key,
            "dataset_name": datasets.get("dataset_name", ""),
            "code_dataset_name": datasets.get("code_dataset_name", ""),
            "rules_dataset_name": datasets.get("rules_dataset_name", ""),
            "effective_mode": effective_mode,
            "selected_file_count": len(files),
            "changed_count": changed_count,
            "removed_count": removed_count,
            "unchanged_count": unchanged_count,
            "code_payload_count": code_payload_count,
            "rules_payload_count": rules_payload_count,
            "data_ids_supported": data_ids_supported,
        },
    )
    emit_event(
        {
            "type": "index_job_done",
            "job_id": job_id,
            "repo": repo,
            "scope_hash": scope,
            "effective_mode": effective_mode,
            "selected_file_count": len(files),
            "changed_count": changed_count,
            "removed_count": removed_count,
        }
    )


def process_message(msg: dict[str, Any]) -> None:
    body = json.loads(msg["Body"])
    if body.get("type") not in {None, "cognee_reseed", "cognee_index"}:
        print(f"[WARN] Ignoring unknown Cognee message type: {body.get('type')}", flush=True)
        return
    process_index_job(body)


def single_job_mode_enabled() -> bool:
    return str(os.environ.get("COGNEE_INDEXER_SINGLE_JOB", "1")).lower() in {"1", "true", "yes", "on"}

def main() -> None:
    run(["gh", "auth", "status"], timeout=COMMAND_TIMEOUT)
    os.makedirs(WORKDIR, exist_ok=True)
    print("Cognee indexer started. Waiting for SQS jobs...", flush=True)

    while True:
        response = sqs.receive_message(
            QueueUrl=COGNEE_QUEUE_URL,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
            VisibilityTimeout=SQS_VISIBILITY_TIMEOUT,
        )
        messages = response.get("Messages", [])
        if not messages:
            time.sleep(POLL_SECONDS)
            continue

        for msg in messages:
            receipt_handle = msg["ReceiptHandle"]
            job: dict[str, Any] = {}
            try:
                job = json.loads(msg["Body"])
                with global_task_lock("cognee-indexer"):
                    process_index_job(job)
                sqs.delete_message(QueueUrl=COGNEE_QUEUE_URL, ReceiptHandle=receipt_handle)
                print("[INFO] Completed Cognee SQS job.", flush=True)
                if single_job_mode_enabled():
                    print("[INFO] COGNEE_INDEXER_SINGLE_JOB enabled; exiting to release Cognee/Ladybug DB handles.", flush=True)
                    return
            except Exception as e:
                print(f"[ERROR] Cognee index job failed: {e}", flush=True)
                emit_event({"type": "index_job_error", "error": str(e), "job": job})
                job_id = str(job.get("index_job_id") or job.get("job_id") or "")
                repo = str(job.get("repo") or "")
                scope = str(job.get("scope_hash") or "")
                if job_id:
                    try:
                        update_job_status(job_id, "error", str(e), {"job": job})
                    except Exception:
                        pass
                if repo and scope:
                    try:
                        update_index_status(repo, scope, "error", str(e), {"last_error_at": now_ms()})
                    except Exception:
                        pass
                # Delete after recording the failure so poison messages do not loop forever.
                sqs.delete_message(QueueUrl=COGNEE_QUEUE_URL, ReceiptHandle=receipt_handle)
                if single_job_mode_enabled():
                    print("[INFO] COGNEE_INDEXER_SINGLE_JOB enabled after failed job; exiting to release Cognee/Ladybug DB handles.", flush=True)
                    return


if __name__ == "__main__":
    try:
        main()
    finally:
        shutdown_cognee_runner()
