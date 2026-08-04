from __future__ import annotations

import os
import re
import time
import uuid
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

from repo_context_snapshot import load_chat_context_text
from repo_indexing import to_dynamo_value

AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("BEDROCK_REGION", "us-east-1"))
RUNS_TABLE = os.environ.get("RUNS_TABLE", "agent_runs")

BEDROCK_CHAT_MODEL = os.environ.get(
    "BEDROCK_CHAT_MODEL",
    os.environ.get("BEDROCK_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
)
CHAT_MAX_TOKENS = int(os.environ.get("CHAT_MAX_TOKENS", "4000"))
CHAT_TEMPERATURE = float(os.environ.get("CHAT_TEMPERATURE", os.environ.get("BEDROCK_TEMPERATURE", "0.1")))
CHAT_HISTORY_USER_TURNS = int(os.environ.get("CHAT_HISTORY_USER_TURNS", "3"))
CHAT_CONTEXT_MAX_CHARS = int(os.environ.get("CHAT_CONTEXT_MAX_CHARS", "24000"))

bedrock = boto3.client("bedrock-runtime", region_name=AWS_REGION)
dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
table = dynamodb.Table(RUNS_TABLE)


def chat_pk(user_email: str, repo: str, scope_hash: str) -> str:
    user = (user_email or "anonymous").strip().lower() or "anonymous"
    return f"REPO_CHAT#{user}#{repo}#{scope_hash or 'default'}"


def save_chat_message(user_email: str, repo: str, scope_hash: str, role: str, content: str) -> None:
    ts = int(time.time() * 1000)
    table.put_item(
        Item=to_dynamo_value(
            {
                "pk": chat_pk(user_email, repo, scope_hash),
                "sk": f"MSG#{ts}#{uuid.uuid4()}",
                "repo": repo,
                "scope_hash": scope_hash,
                "role": role,
                "content": content,
                "created_at": ts,
            }
        )
    )


def load_recent_chat_messages(user_email: str, repo: str, scope_hash: str) -> list[dict[str, str]]:
    resp = table.query(
        KeyConditionExpression=Key("pk").eq(chat_pk(user_email, repo, scope_hash)) & Key("sk").begins_with("MSG#"),
        ScanIndexForward=False,
        Limit=20,
    )

    newest = resp.get("Items", [])
    picked_rev: list[dict[str, str]] = []
    user_turns = 0

    for item in newest:
        role = str(item.get("role") or "")
        content = str(item.get("content") or "")
        if not role or not content:
            continue

        picked_rev.append({"role": role, "content": content})

        if role == "user":
            user_turns += 1

        if user_turns >= CHAT_HISTORY_USER_TURNS:
            break

    return list(reversed(picked_rev))


def _bedrock_text(messages: list[dict[str, Any]], system_text: str) -> str:
    resp = bedrock.converse(
        modelId=BEDROCK_CHAT_MODEL,
        system=[{"text": system_text}],
        messages=messages,
        inferenceConfig={
            "maxTokens": CHAT_MAX_TOKENS,
            "temperature": CHAT_TEMPERATURE,
        },
    )

    blocks = resp.get("output", {}).get("message", {}).get("content", [])
    return "\n".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict)).strip()


def answer_repo_chat(user_email: str, repo: str, scope_hash: str, message: str) -> dict[str, Any]:
    message = (message or "").strip()

    if not message:
        raise ValueError("Message is required.")

    context_text, sources, context_meta = load_chat_context_text(repo, scope_hash, CHAT_CONTEXT_MAX_CHARS)
    history = load_recent_chat_messages(user_email, repo, scope_hash)

    system_text = f"""You are Cognimoss Repo Chat, a repository-aware assistant.

Answer questions about repository {repo}.
Use the provided repository context when it is relevant.
Cite repository sources inline using [S1], [S2], etc.
Only cite source ids that appear in the context.
If the context is insufficient, say what is missing and give the best next command or file to inspect.
Be concise and practical.

Repository context:
{context_text if context_text else '(No saved repo context snapshot is available for this scope.)'}
"""

    bedrock_messages: list[dict[str, Any]] = []

    for h in history:
        role = "assistant" if h["role"] == "assistant" else "user"
        bedrock_messages.append({"role": role, "content": [{"text": h["content"]}]})

    bedrock_messages.append({"role": "user", "content": [{"text": message}]})

    answer = _bedrock_text(bedrock_messages, system_text)
    cited = sorted(set(re.findall(r"\[(S\d+)\]", answer)))

    save_chat_message(user_email, repo, scope_hash, "user", message)
    save_chat_message(user_email, repo, scope_hash, "assistant", answer)

    return {
        "repo": repo,
        "scope_hash": scope_hash,
        "answer": answer,
        "cited_source_ids": cited,
        "sources": [s for s in sources if not cited or s.get("source_id") in cited],
        "context_available": bool(context_text),
        "context_snapshot": context_meta or {},
        "model": BEDROCK_CHAT_MODEL,
    }
