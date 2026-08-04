from __future__ import annotations

import gzip
import json
import os
import re
import time
from typing import Any

import boto3

from repo_indexing import to_dynamo_value

AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("BEDROCK_REGION", "us-east-1"))
AWS_ENDPOINT_URL = os.environ.get("AWS_ENDPOINT_URL", "").strip() or None
RUNS_TABLE = os.environ.get("RUNS_TABLE", os.environ.get("COGNEE_INDEX_TABLE", "agent_runs"))
CHAT_CONTEXT_BUCKET = os.environ.get("CHAT_CONTEXT_BUCKET", "")
CHAT_CONTEXT_PREFIX = os.environ.get("CHAT_CONTEXT_PREFIX", "repo-chat-context")
CHAT_SNAPSHOT_MAX_FILES = int(os.environ.get("CHAT_SNAPSHOT_MAX_FILES", "120"))
CHAT_SNAPSHOT_FILE_CHARS = int(os.environ.get("CHAT_SNAPSHOT_FILE_CHARS", "6000"))
CHAT_CONTEXT_MAX_CHARS = int(os.environ.get("CHAT_CONTEXT_MAX_CHARS", "24000"))

_aws_kwargs = {"region_name": AWS_REGION}
if AWS_ENDPOINT_URL:
    _aws_kwargs["endpoint_url"] = AWS_ENDPOINT_URL

s3 = boto3.client("s3", **_aws_kwargs)
dynamodb = boto3.resource("dynamodb", **_aws_kwargs)
table = dynamodb.Table(RUNS_TABLE)


def _safe_part(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9_.-]+", "_", value).strip("_")
    return value[:160] or "default"


def chat_context_pk(repo: str) -> str:
    return f"CHAT_CONTEXT#{repo}"


def chat_context_sk(scope_hash: str) -> str:
    return f"SCOPE#{scope_hash or 'default'}"


def _read_text(path: str, max_chars: int) -> str:
    try:
        with open(path, "rb") as f:
            raw = f.read(max_chars + 1)
        if b"\x00" in raw:
            return ""
        return raw.decode("utf-8", errors="replace")[:max_chars]
    except Exception:
        return ""


def save_chat_context_snapshot(
    *,
    repo: str,
    scope_hash: str,
    snapshot_key: str,
    head_sha: str,
    datasets: dict[str, Any],
    files: list[dict[str, Any]],
) -> dict[str, Any]:
    if not CHAT_CONTEXT_BUCKET:
        return {"saved": False, "reason": "CHAT_CONTEXT_BUCKET is not configured."}

    records: list[dict[str, str]] = []

    for idx, f in enumerate(files[:CHAT_SNAPSHOT_MAX_FILES], start=1):
        rel = str(f.get("path") or "")
        full_path = str(f.get("full_path") or "")
        text = _read_text(full_path, CHAT_SNAPSHOT_FILE_CHARS)
        if not text.strip():
            continue
        records.append({"source_id": f"S{idx}", "path": rel, "text": text})

    body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    gz = gzip.compress(body.encode("utf-8"))

    owner_repo = repo.replace("/", "_")
    s3_key = f"{CHAT_CONTEXT_PREFIX}/{owner_repo}/{_safe_part(scope_hash)}/{_safe_part(snapshot_key)}.jsonl.gz"

    s3.put_object(
        Bucket=CHAT_CONTEXT_BUCKET,
        Key=s3_key,
        Body=gz,
        ContentType="application/jsonl",
        ContentEncoding="gzip",
    )

    ts = int(time.time() * 1000)

    item = {
        "pk": chat_context_pk(repo),
        "sk": chat_context_sk(scope_hash),
        "repo": repo,
        "scope_hash": scope_hash,
        "snapshot_key": snapshot_key,
        "head_sha": head_sha,
        "dataset_name": datasets.get("dataset_name", ""),
        "code_dataset_name": datasets.get("code_dataset_name", ""),
        "rules_dataset_name": datasets.get("rules_dataset_name", ""),
        "s3_bucket": CHAT_CONTEXT_BUCKET,
        "s3_key": s3_key,
        "source_count": len(records),
        "created_at": ts,
        "updated_at": ts,
    }

    table.put_item(Item=to_dynamo_value(item))

    return {
        "saved": True,
        "s3_bucket": CHAT_CONTEXT_BUCKET,
        "s3_key": s3_key,
        "source_count": len(records),
    }


def load_chat_context_snapshot(repo: str, scope_hash: str) -> dict[str, Any] | None:
    resp = table.get_item(Key={"pk": chat_context_pk(repo), "sk": chat_context_sk(scope_hash)})
    item = resp.get("Item")
    return item if isinstance(item, dict) else None


def load_chat_context_text(
    repo: str,
    scope_hash: str,
    max_chars: int | None = None,
) -> tuple[str, list[dict[str, str]], dict[str, Any] | None]:
    meta = load_chat_context_snapshot(repo, scope_hash)
    if not meta:
        return "", [], None

    bucket = str(meta.get("s3_bucket") or "")
    key = str(meta.get("s3_key") or "")

    if not bucket or not key:
        return "", [], meta

    obj = s3.get_object(Bucket=bucket, Key=key)
    raw = obj["Body"].read()

    try:
        text = gzip.decompress(raw).decode("utf-8", errors="replace")
    except Exception:
        text = raw.decode("utf-8", errors="replace")

    limit = max_chars or CHAT_CONTEXT_MAX_CHARS
    sources: list[dict[str, str]] = []
    parts: list[str] = []
    used = 0

    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue

        sid = str(rec.get("source_id") or "")
        path = str(rec.get("path") or "")
        body = str(rec.get("text") or "")
        block = f"[{sid}] path={path}\n{body}\n"

        if used + len(block) > limit:
            break

        used += len(block)
        parts.append(block)
        sources.append({"source_id": sid, "path": path})

    return "\n".join(parts), sources, meta
