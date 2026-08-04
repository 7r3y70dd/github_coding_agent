from __future__ import annotations
import base64
import json
import os
import re
import shutil
import subprocess
import time
from decimal import Decimal
from typing import Any, Optional

import boto3
from boto3.dynamodb.conditions import Key

from planner import run_stage1, run_stage2
from cognee_adapter import (
    add_task_memory,
    cognee_status,
    search_issue_context,
    seed_repo_memory,
    shutdown_cognee_runner,
)
from repo_indexing import index_pk, index_sk
from platform_lock import global_task_lock


AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("BEDROCK_REGION", "us-east-1"))
AWS_ENDPOINT_URL = os.environ.get("AWS_ENDPOINT_URL", "").strip() or None
AGENT_QUEUE_URL = os.environ.get("AGENT_QUEUE_URL", "").strip()
AGENT_QUEUE_NAME = os.environ.get("AGENT_QUEUE_NAME", "").strip()
RUNS_TABLE = os.environ.get("RUNS_TABLE", "agent_runs")

SKIP_COGNEE_PRESEED = os.environ.get("SKIP_COGNEE_PRESEED", "1") == "1"
ALLOW_AGENT_COGNEE_SEEDING = os.environ.get("ALLOW_AGENT_COGNEE_SEEDING", "0") == "1"
USE_LATEST_COGNEE_INDEX = os.environ.get("USE_LATEST_COGNEE_INDEX", "1") == "1"
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "5"))
COMMAND_TIMEOUT = int(os.environ.get("COMMAND_TIMEOUT", "900"))
VALIDATION_CREATE_VENV = os.environ.get("VALIDATION_CREATE_VENV", "1") == "1"
VALIDATION_INSTALL_PYTEST = os.environ.get("VALIDATION_INSTALL_PYTEST", "1") == "1"
VALIDATION_INSTALL_REQUIREMENTS = os.environ.get("VALIDATION_INSTALL_REQUIREMENTS", "0") == "1"
VALIDATION_VENV_DIR = os.environ.get("VALIDATION_VENV_DIR", ".venv")
VALIDATION_PYTHON = os.environ.get("VALIDATION_PYTHON", "/usr/bin/python3.11")
VALIDATION_DEFAULT_COMMAND = os.environ.get(
    "VALIDATION_DEFAULT_COMMAND",
    "pytest -q -ra --continue-on-collection-errors",
)

VALIDATION_SCRIPT_CANDIDATES = (
    "scripts/cognimoss-validate.sh",
    ".cognimoss/validate.sh",
)

ESCALATION_ON_VALIDATION_FAILURE = os.environ.get("ESCALATION_ON_VALIDATION_FAILURE", "1") == "1"
ESCALATION_MAX_FAILURE_CHARS = int(os.environ.get("ESCALATION_MAX_FAILURE_CHARS", "12000"))
ESCALATION_MAX_DIFF_CHARS = int(os.environ.get("ESCALATION_MAX_DIFF_CHARS", "20000"))

VALIDATION_PYTHON = os.environ.get("VALIDATION_PYTHON", "/usr/bin/python3.11")
VALIDATION_DEFAULT_COMMAND = os.environ.get(
    "VALIDATION_DEFAULT_COMMAND",
    "pytest -q -ra --continue-on-collection-errors",
)

ESCALATION_ON_VALIDATION_FAILURE = os.environ.get("ESCALATION_ON_VALIDATION_FAILURE", "1") == "1"
ESCALATION_MAX_FAILURE_CHARS = int(os.environ.get("ESCALATION_MAX_FAILURE_CHARS", "12000"))
ESCALATION_MAX_DIFF_CHARS = int(os.environ.get("ESCALATION_MAX_DIFF_CHARS", "20000"))

# Keep this disabled for customer repos.
NO_PR = os.environ.get("NO_PR", "0") == "1"

WORKDIR = os.environ.get("WORKDIR", "/var/lib/agent-runner/work")
SAFE_BRANCH_PREFIX = os.environ.get("BRANCH_PREFIX", "agent/")

DENY_PREFIXES = (
    "agent_runner/",
    "agent/",
    #"scripts/",
    "systemd/",
    ".github/",
)

DENY_FILENAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    "secrets.txt",
    "credentials",
    "id_rsa",
    "id_ed25519",
}

MAX_FILES_TO_READ = int(os.environ.get("MAX_FILES_TO_READ", "8"))
MAX_FILE_CHARS = int(os.environ.get("MAX_FILE_CHARS", "12000"))

PROJECT_ALLOWLIST = tuple(
    p.strip().strip("/") + "/"
    for p in os.environ.get("PROJECT_ALLOWLIST", "project/,src/,apps/").split(",")
    if p.strip()
)

EVENT_LOG = os.environ.get("EVENT_LOG", os.path.join(WORKDIR, "agent-events.jsonl"))

_aws_kwargs = {"region_name": AWS_REGION}
if AWS_ENDPOINT_URL:
    _aws_kwargs["endpoint_url"] = AWS_ENDPOINT_URL

sqs = boto3.client("sqs", **_aws_kwargs)
dynamodb = boto3.resource("dynamodb", **_aws_kwargs)
runs_table = dynamodb.Table(RUNS_TABLE)

if not AGENT_QUEUE_URL and AGENT_QUEUE_NAME:
    AGENT_QUEUE_URL = sqs.get_queue_url(QueueName=AGENT_QUEUE_NAME)["QueueUrl"]
if not AGENT_QUEUE_URL:
    raise RuntimeError("Set AGENT_QUEUE_URL, or set AGENT_QUEUE_NAME so the queue URL can be resolved.")


def now_ms() -> int:
    return int(time.time() * 1000)

VALIDATION_SAFE_ENV_ALLOWLIST = {
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TERM",
    "TMPDIR",
    "ENVIRONMENT",
    "TESTING",
    "DATABASE_URL",
    "REDIS_URL",
    "PYTHONPATH",
}


VALIDATION_BLOCKED_ENV_PREFIXES = (
    "AWS_",
    "BEDROCK_",
    "GITHUB_",
    "GH_",
    "OPENAI_",
    "ANTHROPIC_",
    "COGNEE_",
    "LLM_",
    "EMBEDDING_",
)


