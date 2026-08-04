from __future__ import annotations
import difflib
import json
import os
import re
import time
from typing import Optional

import boto3
import requests

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")

OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b-instruct-q4_K_M")
PLANNER_MODEL = os.environ.get("PLANNER_MODEL", OLLAMA_MODEL)
WRITER_MODEL = os.environ.get("WRITER_MODEL", OLLAMA_MODEL)

MAX_FILES_TO_READ = int(os.environ.get("MAX_FILES_TO_READ", "8"))

EVENT_LOG = os.environ.get("EVENT_LOG")

MODEL_PROVIDER = os.environ.get("MODEL_PROVIDER", "ollama").lower()

BEDROCK_REGION = os.environ.get("BEDROCK_REGION", os.environ.get("AWS_REGION", "us-east-1"))
BEDROCK_MODEL = os.environ.get("BEDROCK_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
BEDROCK_PLANNER_MODEL = os.environ.get("BEDROCK_PLANNER_MODEL", BEDROCK_MODEL)
BEDROCK_WRITER_MODEL = os.environ.get("BEDROCK_WRITER_MODEL", BEDROCK_MODEL)

BEDROCK_MAX_TOKENS = int(os.environ.get("BEDROCK_MAX_TOKENS", "6000"))
PLANNER_MAX_TOKENS = int(os.environ.get("PLANNER_MAX_TOKENS", str(BEDROCK_MAX_TOKENS)))
WRITER_MAX_TOKENS = int(os.environ.get("WRITER_MAX_TOKENS", str(BEDROCK_MAX_TOKENS)))


def emit_event(event: dict) -> None:
    if not EVENT_LOG:
        return
    try:
        event = dict(event)
        event["ts"] = time.time()
        with open(EVENT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


def parse_json_loose(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("Empty model output (expected JSON).")

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    if start == -1:
        raise ValueError("Model output was not valid JSON (no '{' found).")

    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue

        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    break

    preview = text[:800]
    tail = text[-800:] if len(text) > 800 else ""
    emit_event(
        {
            "type": "json_parse_fail",
            "head": text[:500],
            "tail": text[-500:],
        }
    )
    raise ValueError(f"Model output was not valid JSON. Head:\n{preview}\n\nTail:\n{tail}")


def ollama_generate(
    prompt: str,
    model: Optional[str] = None,
    think: Optional[bool] = None,
    num_ctx: Optional[int] = None,
) -> str:
    use_model = model or OLLAMA_MODEL
    url = f"{OLLAMA_URL}/api/generate"

    keep_alive = os.environ.get("OLLAMA_KEEP_ALIVE", "15m")

    if think is None:
        think = False

    if num_ctx is None:
        num_ctx = int(os.environ.get("OLLAMA_NUM_CTX", "8192"))

    num_predict = int(os.environ.get("OLLAMA_NUM_PREDICT", "900"))
    temperature = float(os.environ.get("OLLAMA_TEMPERATURE", "0.2"))
    timeout_s = int(os.environ.get("OLLAMA_TIMEOUT", "900"))
    max_tries = int(os.environ.get("OLLAMA_RETRIES", "3"))
    backoff = float(os.environ.get("OLLAMA_BACKOFF", "5"))

    # qwen3-coder on your setup does not support thinking.
    if use_model.startswith("qwen3-coder"):
        think = False

    payload = {
        "model": use_model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "keep_alive": keep_alive,
        "options": {
            "num_predict": num_predict,
            "temperature": temperature,
            "num_ctx": num_ctx,
        },
    }

    if think:
        payload["think"] = True

    last_err: Exception | None = None
    for attempt in range(1, max_tries + 1):
        start = time.time()
        try:
            emit_event(
                {
                    "type": "llm_request",
                    "provider": "ollama",
                    "model": use_model,
                    "prompt_chars": len(prompt or ""),
                    "num_predict": num_predict,
                    "temperature": temperature,
                    "num_ctx": num_ctx,
                    "think": think,
                    "keep_alive": keep_alive,
                }
            )

            r = requests.post(url, json=payload, timeout=(10, timeout_s))

            if not r.ok:
                print(f"[ERROR] Ollama HTTP {r.status_code}: {r.text}", flush=True)
                r.raise_for_status()

            data = r.json()
            resp = data.get("response") or data.get("text") or json.dumps(data)
            return resp

        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = e
            sleep_s = backoff * (2 ** (attempt - 1))
            print(
                f"[WARN] Ollama request failed after {time.time() - start:.1f}s "
                f"(attempt {attempt}/{max_tries}): {e}. Sleeping {sleep_s:.1f}s",
                flush=True,
            )
            time.sleep(sleep_s)

        except requests.HTTPError as e:
            last_err = e
            print(f"[ERROR] Ollama bad HTTP response: {e}", flush=True)
            raise

    raise RuntimeError(f"Ollama failed after {max_tries} tries: {last_err}")


def bedrock_generate(
    prompt: str,
    model: Optional[str] = None,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> str:
    use_model = model or BEDROCK_MODEL

    if max_tokens is None:
        max_tokens = BEDROCK_MAX_TOKENS

    if temperature is None:
        temperature = float(os.environ.get("BEDROCK_TEMPERATURE", os.environ.get("OLLAMA_TEMPERATURE", "0.2")))

    timeout_s = int(os.environ.get("BEDROCK_TIMEOUT", "900"))
    max_tries = int(os.environ.get("BEDROCK_RETRIES", "3"))
    backoff = float(os.environ.get("BEDROCK_BACKOFF", "5"))

    config = None
    try:
        from botocore.config import Config

        config = Config(read_timeout=timeout_s, connect_timeout=10, retries={"max_attempts": 0})
    except Exception:
        config = None

    client_kwargs = {"region_name": BEDROCK_REGION}
    if config is not None:
        client_kwargs["config"] = config

    client = boto3.client("bedrock-runtime", **client_kwargs)

    last_err: Exception | None = None
    for attempt in range(1, max_tries + 1):
        start = time.time()
        try:
            emit_event(
                {
                    "type": "bedrock_request",
                    "provider": "bedrock",
                    "model": use_model,
                    "prompt_chars": len(prompt or ""),
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                }
            )

            response = client.converse(
                modelId=use_model,
                messages=[
                    {
                        "role": "user",
                        "content": [{"text": prompt}],
                    }
                ],
                inferenceConfig={
                    "maxTokens": max_tokens,
                    "temperature": temperature,
                },
            )

            message = response.get("output", {}).get("message", {})
            content = message.get("content", [])

            texts: list[str] = []
            for block in content:
                if isinstance(block, dict) and "text" in block:
                    texts.append(block["text"])

            result = "\n".join(texts).strip()

            emit_event(
                {
                    "type": "bedrock_response",
                    "model": use_model,
                    "response_chars": len(result),
                    "elapsed_s": round(time.time() - start, 3),
                }
            )

            if not result:
                raise RuntimeError(f"Bedrock returned no text for model {use_model}")

            return result

        except Exception as e:
            last_err = e
            if attempt >= max_tries:
                break
            sleep_s = backoff * (2 ** (attempt - 1))
            print(
                f"[WARN] Bedrock request failed after {time.time() - start:.1f}s "
                f"(attempt {attempt}/{max_tries}): {e}. Sleeping {sleep_s:.1f}s",
                flush=True,
            )
            time.sleep(sleep_s)

    raise RuntimeError(f"Bedrock failed after {max_tries} tries: {last_err}")


def generate_text(
    prompt: str,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    think: Optional[bool] = None,
    num_ctx: Optional[int] = None,
    max_tokens: Optional[int] = None,
) -> str:
    use_provider = (provider or os.environ.get("MODEL_PROVIDER", MODEL_PROVIDER)).lower()

    if use_provider == "bedrock":
        return bedrock_generate(prompt, model=model, max_tokens=max_tokens)

    if use_provider == "ollama":
        return ollama_generate(prompt, model=model, think=think, num_ctx=num_ctx)

    raise ValueError(f"Unsupported MODEL_PROVIDER: {use_provider}")


def plan_stage1_prompt(
    issue_title: str,
    issue_body: str,
    repo_tree: str,
    hinted_files: list[str],
    cognee_context: str = "",
) -> str:
    return f"""You are a coding agent working in a git repository.
You will propose a plan and decide which files you must read before editing.

Repository tree (partial):
{repo_tree}

GitHub issue:
Title: {issue_title}

Body:
{issue_body}

Hints (paths mentioned in issue; you may add/remove as needed):
{chr(10).join("- " + p for p in hinted_files) if hinted_files else "(none)"}

Cognee context bundle:
{cognee_context if cognee_context else "(none)"}

Return a JSON object with this schema:

{{
  "plan": ["step 1", "step 2", "..."],
  "read_files": ["relative/path/to/file", "..."],
  "edits": [{{"path": "relative/path/to/file", "action": "create|modify"}}],
  "notes": "short notes for the human reviewer",
  "context_summary": "brief summary of the repo relationships, wiring, imports, tests, and conventions that matter",
  "run": ["optional safe commands to run like 'pytest -q' or 'python -m unittest'"]
}}

Rules:
- Do not delete files.
- Do not run network commands (curl/wget/apt).
- Only propose safe local commands in "run".
- Keep file list small and targeted.
- Output ONLY valid JSON.
- Do not include markdown fences.
- Do not include commentary before or after the JSON object.
- The top-level response must be exactly one JSON object matching the requested schema.
- You MUST NOT modify anything in these paths: agent/, scripts/, systemd/, agent_runner/, .github/
- read_files MUST be limited to the minimum required to do the change correctly (max {MAX_FILES_TO_READ})
- Treat "Current repo context" from Cognee as current truth when it is present.
- Treat "Historical task sidecar" as advisory only.
- Choose read_files to include the smallest correct cross-section of:
  1) target implementation files
  2) wiring/import/caller files
  3) nearby tests
  4) related schemas/models/services/jobs/routes as needed
- If the issue implies integration work, include at least one wiring/caller file when possible.
- Fill context_summary with the key relationships you inferred from Cognee and the repo tree. This summary will be given to the writer.
"""


def plan_stage2_prompt(
    issue_title: str,
    issue_body: str,
    repo_tree: str,
    file_context: str,
    stage1: dict,
    cognee_context: str = "",
) -> str:
    return f"""You are a coding agent working in a git repository.
You already selected files to read. Their contents are included below.
Now produce the final edits.

Repository tree (partial):
{repo_tree}

GitHub issue:
Title: {issue_title}

Body:
{issue_body}

Cognee context bundle:
{cognee_context if cognee_context else "(none)"}

Files (path + content; content may be truncated):
{file_context}

Stage-1 plan (for reference):
{json.dumps(stage1, indent=2)}

Return a JSON object with this schema:

{{
  "plan": ["step 1", "step 2", "..."],
  "edits": [
    {{
      "path": "relative/path/to/file",
      "action": "create|modify",
      "content_b64": "OPTIONAL: base64-encoded UTF-8 file content",
      "content": "OPTIONAL: raw UTF-8 file content (use this if base64 is difficult)"
    }}
  ],
  "notes": "short notes for the human reviewer",
  "run": ["optional safe commands ..."]
}}

Rules:
- Prefer "content_b64" when possible, but if you cannot produce valid base64, use "content" instead.
- Output ONLY valid JSON.
- Do not include markdown fences.
- Do not include commentary before or after the JSON object.
- The top-level response must be exactly one JSON object matching the requested schema.
- Do not run network commands (curl/wget/apt).
- Only propose safe local commands in "run".
- Keep file list small and targeted.
- You MUST NOT modify anything in these paths: agent/, scripts/, systemd/, agent_runner/, .github/
- When action is "modify", preserve unrelated parts of the file. Make the smallest change required.
- Use the stage-1 context_summary plus the full Cognee context to preserve imports, wiring, callers/usages, and adjacent tests.
- If you introduce or rename a symbol, update related imports/usages in the selected files rather than leaving disconnected code.
- If you cannot safely wire the change with the provided files, prefer updating a caller/wiring/test file in the same edit set.
"""


def _planner_provider_and_model() -> tuple[str, str]:
    provider = os.environ.get("MODEL_PROVIDER", MODEL_PROVIDER).lower()
    if provider == "bedrock":
        return provider, os.environ.get("BEDROCK_PLANNER_MODEL", BEDROCK_PLANNER_MODEL)
    return provider, os.environ.get("PLANNER_MODEL", PLANNER_MODEL)


def _writer_provider_and_model() -> tuple[str, str]:
    provider = os.environ.get("MODEL_PROVIDER", MODEL_PROVIDER).lower()
    if provider == "bedrock":
        return provider, os.environ.get("BEDROCK_WRITER_MODEL", BEDROCK_WRITER_MODEL)
    return provider, os.environ.get("WRITER_MODEL", WRITER_MODEL)


def run_stage1(
    issue_title: str,
    issue_body: str,
    repo_tree: str,
    hinted_files: list[str],
    cognee_context: str = "",
) -> dict:
    prompt = plan_stage1_prompt(issue_title, issue_body, repo_tree, hinted_files, cognee_context)
    provider, model = _planner_provider_and_model()

    out = generate_text(
        prompt,
        provider=provider,
        model=model,
        think=os.environ.get("PLANNER_THINK", "1") == "1",
        num_ctx=int(os.environ.get("PLANNER_NUM_CTX", "8192")),
        max_tokens=PLANNER_MAX_TOKENS,
    )
    return parse_json_loose(out)


def run_stage2(
    issue_title: str,
    issue_body: str,
    repo_tree: str,
    file_context: str,
    stage1: dict,
    cognee_context: str = "",
) -> dict:
    prompt = plan_stage2_prompt(issue_title, issue_body, repo_tree, file_context, stage1, cognee_context)
    provider, model = _writer_provider_and_model()

    out = generate_text(
        prompt,
        provider=provider,
        model=model,
        think=os.environ.get("WRITER_THINK", "1") == "1",
        num_ctx=int(os.environ.get("WRITER_NUM_CTX", "8192")),
        max_tokens=WRITER_MAX_TOKENS,
    )
    return parse_json_loose(out)


def norm_title(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", "", s)
    return s


def too_similar(a: str, b: str, threshold: float = 0.90) -> bool:
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold


def backlog_prompt(repo_tree: str, open_titles: list[str]) -> str:
    return f"""You are a project manager agent.

Repository tree:
{repo_tree}

Existing open issue titles (avoid duplicates):
{chr(10).join("- " + t for t in open_titles[:80])}

Rules:
- Do NOT propose changes to agent_runner/, agent/, scripts/, systemd/, .github/
- Propose small, focused tasks only.
- Return ONLY valid JSON.

Return JSON:
{{
  "proposals": [
    {{
      "title": "Short issue title",
      "body": "Clear description and acceptance criteria"
    }}
  ]
}}
"""


def generate_backlog_from_context(repo_tree: str, open_titles: list[str], max_proposals: int) -> list[dict]:
    prompt = backlog_prompt(repo_tree, open_titles)
    provider, model = _planner_provider_and_model()
    model_out = generate_text(
        prompt,
        provider=provider,
        model=model,
        think=os.environ.get("PLANNER_THINK", "1") == "1",
        num_ctx=int(os.environ.get("PLANNER_NUM_CTX", "8192")),
    )
    data = parse_json_loose(model_out)

    proposals = data.get("proposals", []) or []
    clean: list[dict] = []

    seen_norm = [norm_title(t) for t in open_titles if t.strip()]
    seen_set = set(seen_norm)
    batch_seen: list[str] = []

    for p in proposals[:max_proposals]:
        title = (p.get("title") or "").strip()
        body = (p.get("body") or "").strip()
        if not (title and body):
            continue

        nt = norm_title(title)

        if nt in seen_set:
            continue
        if any(too_similar(nt, x) for x in seen_norm):
            continue
        if nt in (norm_title(x) for x in batch_seen):
            continue
        if any(too_similar(nt, norm_title(x)) for x in batch_seen):
            continue

        clean.append({"title": title, "body": body})
        batch_seen.append(title)
        seen_set.add(nt)
        seen_norm.append(nt)

    return clean