def validation_env(repo_path: str) -> dict[str, str]:
    env: dict[str, str] = {}

    for key in VALIDATION_SAFE_ENV_ALLOWLIST:
        if key in os.environ:
            env[key] = os.environ[key]

    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.setdefault("HOME", os.path.join(repo_path, ".cognimoss-home"))
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("ENVIRONMENT", "test")
    env.setdefault("TESTING", "true")

    # Explicitly disable SDK fallback to EC2 instance metadata.
    env["AWS_EC2_METADATA_DISABLED"] = "true"

    for key in list(env):
        if any(key.startswith(prefix) for prefix in VALIDATION_BLOCKED_ENV_PREFIXES):
            env.pop(key, None)

    os.makedirs(env["HOME"], exist_ok=True)

    return env

def emit_event(event: dict) -> None:
    try:
        os.makedirs(os.path.dirname(EVENT_LOG), exist_ok=True)
        event = dict(event)
        event["ts"] = time.time()
        with open(EVENT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass

def find_repo_validation_script(repo_path: str) -> str:
    for rel in VALIDATION_SCRIPT_CANDIDATES:
        normalized = os.path.normpath(rel).replace("\\", "/")

        if normalized not in VALIDATION_SCRIPT_CANDIDATES:
            continue

        full = os.path.join(repo_path, normalized)

        if os.path.isfile(full):
            return normalized

    return ""

def update_run_status(run_id: str, status: str, message: str = "", pr_url: str = "") -> None:
    update_expr = "SET #status = :status, updated_at = :updated_at, status_message = :message"
    expr_names = {"#status": "status"}
    expr_values: dict[str, Any] = {
        ":status": status,
        ":updated_at": Decimal(now_ms()),
        ":message": message,
    }

    if pr_url:
        update_expr += ", pr_url = :pr_url"
        expr_values[":pr_url"] = pr_url

    runs_table.update_item(
        Key={
            "pk": f"RUN#{run_id}",
            "sk": "META",
        },
        UpdateExpression=update_expr,
        ExpressionAttributeNames=expr_names,
        ExpressionAttributeValues=expr_values,
    )


def put_run_event(
    run_id: str,
    event_type: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> None:
    ts = now_ms()

    runs_table.put_item(
        Item={
            "pk": f"RUN#{run_id}",
            "sk": f"EVENT#{ts}",
            "run_id": run_id,
            "ts": Decimal(ts),
            "event_type": event_type,
            "message": message,
            "payload": payload or {},
        }
    )

def log_workflow_step(
    run_id: str,
    issue_number: int,
    event_type: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> None:
    """Record a major workflow checkpoint in stdout, JSONL, and DynamoDB.

    This is intentionally best-effort: progress logging should never stop the
    coding agent from completing the GitHub issue.
    """
    payload = payload or {}

    print(
        f"[WORKFLOW] run={run_id} issue={issue_number} step={event_type}: {message}",
        flush=True,
    )

    emit_event(
        {
            "type": event_type,
            "run_id": run_id,
            "issue": issue_number,
            "message": message,
            **payload,
        }
    )

    try:
        put_run_event(run_id, event_type, message, payload)
    except Exception:
        pass


def cognee_required() -> bool:
    try:
        return bool(cognee_status().get("required"))
    except Exception:
        return False


def get_ready_cognee_index(repo: str, scope_hash: str = "") -> dict[str, Any] | None:
    """Return a ready Cognee index record for this repo/scope.

    Agent workers do not create or update repository indexes. They only consume
    records written by the separate Cognee indexer service.
    """
    repo = validate_repo(repo)
    scope_hash = (scope_hash or "").strip()

    try:
        if scope_hash:
            resp = runs_table.get_item(Key={"pk": index_pk(repo), "sk": index_sk(scope_hash)})
            item = resp.get("Item") or {}
            if item.get("status") == "ready" and item.get("dataset_name"):
                return item
            return None

        if not USE_LATEST_COGNEE_INDEX:
            return None

        resp = runs_table.query(KeyConditionExpression=Key("pk").eq(index_pk(repo)))
        candidates = [i for i in resp.get("Items", []) if i.get("status") == "ready" and i.get("dataset_name")]
        if not candidates:
            return None

        candidates.sort(key=lambda i: int(i.get("last_indexed_at") or i.get("updated_at") or 0), reverse=True)
        return candidates[0]
    except Exception as e:
        emit_event({"type": "cognee_index_lookup_warning", "repo": repo, "scope_hash": scope_hash, "error": str(e)})
        return None


def search_issue_context_scoped(
    title: str,
    body: str,
    hinted_files: list[str],
    repo: str,
    index_record: dict[str, Any],
) -> str:
    """Search only the ready Cognee dataset selected for this run.

    Newer cognee_adapter.py versions should accept dataset_name/scope_hash. The
    TypeError fallback keeps older adapters from crashing while you roll out the
    separate indexer.
    """
    dataset_name = str(index_record.get("dataset_name") or "")
    code_dataset_name = str(index_record.get("code_dataset_name") or dataset_name or "")
    rules_dataset_name = str(index_record.get("rules_dataset_name") or "")
    scope_hash = str(index_record.get("scope_hash") or "")
    snapshot_key = str(index_record.get("snapshot_key") or "")

    try:
        return search_issue_context(
            title,
            body,
            hinted_files,
            repo=repo,
            dataset_name=dataset_name,
            code_dataset_name=code_dataset_name,
            rules_dataset_name=rules_dataset_name,
            scope_hash=scope_hash,
            snapshot_key=snapshot_key,
        )
    except TypeError:
        emit_event(
            {
                "type": "cognee_adapter_legacy_search",
                "repo": repo,
                "scope_hash": scope_hash,
                "dataset_name": dataset_name,
                "message": "search_issue_context does not accept dataset_name/scope_hash yet.",
            }
        )
        return search_issue_context(title, body, hinted_files, repo=repo)


def validate_repo(repo: str) -> str:
    repo = (repo or "").strip()

    if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo):
        raise ValueError(f"Invalid repo format: {repo}. Expected owner/repo.")

    return repo


def is_safe_relpath(rel: str) -> bool:
    rel = (rel or "").lstrip("./")

    if not rel or rel.startswith("/") or ".." in rel.split("/"):
        return False

    if any(rel.startswith(p) for p in DENY_PREFIXES):
        return False

    name = os.path.basename(rel)
    if name in DENY_FILENAMES:
        return False

    if name.endswith((".pem", ".key", ".p12", ".pfx")):
        return False

    return True


def extract_filepaths_from_issue(text: str) -> list[str]:
    text = text or ""
    found: list[str] = []

    m = re.search(r"(?is)^\s*##\s*target files\s*(.+?)(?:\n##\s|\Z)", text)
    if m:
        block = m.group(1)
        for line in block.splitlines():
            line = line.strip()
            if not line:
                continue
            line = re.sub(r"^[\-\*\d\.\)\s]+", "", line).strip()
            line = line.strip("`").strip()
            if is_safe_relpath(line):
                found.append(line)

    text_no_urls = re.sub(r"https?://\S+", " ", text)
    candidates = re.findall(r"(?<!@)([A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-\.]+)+)", text_no_urls)

    for c in candidates:
        c = c.strip().strip("`").strip()
        if is_safe_relpath(c) and c not in found:
            found.append(c)

    return found[:MAX_FILES_TO_READ]


def read_text_file(repo_path: str, rel: str, max_chars: int = MAX_FILE_CHARS) -> str:
    rel = rel.lstrip("./")

    if not is_safe_relpath(rel):
        return ""

    full = os.path.join(repo_path, rel)

    if not os.path.exists(full) or not os.path.isfile(full):
        return ""

    try:
        with open(full, "rb") as f:
            raw = f.read(max_chars + 1)

        if b"\x00" in raw:
            return ""

        text = raw.decode("utf-8", errors="replace")
        return text[:max_chars]
    except Exception:
        return ""


def run(cmd: list[str], cwd: Optional[str] = None) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=COMMAND_TIMEOUT)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Command timed out after {COMMAND_TIMEOUT}s: {' '.join(cmd)}") from e

    out = (res.stdout or "").strip()
    err = (res.stderr or "").strip()

    if res.returncode != 0:
        raise RuntimeError(
            f"Command failed ({res.returncode}): {' '.join(cmd)}\n"
            f"STDERR:\n{err}\n"
            f"STDOUT:\n{out}"
        )

    return out

def prepare_validation_env(repo_path: str) -> None:
    if not VALIDATION_CREATE_VENV:
        return

    py = os.path.join(repo_path, VALIDATION_VENV_DIR, "bin", "python")

    if not os.path.exists(py):
        run([VALIDATION_PYTHON, "-m", "venv", VALIDATION_VENV_DIR], cwd=repo_path)

    if VALIDATION_INSTALL_PYTEST:
        run([py, "-m", "pip", "install", "-U", "pip"], cwd=repo_path)
        run([py, "-m", "pip", "install", "pytest"], cwd=repo_path)

    if VALIDATION_INSTALL_REQUIREMENTS and os.path.exists(os.path.join(repo_path, "requirements.txt")):
        run([py, "-m", "pip", "install", "-r", "requirements.txt"], cwd=repo_path)


def rewrite_validation_command(command: str) -> str:
    command = str(command or "").strip()

    if not command:
        return command

    prefix = f". {VALIDATION_VENV_DIR}/bin/activate && "

    if command == "pytest":
        return prefix + "python -m pytest"

    if command.startswith("pytest "):
        return prefix + "python -m " + command

    if command.startswith("python -m pytest"):
        return prefix + command

    if " pytest " in f" {command} ":
        return prefix + command.replace(" pytest ", " python -m pytest ")

    return command


def dedupe_commands(commands: list[str]) -> list[str]:
    out: list[str] = []
    for c in commands:
        c = str(c or "").strip()
        if c and c not in out:
            out.append(c)
    return out


def validation_command_list(repo_path: str, commands: list[str]) -> list[str]:
    script = find_repo_validation_script(repo_path)

    if script:
        return [f"bash {script}"]

    cmds = list(commands or [])

    if VALIDATION_DEFAULT_COMMAND:
        cmds = [VALIDATION_DEFAULT_COMMAND] + cmds

    return dedupe_commands(safe_commands_only(cmds))


def run_validation_command(cmd: list[str], cwd: str) -> str:
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=COMMAND_TIMEOUT,
            env=validation_env(cwd),
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Command timed out after {COMMAND_TIMEOUT}s: {' '.join(cmd)}") from e

    out = (res.stdout or "").strip()
    err = (res.stderr or "").strip()

    if res.returncode != 0:
        raise RuntimeError(
            f"Command failed ({res.returncode}): {' '.join(cmd)}\n"
            f"STDERR:\n{err}\n"
            f"STDOUT:\n{out}"
        )

    return out


def run_validation_suite(repo_path: str, commands: list[str]) -> tuple[list[str], bool]:
    script = find_repo_validation_script(repo_path)
    cmds = validation_command_list(repo_path, commands)
    results: list[str] = []
    failed = False

    # If the repo has its own Cognimoss validation script, do NOT force Python setup.
    # The script owns its own setup: Docker, DB, Unreal build, npm, cargo, etc.
    if cmds and not script:
        try:
            prepare_validation_env(repo_path)
        except Exception as e:
            return [f"$ prepare validation environment\nERROR: {e}\n"], True

    for c in cmds:
        effective_command = rewrite_validation_command(c)

        try:
            # Use run_validation_command here if you added the sanitized-env helper.
            # This prevents validation scripts from inheriting worker secrets.
            out = run_validation_command(["bash", "-lc", effective_command], cwd=repo_path)
            results.append(f"$ {effective_command}\n{out}\n")
        except Exception as e:
            failed = True
            results.append(f"$ {effective_command}\nERROR: {e}\n")

    return results, failed


def git_changed_files(repo_path: str) -> list[str]:
    try:
        out = run(["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"], cwd=repo_path)
        return [p.strip() for p in out.splitlines() if p.strip() and is_safe_relpath(p.strip())]
    except Exception:
        return []


def git_head_diff(repo_path: str, max_chars: int = ESCALATION_MAX_DIFF_CHARS) -> str:
    try:
        out = run(["git", "show", "--stat", "--patch", "--find-renames", "HEAD"], cwd=repo_path)
        return out[:max_chars]
    except Exception as e:
        return f"(Could not read git diff: {e})"


def build_file_context(repo_path: str, paths: list[str]) -> str:
    chunks: list[str] = []
    for p in paths[:MAX_FILES_TO_READ]:
        content = read_text_file(repo_path, p, max_chars=MAX_FILE_CHARS)
        if content:
            chunks.append(f"--- FILE: {p} ---\n{content}\n")
    return "\n".join(chunks) if chunks else "(No file contents were loaded.)"


def run_with_escalation_model(
    title: str,
    body: str,
    repo_tree: str,
    file_context: str,
    stage1: dict,
    cognee_context: str,
) -> dict:
    escalation_model = os.environ.get("BEDROCK_ESCALATION_MODEL", "").strip()

    if not escalation_model:
        return run_stage2(title, body, repo_tree, file_context, stage1, cognee_context)

    old_writer_model = os.environ.get("BEDROCK_WRITER_MODEL")

    try:
        os.environ["BEDROCK_WRITER_MODEL"] = escalation_model
        return run_stage2(title, body, repo_tree, file_context, stage1, cognee_context)
    finally:
        if old_writer_model is None:
            os.environ.pop("BEDROCK_WRITER_MODEL", None)
        else:
            os.environ["BEDROCK_WRITER_MODEL"] = old_writer_model


def run_escalation_repair(
    *,
    repo: str,
    issue_number: int,
    run_id: str,
    title: str,
    body: str,
    repo_tree: str,
    task_dir: str,
    stage1: dict,
    initial_plan: dict,
    merged_files: list[str],
    ready_index: dict[str, Any] | None,
    cognee_context: str,
    validation_results: list[str],
) -> tuple[dict, list[str]]:
    failure_text = "".join(validation_results)[-ESCALATION_MAX_FAILURE_CHARS:]
    changed_files = git_changed_files(task_dir)
    changed_diff = git_head_diff(task_dir)

    escalation_body = f"""Original GitHub issue:

{body}

The initial generated code failed validation.

Validation failure text:
{failure_text}

Changed files:
{chr(10).join(changed_files) if changed_files else "(unknown)"}

Current git diff from the local commit:
{changed_diff}

Please make a minimal repair that preserves the original issue intent.
Only modify files needed to fix the validation failure.
"""

    escalation_context = cognee_context

    if ready_index:
        try:
            escalation_context = search_issue_context_scoped(
                f"Fix validation failure: {title}",
                escalation_body,
                changed_files or merged_files,
                repo=repo,
                index_record=ready_index,
            )
        except Exception as e:
            _record_cognee_warning(run_id, issue_number, "search_escalation_context", e)

    repair_paths: list[str] = []
    for p in changed_files + merged_files:
        if p and p not in repair_paths and is_safe_relpath(p):
            repair_paths.append(p)

    repair_file_context = build_file_context(task_dir, repair_paths)

    repair_stage = dict(stage1)
    repair_stage["initial_writer_plan"] = initial_plan
    repair_stage["validation_failure"] = failure_text
    repair_stage["changed_files"] = changed_files
    repair_stage["changed_diff"] = changed_diff

    repair_plan = run_with_escalation_model(
        title,
        escalation_body,
        repo_tree,
        repair_file_context,
        repair_stage,
        escalation_context,
    )

    repair_edits = repair_plan.get("edits", []) or []

    if repair_edits:
        apply_edits(task_dir, repair_edits)

    return repair_plan, changed_files


def gh_api_json(path: str) -> Any:
    out = run(["gh", "api", "-H", "Accept: application/vnd.github+json", path])
    return json.loads(out) if out else None


def gh_get_issue(repo: str, issue_number: int) -> dict[str, Any]:
    issue = gh_api_json(f"repos/{repo}/issues/{issue_number}")

    if not issue:
        raise RuntimeError(f"GitHub issue not found: {repo}#{issue_number}")

    if "pull_request" in issue:
        raise RuntimeError(f"GitHub issue {repo}#{issue_number} is a pull request, not an issue.")

    return issue


def gh_post_comment(repo: str, issue_number: int, body: str) -> None:
    run(["gh", "api", f"repos/{repo}/issues/{issue_number}/comments", "-f", f"body={body}"])


def pr_exists_for_branch(repo: str, branch: str) -> bool:
    out = run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--head",
            branch,
            "--state",
            "all",
            "--json",
            "number",
        ],
        cwd=None,
    )

    data = json.loads(out) if out else []
    return len(data) > 0


def sanitize_branch_name(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9\-]+", "-", s).strip("-")

    if not s:
        s = "task"

    return s[:40]


def sanitize_repo_for_path(repo: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", repo)


def read_repo_tree(repo_path: str, max_files: int = 200) -> str:
    return run(
        ["bash", "-lc", f"find . -maxdepth 3 -type f | sed 's|^./||' | head -n {max_files}"],
        cwd=repo_path,
    )


def read_repo_tree_allowlisted(repo_path: str, max_files: int = 200) -> str:
    parts = []

    for d in PROJECT_ALLOWLIST:
        parts.append(f'if [ -d "{d}" ]; then find "{d}" -maxdepth 3 -type f; fi')

    cmd = " ; ".join(parts) + f" | sed 's|^./||' | head -n {max_files}"
    return run(["bash", "-lc", cmd], cwd=repo_path)


def safe_commands_only(cmds: list[str]) -> list[str]:
    banned = (
        "rm ",
        "sudo",
        "apt",
        "curl",
        "wget",
        "dd ",
        "mkfs",
        "mount",
        "umount",
        ":(){",
        "shutdown",
        "reboot",
        "env",
        "printenv",
        "gh auth",
        "aws ",
        "cat ~/.",
        "cat /",
        "grep ",
        "find /",
        "python -c",
        "node -e",
    )

    safe = []

    for c in cmds:
        c_strip = str(c or "").strip()
        c_low = c_strip.lower()

        if not c_strip:
            continue
        if any(b in c_low for b in banned):
            continue

        safe.append(c_strip)

    return safe


def apply_edits(repo_path: str, edits: list[dict]) -> None:
    for e in edits:
        rel = e["path"].lstrip("./")

        if not is_safe_relpath(rel):
            raise ValueError(f"Refusing unsafe/protected path: {rel}")

        full = os.path.join(repo_path, rel)
        os.makedirs(os.path.dirname(full) or repo_path, exist_ok=True)

        if "content_b64" in e:
            try:
                content = base64.b64decode(e["content_b64"]).decode("utf-8", errors="replace")
            except Exception as ex:
                raise ValueError(f"Invalid base64 content for {rel}: {ex}") from ex
        else:
            content = e.get("content", "")

        with open(full, "w", encoding="utf-8") as f:
            f.write(content)


def git_commit_changes(repo_path: str, message: str) -> bool:
    status = run(["git", "status", "--porcelain"], cwd=repo_path)

    if not status.strip():
        return False

    run(["git", "add", "-A"], cwd=repo_path)

    try:
        run(["git", "commit", "-m", message], cwd=repo_path)
        return True
    except RuntimeError as e:
        if "nothing to commit" in str(e).lower():
            return False
        raise


def git_push_branch(repo_path: str, branch: str) -> None:
    run(["git", "push", "-u", "origin", branch], cwd=repo_path)


def git_commit_push(repo_path: str, branch: str, message: str) -> None:
    # Backward-compatible wrapper for older call sites. New workflow commits,
    # validates/repairs, then pushes only after the final validation decision.
    committed = git_commit_changes(repo_path, message)
    if committed:
        git_push_branch(repo_path, branch)


def open_pr(repo: str, branch: str, title: str, body: str) -> str:
    return run(
        [
            "gh",
            "pr",
            "create",
            "--repo",
            repo,
            "--head",
            branch,
            "--base",
            os.environ.get("BASE_BRANCH", "main"),
            "--title",
            title,
            "--body",
            body,
        ]
    ).strip()


def _record_cognee_warning(run_id: str, issue_number: int, stage: str, error: Exception) -> None:
    message = f"Cognee {stage} failed: {error}"
    emit_event(
        {
            "type": "cognee_warning",
            "stage": stage,
            "run_id": run_id,
            "issue": issue_number,
            "error": str(error),
        }
    )
    try:
        put_run_event(
            run_id,
            "cognee_warning",
            message,
            {"stage": stage, "error": str(error)},
        )
    except Exception:
        pass


def process_github_issue(
    run_id: str,
    repo: str,
    issue_number: int,
    fallback_title: str = "",
    cognee_scope_hash: str = "",
    cognee_dataset_name: str = "",
    cognee_code_dataset_name: str = "",
    cognee_rules_dataset_name: str = "",
    cognee_snapshot_key: str = "",
) -> str:
    repo = validate_repo(repo)

    log_workflow_step(
        run_id,
        issue_number,
        "issue_start",
        "Starting GitHub issue processing.",
        {
            "repo": repo,
            "fallback_title": fallback_title,
            "cognee": cognee_status(),
            "cognee_scope_hash": cognee_scope_hash,
            "cognee_dataset_name": cognee_dataset_name,
            "cognee_code_dataset_name": cognee_code_dataset_name,
            "cognee_rules_dataset_name": cognee_rules_dataset_name,
            "cognee_snapshot_key": cognee_snapshot_key,
        },
    )

    update_run_status(run_id, "running", "Worker started processing the queued issue.")
    put_run_event(
        run_id,
        "worker_started",
        "Worker started processing the queued issue.",
        {"repo": repo, "issue_number": issue_number, "cognee": cognee_status()},
    )

    log_workflow_step(
        run_id,
        issue_number,
        "github_issue_fetch_start",
        "Fetching GitHub issue details.",
        {"repo": repo},
    )

    issue = gh_get_issue(repo, issue_number)

    log_workflow_step(
        run_id,
        issue_number,
        "github_issue_fetch_done",
        "GitHub issue details were loaded.",
        {
            "repo": repo,
            "title": issue.get("title") or fallback_title or f"Issue {issue_number}",
        },
    )

    title = issue.get("title") or fallback_title or f"Issue {issue_number}"
    body = issue.get("body") or ""

    log_workflow_step(
        run_id,
        issue_number,
        "github_comment_start",
        "Posting initial progress comment to the GitHub issue.",
        {"repo": repo},
    )

    gh_post_comment(repo, issue_number, "Agent is on the job. Preparing workspace...")

    repo_path_fragment = sanitize_repo_for_path(repo)
    task_dir = os.path.join(WORKDIR, repo_path_fragment, f"issue-{issue_number}-{run_id[:8]}")

    log_workflow_step(
        run_id,
        issue_number,
        "workspace_prepare_start",
        "Preparing a clean local workspace.",
        {
            "workdir": WORKDIR,
            "task_dir": task_dir,
        },
    )

    if os.path.exists(task_dir):
        shutil.rmtree(task_dir)

    os.makedirs(task_dir, exist_ok=True)

    log_workflow_step(
        run_id,
        issue_number,
        "workspace_prepare_done",
        "Local workspace is ready.",
        {"task_dir": task_dir},
    )

    log_workflow_step(
        run_id,
        issue_number,
        "repo_clone_start",
        "Cloning the target repository.",
        {
            "repo": repo,
            "task_dir": task_dir,
        },
    )

    run(["gh", "repo", "clone", repo, task_dir])

    log_workflow_step(
        run_id,
        issue_number,
        "repo_clone_done",
        "Repository clone completed.",
        {
            "repo": repo,
            "task_dir": task_dir,
        },
    )

    branch = SAFE_BRANCH_PREFIX + sanitize_branch_name(title) + f"-{issue_number}-{run_id[:8]}"

    log_workflow_step(
        run_id,
        issue_number,
        "branch_prepare_start",
        "Preparing the working branch.",
        {
            "branch": branch,
            "no_pr": NO_PR,
            "base_branch": os.environ.get("BASE_BRANCH", "main"),
        },
    )

    if not NO_PR:
        if pr_exists_for_branch(repo, branch):
            gh_post_comment(repo, issue_number, f"A PR already exists for branch: `{branch}`. Skipping.")
            update_run_status(run_id, "skipped", f"PR already exists for branch {branch}.")
            return ""

        run(["git", "checkout", "-b", branch], cwd=task_dir)
    else:
        run(["git", "checkout", os.environ.get("BASE_BRANCH", "main")], cwd=task_dir)
        run(["git", "pull", "--ff-only", "origin", os.environ.get("BASE_BRANCH", "main")], cwd=task_dir)

        log_workflow_step(
            run_id,
            issue_number,
            "branch_prepare_done",
            "Working branch is ready.",
            {
                "branch": branch if not NO_PR else os.environ.get("BASE_BRANCH", "main"),
                "no_pr": NO_PR,
            },
        )

    snapshot_key = ""

    if not ALLOW_AGENT_COGNEE_SEEDING:
        log_workflow_step(
            run_id,
            issue_number,
            "cognee_preseed_skipped",
            "Agent worker no longer performs repository Cognee ingestion; seeding is handled by the Cognee indexer service.",
            {
                "mode": "issue_pickup",
                "repo": repo,
                "reason": "ALLOW_AGENT_COGNEE_SEEDING=0",
            },
        )
    elif SKIP_COGNEE_PRESEED:
        log_workflow_step(
            run_id,
            issue_number,
            "cognee_preseed_skipped",
            "Skipping pre-planning full-repo Cognee ingestion to reduce cost.",
            {
                "mode": "issue_pickup",
                "repo": repo,
                "reason": "SKIP_COGNEE_PRESEED=1",
            },
        )
    else:
        log_workflow_step(
            run_id,
            issue_number,
            "cognee_preseed_start",
            "Indexing the current repository with Cognee before planning.",
            {
                "mode": "issue_pickup",
                "repo": repo,
            },
        )

        try:
            snapshot_key = seed_repo_memory(task_dir, repo=repo)
        except Exception as e:
            _record_cognee_warning(run_id, issue_number, "seed_before_planning", e)
            if cognee_required():
                raise

        log_workflow_step(
            run_id,
            issue_number,
            "cognee_preseed_done",
            "Pre-planning Cognee repository index is ready."
            if snapshot_key
            else "Pre-planning Cognee repository indexing was skipped or unavailable.",
            {
                "mode": "issue_pickup",
                "repo": repo,
                "snapshot_key": snapshot_key,
            },
        )

    log_workflow_step(
        run_id,
        issue_number,
        "repo_tree_start",
        "Reading a partial repository tree for planner context.",
        {"task_dir": task_dir},
    )

    repo_tree = read_repo_tree(task_dir)
    hinted_files = extract_filepaths_from_issue(body)

    log_workflow_step(
        run_id,
        issue_number,
        "repo_tree_done",
        "Repository tree and issue-mentioned file hints are ready.",
        {
            "repo_tree_chars": len(repo_tree or ""),
            "hinted_files": hinted_files,
        },
    )

    requested_scope = (cognee_scope_hash or "").strip()
    ready_index = get_ready_cognee_index(repo, requested_scope)

    if not ready_index and cognee_dataset_name:
        log_workflow_step(
            run_id,
            issue_number,
            "context_scope_not_ready",
            "A Cognee dataset was requested, but no ready index record was found for it; continuing without Cognee context.",
            {
                "repo": repo,
                "requested_scope_hash": requested_scope,
                "requested_dataset_name": cognee_dataset_name,
                "requested_code_dataset_name": cognee_code_dataset_name,
                "requested_rules_dataset_name": cognee_rules_dataset_name,
                "requested_snapshot_key": cognee_snapshot_key,
            },
        )

    log_workflow_step(
        run_id,
        issue_number,
        "context_search_start",
        "Searching the selected ready Cognee dataset for issue-relevant repository context."
        if ready_index
        else "No ready Cognee dataset is available; continuing with repo tree and direct file reads.",
        {
            "repo": repo,
            "hinted_files": hinted_files,
            "preseed_skipped": not ALLOW_AGENT_COGNEE_SEEDING or SKIP_COGNEE_PRESEED,
            "requested_scope_hash": requested_scope,
            "ready_scope_hash": ready_index.get("scope_hash") if ready_index else "",
            "dataset_name": ready_index.get("dataset_name") if ready_index else "",
            "code_dataset_name": ready_index.get("code_dataset_name") if ready_index else "",
            "rules_dataset_name": ready_index.get("rules_dataset_name") if ready_index else "",
            "snapshot_key": ready_index.get("snapshot_key") if ready_index else "",
        },
    )

    cognee_context = ""
    if ready_index:
        try:
            cognee_context = search_issue_context_scoped(title, body, hinted_files, repo=repo, index_record=ready_index)
        except Exception as e:
            _record_cognee_warning(run_id, issue_number, "search_context", e)
            if cognee_required():
                raise
            cognee_context = ""

    log_workflow_step(
        run_id,
        issue_number,
        "context_search_done",
        "Relevant Cognee context was retrieved from the selected ready dataset."
        if cognee_context
        else "No Cognee context was retrieved; continuing with repo tree and direct file reads.",
        {
            "context_chars": len(cognee_context or ""),
            "hinted_files": hinted_files,
            "scope_hash": ready_index.get("scope_hash") if ready_index else "",
            "dataset_name": ready_index.get("dataset_name") if ready_index else "",
        },
    )

    log_workflow_step(
        run_id,
        issue_number,
        "planner_stage1_start",
        "Planning implementation steps and selecting files to inspect.",
        {
            "repo_tree_chars": len(repo_tree or ""),
            "hinted_files": hinted_files,
            "cognee_context_chars": len(cognee_context or ""),
        },
    )

    stage1 = run_stage1(title, body, repo_tree, hinted_files, cognee_context)

    log_workflow_step(
        run_id,
        issue_number,
        "planner_stage1_done",
        "Planner completed the implementation plan.",
        {
            "read_files": stage1.get("read_files", []),
            "planned_edits": stage1.get("edits", []),
            "run_commands": stage1.get("run", []),
            "context_summary": (stage1.get("context_summary") or "")[:500],
        },
    )

    read_files = stage1.get("read_files", []) or []

    merged_files: list[str] = []

    for p in hinted_files + list(read_files):
        p = (p or "").lstrip("./")

        if p and is_safe_relpath(p) and p not in merged_files:
            merged_files.append(p)

        if len(merged_files) >= MAX_FILES_TO_READ:
            break

    chunks: list[str] = []

    for p in merged_files:
        content = read_text_file(task_dir, p, max_chars=MAX_FILE_CHARS)
        if content == "":
            continue
        chunks.append(f"--- FILE: {p} ---\n{content}\n")

    file_context = "\n".join(chunks) if chunks else "(No file contents were loaded.)"

    log_workflow_step(
        run_id,
        issue_number,
        "files_loaded",
        "Target files were loaded for editing.",
        {
            "count": len(chunks),
            "paths": merged_files,
        },
    )

    log_workflow_step(
        run_id,
        issue_number,
        "writer_stage2_start",
        "Generating concrete code changes.",
        {
            "loaded_file_count": len(chunks),
            "loaded_paths": merged_files,
            "cognee_context_chars": len(cognee_context or ""),
        },
    )

    plan = run_stage2(title, body, repo_tree, file_context, stage1, cognee_context)

    log_workflow_step(
        run_id,
        issue_number,
        "writer_stage2_done",
        "Code changes were generated.",
        {
            "edit_count": len(plan.get("edits", []) or []),
            "edit_paths": [e.get("path") for e in (plan.get("edits", []) or [])],
            "run_commands": plan.get("run", []),
        },
    )

    steps = plan.get("plan", stage1.get("plan", [])) or []
    edits = plan.get("edits", []) or []
    notes = plan.get("notes", stage1.get("notes", "")) or ""
    run_cmds = safe_commands_only(plan.get("run", stage1.get("run", [])) or [])

    gh_post_comment(
        repo,
        issue_number,
        "🤖 Proposed plan:\n" + "\n".join([f"- {s}" for s in steps]) + "\n\nApplying edits...",
    )

    log_workflow_step(
        run_id,
        issue_number,
        "apply_edits_start",
        "Applying generated file changes to the local workspace.",
        {
            "edit_count": len(edits),
            "edit_paths": [e.get("path") for e in edits],
        },
    )

    apply_edits(task_dir, edits)

    log_workflow_step(
        run_id,
        issue_number,
        "apply_edits_done",
        "Generated changes were applied.",
        {
            "files": [e.get("path") for e in edits],
        },
    )

    commit_msg = f"Agent: {title} (issue #{issue_number})"
    target_branch = branch if not NO_PR else os.environ.get("BASE_BRANCH", "main")

    log_workflow_step(
        run_id,
        issue_number,
        "git_commit_local_start",
        "Committing generated changes locally before validation.",
        {
            "commit_message": commit_msg,
            "branch": target_branch,
            "no_pr": NO_PR,
        },
    )

    committed = git_commit_changes(task_dir, commit_msg)

    if not committed:
        gh_post_comment(repo, issue_number, "⚠️ No file changes were produced, so no PR was opened.")
        update_run_status(run_id, "skipped", "No file changes were produced.")
        put_run_event(
            run_id,
            "skipped",
            "No file changes were produced.",
            {"repo": repo, "issue_number": issue_number},
        )
        return ""

    results: list[str] = []
    validation_failed = False
    escalation_attempted = False
    escalation_notes = ""

    log_workflow_step(
        run_id,
        issue_number,
        "validation_attempt_1_start",
        "Running validation after the initial local commit.",
        {
            "commands": validation_command_list(task_dir, run_cmds),
        },
    )

    first_results, first_failed = run_validation_suite(task_dir, run_cmds)
    results.extend(["\n--- Validation attempt 1 ---\n"] + first_results)
    validation_failed = first_failed

    log_workflow_step(
        run_id,
        issue_number,
        "validation_attempt_1_done",
        "Initial validation completed.",
        {
            "failed": first_failed,
            "result_count": len(first_results),
        },
    )

    if first_failed and ESCALATION_ON_VALIDATION_FAILURE:
        escalation_attempted = True

        gh_post_comment(
            repo,
            issue_number,
            "⚠️ Initial validation failed. Sending failure output and local diff to the escalation model for one repair attempt.",
        )

        log_workflow_step(
            run_id,
            issue_number,
            "escalation_repair_start",
            "Running escalation model repair after validation failure.",
            {
                "escalation_model": os.environ.get("BEDROCK_ESCALATION_MODEL", ""),
            },
        )

        try:
            repair_plan, changed_files_before_repair = run_escalation_repair(
                repo=repo,
                issue_number=issue_number,
                run_id=run_id,
                title=title,
                body=body,
                repo_tree=repo_tree,
                task_dir=task_dir,
                stage1=stage1,
                initial_plan=plan,
                merged_files=merged_files,
                ready_index=ready_index,
                cognee_context=cognee_context,
                validation_results=first_results,
            )

            repair_edits = repair_plan.get("edits", []) or []
            repair_run_cmds = safe_commands_only(repair_plan.get("run", []) or [])
            escalation_notes = repair_plan.get("notes", "") or ""

            log_workflow_step(
                run_id,
                issue_number,
                "escalation_repair_done",
                "Escalation model repair completed.",
                {
                    "edit_count": len(repair_edits),
                    "edit_paths": [e.get("path") for e in repair_edits],
                    "changed_files_before_repair": changed_files_before_repair,
                },
            )

            repair_committed = git_commit_changes(
                task_dir,
                f"Agent: repair validation failure for issue #{issue_number}",
            )

            if repair_committed:
                log_workflow_step(
                    run_id,
                    issue_number,
                    "validation_attempt_2_start",
                    "Running validation again after escalation repair.",
                    {
                        "commands": validation_command_list(task_dir, repair_run_cmds or run_cmds),
                    },
                )

                second_results, second_failed = run_validation_suite(task_dir, repair_run_cmds or run_cmds)
                results.extend(["\n--- Validation attempt 2 after escalation repair ---\n"] + second_results)
                validation_failed = second_failed

                log_workflow_step(
                    run_id,
                    issue_number,
                    "validation_attempt_2_done",
                    "Second validation completed after escalation repair.",
                    {
                        "failed": second_failed,
                        "result_count": len(second_results),
                    },
                )
            else:
                results.append("\n--- Escalation repair ---\nEscalation model produced no additional file changes.\n")
                validation_failed = True

        except Exception as e:
            validation_failed = True
            escalation_notes = f"Escalation repair failed: {e}"
            results.append(f"\n--- Escalation repair failed ---\nERROR: {e}\n")

            log_workflow_step(
                run_id,
                issue_number,
                "escalation_repair_failed",
                "Escalation repair failed; PR will still be opened with validation failure details.",
                {
                    "error": str(e),
                },
            )

    log_workflow_step(
        run_id,
        issue_number,
        "git_push_start",
        "Pushing final branch after validation/escalation workflow.",
        {
            "branch": target_branch,
            "validation_failed": validation_failed,
            "escalation_attempted": escalation_attempted,
        },
    )

    git_push_branch(task_dir, target_branch)

    if NO_PR:
        pr_url = ""
    else:
        emit_event({"type": "pr_create_start", "run_id": run_id, "issue": issue_number, "branch": branch})

        validation_summary = (
            "✅ Validation passed."
            if not validation_failed
            else "⚠️ Validation is still failing after the escalation repair attempt. This PR is opened for human review."
        )

        pr_body = f"""Automated changes for issue #{issue_number}.

Validation status:
{validation_summary}

Escalation attempted:
{escalation_attempted}

Notes:
{notes}

Escalation notes:
{escalation_notes}

Commands run:
{('' if results else 'None')}
{''.join(results)[:8000]}
"""
        pr_url = open_pr(repo, branch, f"{title} (agent)", pr_body)

    emit_event(
        {
            "type": "cognee_seed_skipped",
            "mode": "post_success_pr",
            "run_id": run_id,
            "issue": issue_number,
            "repo": repo,
            "reason": "Repository Cognee ingestion is handled by the separate Cognee indexer service.",
            "changed_files": [e.get("path") for e in edits],
        }
    )

    try:
        put_run_event(
            run_id,
            "cognee_seed_skipped",
            "Repository Cognee ingestion is handled by the separate Cognee indexer service.",
            {
                "repo": repo,
                "issue_number": issue_number,
                "changed_files": [e.get("path") for e in edits],
            },
        )
    except Exception:
        pass

    try:
        add_task_memory(
            issue_number=issue_number,
            title=title,
            body=body,
            changed_files=[e.get("path") for e in edits],
            notes=notes,
            status="success",
            repo=repo,
        )
    except Exception as e:
        _record_cognee_warning(run_id, issue_number, "add_success_task_memory", e)

    if NO_PR:
        if validation_failed:
            gh_post_comment(
                repo,
                issue_number,
                "⚠️ Changes were pushed directly, but validation is still failing after escalation.",
            )
            update_run_status(run_id, "success", "Changes pushed, but validation failed after escalation.")
        else:
            gh_post_comment(
                repo,
                issue_number,
                "✅ Changes were committed, validated, and pushed directly.",
            )
            update_run_status(run_id, "success", "Changes were validated and pushed directly.")
    else:
        if validation_failed:
            gh_post_comment(
                repo,
                issue_number,
                f"⚠️ Opened PR with failing validation after escalation: {pr_url}\n\nPlease review the included test failure output.",
            )
            update_run_status(run_id, "success", "PR opened with failing validation after escalation.", pr_url=pr_url)
        else:
            gh_post_comment(
                repo,
                issue_number,
                f"✅ Opened PR after successful validation: {pr_url}\n\nPlease review and merge if it looks good.",
            )
            update_run_status(run_id, "success", "Pull request opened after successful validation.", pr_url=pr_url)

    put_run_event(
        run_id,
        "success",
        "Run completed successfully.",
        {
            "pr_url": pr_url,
        },
    )

    return pr_url


def process_sqs_job(job: dict[str, Any]) -> None:
    run_id = str(job["run_id"])
    repo = validate_repo(str(job["repo"]))
    issue_number = int(job["issue_number"])
    title = str(job.get("title") or "")
    cognee_scope_hash = str(job.get("cognee_scope_hash") or job.get("scope_hash") or "")
    cognee_dataset_name = str(job.get("cognee_dataset_name") or job.get("dataset_name") or "")
    cognee_code_dataset_name = str(job.get("cognee_code_dataset_name") or job.get("code_dataset_name") or cognee_dataset_name or "")
    cognee_rules_dataset_name = str(job.get("cognee_rules_dataset_name") or job.get("rules_dataset_name") or "")
    cognee_snapshot_key = str(job.get("cognee_snapshot_key") or job.get("snapshot_key") or "")

    process_github_issue(
        run_id=run_id,
        repo=repo,
        issue_number=issue_number,
        fallback_title=title,
        cognee_scope_hash=cognee_scope_hash,
        cognee_dataset_name=cognee_dataset_name,
        cognee_code_dataset_name=cognee_code_dataset_name,
        cognee_rules_dataset_name=cognee_rules_dataset_name,
        cognee_snapshot_key=cognee_snapshot_key,
    )


def _record_failed_task_memory(job: dict[str, Any], error: Exception) -> None:
    try:
        issue_number = int(job.get("issue_number", -1))
        title = str(job.get("title", ""))
        repo = str(job.get("repo", ""))
        body = json.dumps(job, ensure_ascii=False)

        if issue_number > 0:
            add_task_memory(
                issue_number=issue_number,
                title=title,
                body=body,
                changed_files=[],
                notes=f"Worker failed before completion: {error}",
                status="error",
                repo=repo,
            )
    except Exception as mem_error:
        print(f"[WARN] Failed to add Cognee error task memory: {mem_error}", flush=True)


def single_job_mode_enabled() -> bool:
    return str(os.environ.get("AGENT_WORKER_SINGLE_JOB", "1")).lower() in {"1", "true", "yes", "on"}

def main() -> None:
    run(["gh", "auth", "status"])
    os.makedirs(WORKDIR, exist_ok=True)

    print("Agent worker started. Waiting for SQS jobs...", flush=True)
    emit_event({"type": "worker_started", "cognee": cognee_status()})

    while True:
        response = sqs.receive_message(
            QueueUrl=AGENT_QUEUE_URL,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
            VisibilityTimeout=int(os.environ.get("SQS_VISIBILITY_TIMEOUT", "3600")),
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
                run_id = str(job["run_id"])

                put_run_event(
                    run_id,
                    "sqs_received",
                    "Worker received the queued issue.",
                    {
                        "repo": job.get("repo"),
                        "issue_number": job.get("issue_number"),
                    },
                )

                with global_task_lock("agent-worker"):
                    process_sqs_job(job)

                sqs.delete_message(
                    QueueUrl=AGENT_QUEUE_URL,
                    ReceiptHandle=receipt_handle,
                )

                print(f"[INFO] Completed SQS job: {job}", flush=True)
                if single_job_mode_enabled():
                    print("[INFO] AGENT_WORKER_SINGLE_JOB enabled; exiting to release Cognee/Ladybug DB handles.", flush=True)
                    return

            except Exception as e:
                print(f"[ERROR] {e}", flush=True)

                try:
                    run_id = str(job.get("run_id", ""))

                    if run_id:
                        update_run_status(run_id, "error", str(e))
                        put_run_event(
                            run_id,
                            "error",
                            "Worker failed while processing the queued issue.",
                            {
                                "error": str(e),
                                "job": job,
                            },
                        )

                    _record_failed_task_memory(job, e)

                    issue_number = int(job.get("issue_number", -1))
                    repo = str(job.get("repo", ""))

                    if repo and issue_number > 0:
                        try:
                            gh_post_comment(
                                repo,
                                issue_number,
                                f"❌ Agent error: `{e}`\n\nThe run was stopped and marked as failed.",
                            )
                        except Exception:
                            pass

                except Exception:
                    pass

                sqs.delete_message(
                    QueueUrl=AGENT_QUEUE_URL,
                    ReceiptHandle=receipt_handle,
                )

                if single_job_mode_enabled():
                    print("[INFO] AGENT_WORKER_SINGLE_JOB enabled after failed job; exiting to release Cognee/Ladybug DB handles.", flush=True)
                    return


if __name__ == "__main__":
    try:
        main()
    finally:
        shutdown_cognee_runner()
