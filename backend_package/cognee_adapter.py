# agent_runner/cognee_adapter.py

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import contextlib
import fcntl
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterable
from uuid import NAMESPACE_URL, UUID, uuid5


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _set_env_from_alias(canonical: str, aliases: Iterable[str], default: str | None = None) -> None:
    if os.environ.get(canonical):
        return
    for alias in aliases:
        value = os.environ.get(alias)
        if value:
            os.environ[canonical] = value
            return
    if default is not None:
        os.environ[canonical] = default


# Process-level Cognee settings must be available before importing cognee.
COGNEE_ROOT = _env("COGNEE_ROOT", "/var/lib/agent-runner/cognee")
os.environ.setdefault("SYSTEM_ROOT_DIRECTORY", os.path.join(COGNEE_ROOT, "system"))
os.environ.setdefault("DATA_ROOT_DIRECTORY", os.path.join(COGNEE_ROOT, "data"))
os.environ.setdefault("COGNEE_LOGS_DIR", os.path.join(COGNEE_ROOT, "logs"))
os.environ.setdefault("LOG_LEVEL", _env("COGNEE_LOG_LEVEL", "INFO") or "INFO")

# Make Bedrock region available to Cognee/LiteLLM when your app uses BEDROCK_REGION.
_set_env_from_alias("AWS_REGION", ["COGNEE_AWS_REGION", "BEDROCK_REGION"], default=None)

# LLM aliases. COGNEE_* wins, then Cognee-standard env names, then the old local default.
_llm_provider_default = _env("COGNEE_LLM_PROVIDER", _env("LLM_PROVIDER", "ollama")) or "ollama"
_set_env_from_alias("LLM_PROVIDER", ["COGNEE_LLM_PROVIDER"], default=_llm_provider_default)

if (_env("LLM_PROVIDER", "ollama") or "ollama").lower() == "bedrock":
    _llm_model_default = _env("COGNEE_LLM_MODEL", _env("LLM_MODEL", _env("BEDROCK_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0")))
else:
    _llm_model_default = _env("COGNEE_LLM_MODEL", _env("LLM_MODEL", "qwen2.5-coder:32b-instruct-q4_K_M"))

_set_env_from_alias("LLM_MODEL", ["COGNEE_LLM_MODEL"], default=_llm_model_default)

if (_env("LLM_PROVIDER", "ollama") or "ollama").lower() == "ollama":
    _set_env_from_alias("LLM_API_KEY", ["COGNEE_LLM_API_KEY"], default="ollama")
    _set_env_from_alias("LLM_ENDPOINT", ["COGNEE_LLM_ENDPOINT"], default="http://localhost:11434/v1")
else:
    _set_env_from_alias("LLM_API_KEY", ["COGNEE_LLM_API_KEY"], default=None)
    _set_env_from_alias("LLM_ENDPOINT", ["COGNEE_LLM_ENDPOINT"], default=None)

# Embedding aliases. Never leave embeddings to the Cognee/OpenAI default by accident.
_embedding_provider_default = _env("COGNEE_EMBEDDING_PROVIDER", _env("EMBEDDING_PROVIDER"))
if not _embedding_provider_default:
    _embedding_provider_default = "bedrock" if (_env("LLM_PROVIDER", "ollama") or "ollama").lower() == "bedrock" else "ollama"
_set_env_from_alias("EMBEDDING_PROVIDER", ["COGNEE_EMBEDDING_PROVIDER"], default=_embedding_provider_default)

_embedding_provider = (_env("EMBEDDING_PROVIDER", "ollama") or "ollama").lower()
if _embedding_provider == "bedrock":
    _set_env_from_alias("EMBEDDING_MODEL", ["COGNEE_EMBEDDING_MODEL"], default="amazon.titan-embed-text-v2:0")
    _set_env_from_alias("EMBEDDING_DIMENSIONS", ["COGNEE_EMBEDDING_DIMENSIONS"], default="1024")
    _set_env_from_alias("EMBEDDING_API_KEY", ["COGNEE_EMBEDDING_API_KEY"], default=None)
elif _embedding_provider == "ollama":
    _set_env_from_alias("EMBEDDING_MODEL", ["COGNEE_EMBEDDING_MODEL"], default="nomic-embed-text:latest")
    _set_env_from_alias("EMBEDDING_ENDPOINT", ["COGNEE_EMBEDDING_ENDPOINT"], default="http://localhost:11434/api/embed")
    _set_env_from_alias("EMBEDDING_DIMENSIONS", ["COGNEE_EMBEDDING_DIMENSIONS"], default="768")
    _set_env_from_alias("EMBEDDING_API_KEY", ["COGNEE_EMBEDDING_API_KEY"], default="ollama")
    os.environ.setdefault("HUGGINGFACE_TOKENIZER", _env("COGNEE_HUGGINGFACE_TOKENIZER", "nomic-ai/nomic-embed-text-v1.5") or "nomic-ai/nomic-embed-text-v1.5")
elif _embedding_provider == "fastembed":
    _set_env_from_alias("EMBEDDING_MODEL", ["COGNEE_EMBEDDING_MODEL"], default="sentence-transformers/all-MiniLM-L6-v2")
    _set_env_from_alias("EMBEDDING_DIMENSIONS", ["COGNEE_EMBEDDING_DIMENSIONS"], default="384")
else:
    _set_env_from_alias("EMBEDDING_MODEL", ["COGNEE_EMBEDDING_MODEL"], default=None)
    _set_env_from_alias("EMBEDDING_ENDPOINT", ["COGNEE_EMBEDDING_ENDPOINT"], default=None)
    _set_env_from_alias("EMBEDDING_DIMENSIONS", ["COGNEE_EMBEDDING_DIMENSIONS"], default=None)
    _set_env_from_alias("EMBEDDING_API_KEY", ["COGNEE_EMBEDDING_API_KEY"], default=None)

import cognee
from cognee import SearchType

try:
    from cognee.modules.engine.models.node_set import NodeSet
except Exception:  # pragma: no cover - optional across Cognee versions
    NodeSet = None


COGNEE_ENABLED = os.environ.get("COGNEE_ENABLED", "0") == "1"
COGNEE_REQUIRED = os.environ.get("COGNEE_REQUIRED", "0") == "1"

REPO_DATASET = os.environ.get("COGNEE_REPO_DATASET", "repo_memory")
TASK_DATASET = os.environ.get("COGNEE_TASK_DATASET", "task_history")

NODESET_REPO_DOCS = ["repo_docs"]
NODESET_CODING_RULES = ["coding_rules"]
NODESET_TASK_HISTORY = ["task_history"]

RULE_FILE_NAMES = {"AGENT_RULES.md", "AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md"}

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".tox",
    "target",
    "coverage",
    ".next",
    ".turbo",
}

KEEP_EXTS = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".java",
    ".go",
    ".rs",
    ".c",
    ".cpp",
    ".h",
    ".hpp",
    ".md",
    ".rst",
    ".txt",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".sql",
}

SECRET_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    "secrets.txt",
    "credentials",
    "id_rsa",
    "id_ed25519",
}

MAX_CONTEXT_CHUNKS = int(os.environ.get("COGNEE_MAX_CONTEXT_CHUNKS", "18"))
MAX_RULE_CHUNKS = int(os.environ.get("COGNEE_MAX_RULE_CHUNKS", "6"))
MAX_HISTORY_CHUNKS = int(os.environ.get("COGNEE_MAX_HISTORY_CHUNKS", "6"))
MAX_CONTEXT_CHARS = int(os.environ.get("COGNEE_MAX_CONTEXT_CHARS", "45000"))
MAX_CHUNK_CHARS = int(os.environ.get("COGNEE_MAX_CHUNK_CHARS", "6000"))
MAX_SEARCH_QUERIES = int(os.environ.get("COGNEE_MAX_SEARCH_QUERIES", "10"))
MAX_SYMBOL_TERMS = int(os.environ.get("COGNEE_MAX_SYMBOL_TERMS", "8"))
MAX_HINT_FILES = int(os.environ.get("COGNEE_MAX_HINT_FILES", "6"))
MAX_REPO_FILES = int(os.environ.get("COGNEE_MAX_REPO_FILES", "750"))
MAX_RULE_FILES = int(os.environ.get("COGNEE_MAX_RULE_FILES", "20"))
MAX_FILE_CHARS = int(os.environ.get("COGNEE_MAX_FILE_CHARS", "24000"))
FILE_SEGMENT_CHARS = int(os.environ.get("COGNEE_FILE_SEGMENT_CHARS", "7000"))
SEARCH_TOP_K = int(os.environ.get("COGNEE_SEARCH_TOP_K", "8"))
SEARCH_TIMEOUT_SECONDS = int(os.environ.get("COGNEE_TIMEOUT", "1800"))
USE_LEXICAL_SEARCH = os.environ.get("COGNEE_USE_LEXICAL_SEARCH", "1") == "1"
REMEMBER_SELF_IMPROVEMENT = os.environ.get("COGNEE_SELF_IMPROVEMENT", "0") == "1"
REMEMBER_CHUNK_SIZE = int(os.environ.get("COGNEE_REMEMBER_CHUNK_SIZE", "0"))
RESET_REPO_DATASETS = os.environ.get("COGNEE_RESET_REPO_DATASETS", "1") == "1"
REPO_DATASET_MODE = os.environ.get("COGNEE_REPO_DATASET_MODE", "auto").lower()
STRICT_REPO_DATASET_RESET = os.environ.get("COGNEE_STRICT_REPO_DATASET_RESET", "1") == "1"
SCOPED_INDEX_DATA_PER_BATCH = int(os.environ.get("COGNEE_SCOPED_INDEX_DATA_PER_BATCH", "20"))

# Local Cognee/Ladybug graph storage is not safe for concurrent access from
# the indexer and worker processes. This host-level lock serializes all Cognee
# reads/writes so jobs wait instead of colliding on cognee_graph_ladybug.
COGNEE_PROCESS_LOCK = os.environ.get("COGNEE_PROCESS_LOCK", "1") == "1"
COGNEE_LOCK_PATH = os.environ.get("COGNEE_LOCK_PATH", os.path.join(COGNEE_ROOT, "cognee-process.lock"))
COGNEE_LOCK_TIMEOUT_SECONDS = int(os.environ.get("COGNEE_LOCK_TIMEOUT_SECONDS", "1800"))
COGNEE_LOCK_POLL_SECONDS = float(os.environ.get("COGNEE_LOCK_POLL_SECONDS", "2"))

@contextlib.contextmanager
def _cognee_process_lock(operation: str = "cognee"):
    if not COGNEE_PROCESS_LOCK:
        yield
        return

    os.makedirs(os.path.dirname(COGNEE_LOCK_PATH), exist_ok=True)
    started = time.time()
    last_log = 0.0

    with open(COGNEE_LOCK_PATH, "w", encoding="utf-8") as lock_file:
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                waited = time.time() - started
                if waited >= COGNEE_LOCK_TIMEOUT_SECONDS:
                    raise TimeoutError(
                        f"Timed out waiting {int(waited)}s for Cognee process lock at {COGNEE_LOCK_PATH}. "
                        "Another worker/indexer may be stuck or still using the local Cognee DB."
                    )
                if waited - last_log >= 30:
                    print(f"[INFO] Waiting for Cognee process lock for {operation}: {int(waited)}s", flush=True)
                    last_log = waited
                time.sleep(COGNEE_LOCK_POLL_SECONDS)

        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


_CURRENT_REPO_SNAPSHOT_KEY = ""
_CURRENT_REPO_CODE_DATASET = ""
_CURRENT_REPO_RULES_DATASET = ""


def _call_config(method_name: str, value: Any) -> None:
    if value is None or value == "":
        return
    method = getattr(cognee.config, method_name, None)
    if callable(method):
        method(value)


def configure_cognee_from_env() -> None:
    """Apply runtime-safe Cognee config setters from environment variables."""
    _call_config("set_llm_provider", os.environ.get("LLM_PROVIDER"))
    _call_config("set_llm_model", os.environ.get("LLM_MODEL"))
    _call_config("set_llm_api_key", os.environ.get("LLM_API_KEY"))
    _call_config("set_llm_endpoint", os.environ.get("LLM_ENDPOINT"))

    try:
        llm_temperature = os.environ.get("LLM_TEMPERATURE")
        if llm_temperature is not None and hasattr(cognee.config, "set_llm_config"):
            cognee.config.set_llm_config({"llm_temperature": float(llm_temperature)})
    except Exception:
        pass

    _call_config("set_embedding_provider", os.environ.get("EMBEDDING_PROVIDER"))
    _call_config("set_embedding_model", os.environ.get("EMBEDDING_MODEL"))
    _call_config("set_embedding_api_key", os.environ.get("EMBEDDING_API_KEY"))
    _call_config("set_embedding_endpoint", os.environ.get("EMBEDDING_ENDPOINT"))

    dimensions = os.environ.get("EMBEDDING_DIMENSIONS")
    if dimensions:
        try:
            _call_config("set_embedding_dimensions", int(dimensions))
        except ValueError:
            _call_config("set_embedding_dimensions", dimensions)

    _call_config("system_root_directory", os.environ.get("SYSTEM_ROOT_DIRECTORY"))
    _call_config("data_root_directory", os.environ.get("DATA_ROOT_DIRECTORY"))


configure_cognee_from_env()


def _safe_dataset_part(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9_.-]+", "_", value)
    value = value.strip("_")
    return value[:120] or "default"


def _base_repo_dataset_part(repo: str) -> str:
    return _safe_dataset_part(repo or "default")


def _snapshot_dataset_part(snapshot_key: str) -> str:
    digest = hashlib.sha1((snapshot_key or "").encode("utf-8", errors="replace")).hexdigest()[:16]
    suffix = _safe_dataset_part(snapshot_key)[-64:]
    return _safe_dataset_part(f"{suffix}_{digest}")


def _repo_code_dataset(repo: str, snapshot_key: str | None = None) -> str:
    base = f"{REPO_DATASET}__{_base_repo_dataset_part(repo)}__code"
    if snapshot_key:
        return f"{base}__{_snapshot_dataset_part(snapshot_key)}"
    return f"{base}__current"


def _repo_rules_dataset(repo: str, snapshot_key: str | None = None) -> str:
    base = f"{REPO_DATASET}__{_base_repo_dataset_part(repo)}__rules"
    if snapshot_key:
        return f"{base}__{_snapshot_dataset_part(snapshot_key)}"
    return f"{base}__current"


def _task_dataset(repo: str) -> str:
    return f"{TASK_DATASET}__{_base_repo_dataset_part(repo)}"


def repo_scope_snapshot_key(
    repo: str,
    ref: str = "main",
    head_sha: str = "",
    scope_hash: str = "",
) -> str:
    ref = (ref or "main").strip() or "main"
    scope_hash = (scope_hash or "default").strip() or "default"
    return f"repo_scope::{repo or 'default'}::{ref}::{scope_hash}"


def repo_scope_datasets(repo: str, scope_hash: str) -> dict[str, str]:
    base = _base_repo_dataset_part(repo)
    scope = _safe_dataset_part(scope_hash or "default")
    code_dataset = f"{REPO_DATASET}__{base}__code__scope_{scope}"
    rules_dataset = f"{REPO_DATASET}__{base}__rules__scope_{scope}"
    return {
        "dataset_name": code_dataset,
        "code_dataset_name": code_dataset,
        "rules_dataset_name": rules_dataset,
    }


def repo_scope_dataset_names(repo: str, ref: str = "main", scope_hash: str = "") -> dict[str, str]:
    names = repo_scope_datasets(repo, scope_hash)
    names["snapshot_key"] = repo_scope_snapshot_key(repo=repo, ref=ref, scope_hash=scope_hash)
    return names


def _repo_scope_data_id(snapshot_key: str, rel_path: str, kind: str, segment: int) -> UUID:
    basis = f"cognimoss:{snapshot_key}:{kind}:{rel_path}:{segment}"
    return uuid5(NAMESPACE_URL, basis)


def _make_data_item(data: str, label: str, metadata: dict[str, Any], data_id: UUID) -> tuple[Any, bool]:
    try:
        from cognee.tasks.ingestion.data_item import DataItem
    except Exception:
        return data, False

    try:
        return DataItem(data=data, label=label, external_metadata=metadata, data_id=data_id), True
    except TypeError:
        try:
            return DataItem(data, label=label, external_metadata=metadata, data_id=data_id), True
        except TypeError:
            try:
                return DataItem(data=data, label=label, external_metadata=metadata), False
            except TypeError:
                return DataItem(data, label=label, external_metadata=metadata), False


def _snapshot_mode() -> bool:
    if REPO_DATASET_MODE in {"snapshot", "versioned"}:
        return True
    if REPO_DATASET_MODE == "current":
        return False
    return not hasattr(cognee, "forget")


class _CogneeAsyncRunner:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="cognee-async-loop", daemon=True)
        self._closed = False
        self._thread.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro):
        if self._closed:
            raise RuntimeError("Cognee async runner is closed.")

        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout=SEARCH_TIMEOUT_SECONDS)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise TimeoutError(f"Cognee operation timed out after {SEARCH_TIMEOUT_SECONDS}s") from exc

    def close(self) -> None:
        if self._closed:
            return

        self._closed = True

        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:
            return

        self._thread.join(timeout=5)

        if not self._loop.is_closed():
            self._loop.close()


_RUNNER = _CogneeAsyncRunner()
atexit.register(_RUNNER.close)


def _run(coro):
    with _cognee_process_lock():
        return _RUNNER.run(coro)


async def _remember_async(data: Any, dataset_name: str, node_set: list[str]) -> None:
    if not data:
        return

    if hasattr(cognee, "remember"):
        kwargs: dict[str, Any] = {
            "dataset_name": dataset_name,
            "node_set": node_set,
            "self_improvement": REMEMBER_SELF_IMPROVEMENT,
        }
        if REMEMBER_CHUNK_SIZE > 0:
            kwargs["chunk_size"] = REMEMBER_CHUNK_SIZE
        try:
            await cognee.remember(data, **kwargs)
            return
        except TypeError:
            # Older v1 builds may not accept every modern keyword.
            kwargs.pop("self_improvement", None)
            kwargs.pop("chunk_size", None)
            await cognee.remember(data, **kwargs)
            return

    await cognee.add(data=data, dataset_name=dataset_name, node_set=node_set)
    await cognee.cognify(datasets=[dataset_name])


async def _forget_dataset_if_exists(dataset_name: str) -> None:
    if not dataset_name:
        return

    if not hasattr(cognee, "forget"):
        if STRICT_REPO_DATASET_RESET:
            raise RuntimeError(
                "Cognee dataset reset requested, but this Cognee version does not expose cognee.forget(). "
                "Set COGNEE_REPO_DATASET_MODE=snapshot or upgrade Cognee."
            )
        return

    try:
        await cognee.forget(dataset=dataset_name)
    except Exception as e:
        msg = str(e).lower()
        not_found_markers = (
            "not found",
            "does not exist",
            "no dataset",
            "404",
            "could not find",
            "dataset not found",
            "nonetype",
            "none type",
            "object has no attribute 'id'",
        )
        if any(marker in msg for marker in not_found_markers):
            return
        if STRICT_REPO_DATASET_RESET:
            raise
        print(f"[WARN] Cognee could not reset dataset {dataset_name}: {e}", flush=True)


async def _search_async(
    query_text: str,
    dataset_name: str,
    query_type: Any,
    top_k: int = SEARCH_TOP_K,
):
    kwargs: dict[str, Any] = {
        "query_text": query_text,
        "query_type": query_type,
        "datasets": [dataset_name],
        "top_k": top_k,
    }

    if hasattr(cognee, "recall"):
        try:
            return await cognee.recall(**kwargs)
        except TypeError:
            # Some builds expose recall(query, ...) rather than query_text keyword.
            kwargs.pop("query_text", None)
            return await cognee.recall(query_text, **kwargs)

    return await cognee.search(**kwargs)


def _model_to_dict(item: Any) -> Any:
    if hasattr(item, "model_dump") and callable(item.model_dump):
        try:
            return item.model_dump()
        except Exception:
            pass
    if hasattr(item, "dict") and callable(item.dict):
        try:
            return item.dict()
        except Exception:
            pass
    return item


def _flatten_search_results(results: Any) -> list[str]:
    texts: list[str] = []

    def visit(value: Any) -> None:
        value = _model_to_dict(value)

        if value is None:
            return

        if isinstance(value, str):
            if value.strip():
                texts.append(value)
            return

        if isinstance(value, (list, tuple, set)):
            for inner in value:
                visit(inner)
            return

        if isinstance(value, dict):
            if "search_result" in value:
                visit(value.get("search_result"))
                return
            if "text" in value and value.get("text"):
                texts.append(str(value["text"]))
                return
            if "context" in value and value.get("context"):
                visit(value.get("context"))
                return
            if "answer" in value and value.get("answer"):
                texts.append(str(value["answer"]))
                return
            if "result" in value and value.get("result"):
                visit(value.get("result"))
                return
            if "content" in value and value.get("content"):
                visit(value.get("content"))
                return
            return

        text = str(value).strip()
        if text:
            texts.append(text)

    visit(results)
    return texts


def _unique_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    for item in items:
        text = (item or "").strip()
        if not text:
            continue
        key = re.sub(r"\s+", " ", text)
        if key in seen:
            continue
        seen.add(key)
        out.append(text)

    return out


def _relpath(root: Path, path: Path) -> str:
    return str(path.resolve().relative_to(root.resolve()))


def _is_secret_file(path: Path) -> bool:
    name = path.name
    if name in SECRET_FILE_NAMES:
        return True
    if name.endswith((".pem", ".key", ".p12", ".pfx")):
        return True
    return False


def _collect_repo_files(repo_root: Path) -> tuple[list[Path], list[Path]]:
    repo_docs: list[Path] = []
    rule_docs: list[Path] = []

    for p in sorted(repo_root.rglob("*")):
        if not p.is_file():
            continue

        try:
            rel_parts = p.resolve().relative_to(repo_root.resolve()).parts
        except Exception:
            rel_parts = p.parts

        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        if _is_secret_file(p):
            continue
        if p.suffix.lower() not in KEEP_EXTS:
            continue

        if p.name in RULE_FILE_NAMES:
            if len(rule_docs) < MAX_RULE_FILES:
                rule_docs.append(p)
        elif len(repo_docs) < MAX_REPO_FILES:
            repo_docs.append(p)

    return repo_docs, rule_docs


def _read_text(path: Path, max_chars: int = MAX_FILE_CHARS) -> str:
    try:
        raw = path.read_bytes()[: max_chars + 1]
        if b"\x00" in raw:
            return ""
        text = raw.decode("utf-8", errors="replace")
        return text[:max_chars]
    except Exception:
        return ""


def _extract_imports_and_symbols(text: str) -> tuple[list[str], list[str]]:
    imports: list[str] = []
    symbols: list[str] = []

    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("import ") or stripped.startswith("from "):
            imports.append(stripped)
        elif stripped.startswith("export ") and " from " in stripped:
            imports.append(stripped)
        elif "require(" in stripped and len(stripped) < 240:
            imports.append(stripped)

    symbol_patterns = [
        r"\b(?:class|def|function|interface|type|enum)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
        r"\bexport\s+(?:async\s+)?(?:function|class|interface|type|const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
        r"\b(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)\s*=>|function\b)",
    ]
    for pattern in symbol_patterns:
        symbols.extend(re.findall(pattern, text or ""))

    return _unique_keep_order(imports)[:30], _unique_keep_order(symbols)[:30]


def _format_bullets(items: list[str]) -> str:
    if not items:
        return "(none)"
    return "\n".join(f"- {item}" for item in items)


def _split_text(text: str, size: int) -> list[str]:
    text = text or ""
    if not text:
        return []
    if size <= 0 or len(text) <= size:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            newline = text.rfind("\n", start, end)
            if newline > start + int(size * 0.5):
                end = newline + 1
        chunks.append(text[start:end])
        start = end
    return chunks


def _build_repo_doc_payloads(root: Path, path: Path, snapshot_key: str) -> list[str]:
    rel = _relpath(root, path)
    text = _read_text(path)
    if not text:
        return []

    imports, symbols = _extract_imports_and_symbols(text)
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
    segments = _split_text(text, FILE_SEGMENT_CHARS)
    total = len(segments)

    payloads: list[str] = []
    for idx, segment in enumerate(segments, start=1):
        payloads.append(
            f"""SNAPSHOT: {snapshot_key}
PATH: {rel}
KIND: repo_doc
SEGMENT: {idx}/{total}
SHA256_16: {digest}
IMPORTS:
{_format_bullets(imports)}
SYMBOLS:
{_format_bullets(symbols)}
CONTENT:
{segment}
""".strip()
        )
    return payloads


def _build_rule_doc_payloads(root: Path, path: Path, snapshot_key: str) -> list[str]:
    rel = _relpath(root, path)
    text = _read_text(path)
    if not text:
        return []

    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
    segments = _split_text(text, FILE_SEGMENT_CHARS)
    total = len(segments)

    payloads: list[str] = []
    for idx, segment in enumerate(segments, start=1):
        payloads.append(
            f"""SNAPSHOT: {snapshot_key}
PATH: {rel}
KIND: coding_rule
SEGMENT: {idx}/{total}
SHA256_16: {digest}
CONTENT:
{segment}
""".strip()
        )
    return payloads


def _extract_issue_terms(issue_title: str, issue_body: str, hinted_files: list[str]) -> list[str]:
    text = "\n".join([issue_title or "", issue_body or "", "\n".join(hinted_files or [])])

    raw_terms = re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text)
    for path in hinted_files or []:
        stem = Path(path).stem
        if stem:
            raw_terms.append(stem)
        raw_terms.extend([part for part in re.split(r"[/_.-]+", path) if len(part) > 2])

    stop = {
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "into",
        "when",
        "then",
        "have",
        "will",
        "should",
        "would",
        "could",
        "issue",
        "github",
        "agent",
        "task",
        "pull",
        "request",
        "tests",
        "test",
        "file",
        "files",
        "code",
        "function",
        "functions",
        "import",
        "imports",
        "using",
        "used",
        "related",
        "need",
        "make",
        "mode",
        "path",
        "update",
        "change",
        "changes",
    }

    keep: list[str] = []
    seen: set[str] = set()

    for term in raw_terms:
        low = term.lower()
        if low in stop:
            continue
        if low in seen:
            continue
        seen.add(low)
        keep.append(term)

    return keep[:20]


def _git_commit_sha(repo_root: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if res.returncode == 0:
            return (res.stdout or "").strip()
    except Exception:
        pass
    return ""


def _snapshot_key_for_repo(repo_root: str, repo: str = "") -> str:
    root = Path(repo_root).resolve()
    sha = _git_commit_sha(root) or root.name
    repo_part = repo or str(root)
    return f"repo_snapshot::{repo_part}::{sha}"



def _is_indexable_repo_path(root: Path, path: Path) -> bool:
    try:
        rel_parts = path.resolve().relative_to(root.resolve()).parts
    except Exception:
        return False
    if not path.is_file():
        return False
    if any(part in SKIP_DIRS for part in rel_parts):
        return False
    if _is_secret_file(path):
        return False
    if path.suffix.lower() not in KEEP_EXTS:
        return False
    return True


def _normalize_scoped_paths(paths: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    for raw in paths or []:
        rel = str(raw or "").replace("\\", "/").strip().lstrip("./")
        while "//" in rel:
            rel = rel.replace("//", "/")
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            continue
        if rel not in out:
            out.append(rel)
    return out


def _paths_to_existing_files(root: Path, paths: Iterable[str] | None) -> list[Path]:
    files: list[Path] = []
    seen: set[str] = set()
    for rel in _normalize_scoped_paths(paths):
        full = (root / rel).resolve()
        try:
            full.relative_to(root.resolve())
        except Exception:
            continue
        if not _is_indexable_repo_path(root, full):
            continue
        rel_s = _relpath(root, full)
        if rel_s in seen:
            continue
        seen.add(rel_s)
        files.append(full)
    return sorted(files, key=lambda p: _relpath(root, p))


def _build_scoped_items_for_path(
    root: Path,
    path: Path,
    snapshot_key: str,
    repo: str,
    scope_hash: str,
    code_dataset: str,
    rules_dataset: str,
) -> tuple[list[Any], list[Any], list[dict[str, str]], bool]:
    rel = _relpath(root, path)
    is_rule = path.name in RULE_FILE_NAMES
    kind = "rules" if is_rule else "code"
    dataset_name = rules_dataset if is_rule else code_dataset
    payloads = _build_rule_doc_payloads(root, path, snapshot_key) if is_rule else _build_repo_doc_payloads(root, path, snapshot_key)

    code_items: list[Any] = []
    rule_items: list[Any] = []
    file_data_ids: list[dict[str, str]] = []
    data_ids_supported = True

    for idx, payload in enumerate(payloads, start=1):
        data_id = _repo_scope_data_id(snapshot_key, rel, kind, idx)
        metadata = {
            "repo": repo,
            "scope_hash": scope_hash,
            "snapshot_key": snapshot_key,
            "path": rel,
            "kind": "coding_rule" if is_rule else "repo_doc",
            "segment": str(idx),
        }
        item, supported = _make_data_item(payload, f"{rel}#{kind}#{idx}", metadata, data_id)
        if is_rule:
            rule_items.append(item)
        else:
            code_items.append(item)
        if supported:
            file_data_ids.append(
                {
                    "dataset_name": dataset_name,
                    "data_id": str(data_id),
                    "kind": kind,
                    "segment": str(idx),
                }
            )
        else:
            data_ids_supported = False

    return code_items, rule_items, file_data_ids, data_ids_supported


async def _add_items_and_cognify_async(items: list[Any], dataset_name: str, node_set: list[str]) -> None:
    if not items:
        return
    kwargs: dict[str, Any] = {
        "data": items,
        "dataset_name": dataset_name,
        "node_set": node_set,
        "incremental_loading": True,
        "data_per_batch": SCOPED_INDEX_DATA_PER_BATCH,
    }
    try:
        await cognee.add(**kwargs)
    except TypeError:
        kwargs.pop("data_per_batch", None)
        try:
            await cognee.add(**kwargs)
        except TypeError:
            kwargs.pop("incremental_loading", None)
            await cognee.add(**kwargs)
    try:
        await cognee.cognify(datasets=[dataset_name], incremental_loading=True)
    except TypeError:
        await cognee.cognify(datasets=[dataset_name])


async def _forget_data_id_if_exists(dataset_name: str, data_id: str) -> None:
    if not dataset_name or not data_id or not hasattr(cognee, "forget"):
        return
    try:
        await cognee.forget(dataset=dataset_name, data_id=UUID(str(data_id)))
    except Exception as e:
        msg = str(e).lower()
        if any(marker in msg for marker in ("not found", "does not exist", "no dataset", "404", "could not find")):
            return
        raise


def forget_repo_scope(repo: str, scope_hash: str) -> None:
    if not COGNEE_ENABLED:
        return
    names = repo_scope_datasets(repo, scope_hash)
    _run(_forget_dataset_if_exists(names["code_dataset_name"]))
    _run(_forget_dataset_if_exists(names["rules_dataset_name"]))


def seed_repo_scope_files(
    repo_root: str,
    repo: str,
    scope_hash: str,
    snapshot_key: str,
    files: list[dict[str, Any]],
    reset: bool = False,
    forget_data_ids: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    if not COGNEE_ENABLED:
        return {}
    return _run(
        _seed_repo_scope_files_async(
            repo_root=repo_root,
            repo=repo,
            scope_hash=scope_hash,
            snapshot_key=snapshot_key,
            files=files,
            reset=reset,
            forget_data_ids=forget_data_ids or [],
        )
    )


async def _seed_repo_scope_files_async(
    repo_root: str,
    repo: str,
    scope_hash: str,
    snapshot_key: str,
    files: list[dict[str, Any]],
    reset: bool,
    forget_data_ids: list[dict[str, str]],
) -> dict[str, Any]:
    global _CURRENT_REPO_CODE_DATASET, _CURRENT_REPO_RULES_DATASET, _CURRENT_REPO_SNAPSHOT_KEY
    root = Path(repo_root).resolve()
    names = repo_scope_datasets(repo, scope_hash)
    code_dataset = names["code_dataset_name"]
    rules_dataset = names["rules_dataset_name"]

    if reset:
        await _forget_dataset_if_exists(code_dataset)
        await _forget_dataset_if_exists(rules_dataset)

    for entry in forget_data_ids or []:
        dataset = str(entry.get("dataset_name") or "")
        data_id = str(entry.get("data_id") or "")
        await _forget_data_id_if_exists(dataset, data_id)

    selected_paths = [str(f.get("path") or "") for f in files]
    paths = _paths_to_existing_files(root, selected_paths)
    code_items: list[Any] = []
    rule_items: list[Any] = []
    file_data_ids: dict[str, list[dict[str, str]]] = {}
    data_ids_supported = True

    for path in paths:
        rel = _relpath(root, path)
        c_items, r_items, ids, supported = _build_scoped_items_for_path(
            root=root,
            path=path,
            snapshot_key=snapshot_key,
            repo=repo,
            scope_hash=scope_hash,
            code_dataset=code_dataset,
            rules_dataset=rules_dataset,
        )
        code_items.extend(c_items)
        rule_items.extend(r_items)
        file_data_ids[rel] = ids
        data_ids_supported = data_ids_supported and supported

    if code_items:
        await _add_items_and_cognify_async(code_items, code_dataset, NODESET_REPO_DOCS)
    if rule_items:
        await _add_items_and_cognify_async(rule_items, rules_dataset, NODESET_CODING_RULES)

    _CURRENT_REPO_SNAPSHOT_KEY = snapshot_key
    _CURRENT_REPO_CODE_DATASET = code_dataset
    _CURRENT_REPO_RULES_DATASET = rules_dataset

    return {
        "snapshot_key": snapshot_key,
        "dataset_name": code_dataset,
        "code_dataset_name": code_dataset,
        "rules_dataset_name": rules_dataset,
        "file_data_ids": file_data_ids,
        "data_ids_supported": data_ids_supported,
        "code_payload_count": len(code_items),
        "rules_payload_count": len(rule_items),
    }


def seed_repo_scope_memory(
    repo_root: str,
    repo: str = "",
    ref: str = "main",
    scope_hash: str = "",
    selected_paths: list[str] | None = None,
    changed_paths: list[str] | None = None,
    deleted_paths: list[str] | None = None,
    old_data_ids_by_path: dict[str, Any] | None = None,
    force_full_reseed: bool = False,
    dataset_name: str = "",
    code_dataset_name: str = "",
    rules_dataset_name: str = "",
    snapshot_key: str = "",
    action: str = "auto",
) -> dict[str, Any]:
    """Compatibility wrapper for indexers that pass path lists instead of file manifests."""
    snapshot_key = snapshot_key or repo_scope_snapshot_key(repo=repo, ref=ref, scope_hash=scope_hash)
    if force_full_reseed:
        forget_entries: list[dict[str, str]] = []
    else:
        forget_entries = []
        old = old_data_ids_by_path or {}
        for rel in _normalize_scoped_paths((changed_paths or []) + (deleted_paths or [])):
            record = old.get(rel) or {}
            for entry in record.get("data_ids", []) or []:
                if isinstance(entry, dict) and entry.get("dataset_name") and entry.get("data_id"):
                    forget_entries.append({"dataset_name": str(entry["dataset_name"]), "data_id": str(entry["data_id"])})
    files = [{"path": p} for p in _normalize_scoped_paths(selected_paths if force_full_reseed else changed_paths or [])]
    return seed_repo_scope_files(
        repo_root=repo_root,
        repo=repo,
        scope_hash=scope_hash,
        snapshot_key=snapshot_key,
        files=files,
        reset=force_full_reseed,
        forget_data_ids=forget_entries,
    )


def seed_repo_memory(repo_root: str, repo: str = "") -> str:
    """Index the current repository state into Cognee and return the active snapshot key."""
    if not COGNEE_ENABLED:
        return ""

    return _run(_seed_repo_memory_async(repo_root, repo))


async def _seed_repo_memory_async(repo_root: str, repo: str = "") -> str:
    global _CURRENT_REPO_CODE_DATASET, _CURRENT_REPO_RULES_DATASET, _CURRENT_REPO_SNAPSHOT_KEY

    root = Path(repo_root).resolve()
    snapshot_key = _snapshot_key_for_repo(str(root), repo)

    use_snapshot_dataset = _snapshot_mode()
    code_dataset = _repo_code_dataset(repo, snapshot_key if use_snapshot_dataset else None)
    rules_dataset = _repo_rules_dataset(repo, snapshot_key if use_snapshot_dataset else None)

    repo_docs, rule_docs = _collect_repo_files(root)

    repo_payloads: list[str] = []
    for p in repo_docs:
        repo_payloads.extend(_build_repo_doc_payloads(root, p, snapshot_key))

    rule_payloads: list[str] = []
    for p in rule_docs:
        rule_payloads.extend(_build_rule_doc_payloads(root, p, snapshot_key))

    if not use_snapshot_dataset and RESET_REPO_DATASETS:
        await _forget_dataset_if_exists(code_dataset)
        await _forget_dataset_if_exists(rules_dataset)

    if repo_payloads:
        await _remember_async(repo_payloads, code_dataset, NODESET_REPO_DOCS)

    if rule_payloads:
        await _remember_async(rule_payloads, rules_dataset, NODESET_CODING_RULES)

    _CURRENT_REPO_SNAPSHOT_KEY = snapshot_key
    _CURRENT_REPO_CODE_DATASET = code_dataset
    _CURRENT_REPO_RULES_DATASET = rules_dataset
    return snapshot_key


async def _add_task_memory_async(
    issue_number: int,
    title: str,
    body: str,
    changed_files: list[str],
    notes: str,
    status: str,
    repo: str = "",
) -> None:
    dataset_name = _task_dataset(repo)

    payload = f"""KIND: historical_task
REPO: {repo}
ISSUE_NUMBER: {issue_number}
STATUS: {status}
TIMESTAMP: {time.time()}
TITLE:
{title or ""}

BODY:
{body or ""}

CHANGED_FILES:
{_format_bullets([str(p) for p in (changed_files or []) if p])}

NOTES:
{notes or ""}
""".strip()

    await _remember_async(payload, dataset_name, NODESET_TASK_HISTORY)


def add_task_memory(
    issue_number: int,
    title: str,
    body: str,
    changed_files: list[str],
    notes: str,
    status: str,
    repo: str = "",
) -> None:
    """Append sidecar GitHub/task history memory."""
    if not COGNEE_ENABLED:
        return

    _run(
        _add_task_memory_async(
            issue_number=issue_number,
            title=title,
            body=body,
            changed_files=changed_files,
            notes=notes,
            status=status,
            repo=repo,
        )
    )


def _build_search_queries(
    issue_title: str,
    issue_body: str,
    hinted_files: list[str],
    snapshot_key: str = "",
) -> list[tuple[str, str]]:
    snapshot_key = snapshot_key or _CURRENT_REPO_SNAPSHOT_KEY or "(unknown_snapshot)"

    queries: list[tuple[str, str]] = []

    queries.append(
        (
            "repo",
            f"""
Repo snapshot: {snapshot_key}

Issue title:
{issue_title}

Issue body:
{issue_body}

Find current-state repo files relevant to implementing this issue.
Prioritize:
- defining files
- imported modules
- callers/usages
- adjacent tests
- entrypoints and wiring
Only return content from the current repo snapshot.
""".strip(),
        )
    )

    queries.append(
        (
            "rules",
            f"""
Repo snapshot: {snapshot_key}

Issue title:
{issue_title}

Issue body:
{issue_body}

Find current coding rules, agent instructions, contributing guidance, and implementation conventions
that apply to this issue.
Only return content from the current repo snapshot.
""".strip(),
        )
    )

    queries.append(
        (
            "history",
            f"""
Issue title:
{issue_title}

Issue body:
{issue_body}

Find similar prior tasks, previous changed files, fixes, and implementation patterns that may help.
Use historical task memory only.
""".strip(),
        )
    )

    for path in hinted_files[:MAX_HINT_FILES]:
        queries.append(
            (
                "repo",
                f"""
Repo snapshot: {snapshot_key}

Target file:
{path}

Issue title:
{issue_title}

Issue body:
{issue_body}

Find current-state context for this file:
- the file itself
- files it imports
- files that import or call it
- sibling modules
- relevant tests
Only return content from the current repo snapshot.
""".strip(),
            )
        )

    for term in _extract_issue_terms(issue_title, issue_body, hinted_files)[:MAX_SYMBOL_TERMS]:
        queries.append(
            (
                "repo",
                f"""
Repo snapshot: {snapshot_key}

Symbol or concept:
{term}

Issue title:
{issue_title}

Issue body:
{issue_body}

Find current-state code and docs related to:
- definitions of {term}
- imports of {term}
- callers/usages of {term}
- tests covering {term}
Only return content from the current repo snapshot.
""".strip(),
            )
        )

    return queries[:MAX_SEARCH_QUERIES]


def _filter_current_snapshot(texts: list[str], snapshot_key: str = "") -> list[str]:
    active_snapshot_key = snapshot_key or _CURRENT_REPO_SNAPSHOT_KEY
    if not active_snapshot_key:
        return texts

    out: list[str] = []
    for text in texts:
        # If the chunk carries a SNAPSHOT header, enforce it. If it does not, keep it;
        # this preserves compatibility with older Cognee chunking/result shapes.
        if "SNAPSHOT:" in text and active_snapshot_key not in text:
            continue
        out.append(text)
    return out


def _search_kind_dataset(
    kind: str,
    repo: str,
    code_dataset_name: str = "",
    rules_dataset_name: str = "",
) -> str:
    if kind == "rules":
        return rules_dataset_name or _CURRENT_REPO_RULES_DATASET or _repo_rules_dataset(repo)
    if kind == "history":
        return _task_dataset(repo)
    return code_dataset_name or _CURRENT_REPO_CODE_DATASET or _repo_code_dataset(repo)


def _search_once(query_text: str, dataset_name: str, query_type: Any, top_k: int) -> list[str]:
    results = _run(_search_async(query_text, dataset_name=dataset_name, query_type=query_type, top_k=top_k))
    return _flatten_search_results(results)


def _maybe_lexical_search(query_text: str, dataset_name: str, top_k: int) -> list[str]:
    if not USE_LEXICAL_SEARCH:
        return []
    lexical_type = getattr(SearchType, "CHUNKS_LEXICAL", None)
    if lexical_type is None:
        return []
    try:
        return _search_once(query_text, dataset_name, lexical_type, max(3, top_k // 2))
    except Exception as e:
        print(f"[WARN] Cognee lexical search failed for {dataset_name}: {e}", flush=True)
        return []


def _trim_chunks(chunks: list[str], max_chunks: int, budget: int) -> list[str]:
    out: list[str] = []
    used = 0

    for chunk in chunks[:max_chunks]:
        text = chunk.strip()
        if not text:
            continue
        if len(text) > MAX_CHUNK_CHARS:
            text = text[:MAX_CHUNK_CHARS] + "\n...[truncated]"
        if used + len(text) + 12 > budget:
            remaining = budget - used - 12
            if remaining > 500:
                out.append(text[:remaining] + "\n...[truncated]")
            break
        out.append(text)
        used += len(text) + 12

    return out


def _section(title: str, chunks: list[str], max_chunks: int, budget: int) -> str:
    trimmed = _trim_chunks(_unique_keep_order(chunks), max_chunks, budget)
    if not trimmed:
        return ""
    return f"## {title}\n\n" + "\n\n---\n\n".join(trimmed)


def search_issue_context(
    issue_title: str,
    issue_body: str,
    hinted_files: list[str] | None = None,
    repo: str = "",
    dataset_name: str = "",
    code_dataset_name: str = "",
    rules_dataset_name: str = "",
    scope_hash: str = "",
    snapshot_key: str = "",
) -> str:
    """Return a structured context bundle for both planner and writer."""
    if not COGNEE_ENABLED:
        return ""

    hinted = hinted_files or []
    code_dataset_name = code_dataset_name or dataset_name
    queries = _build_search_queries(issue_title, issue_body, hinted, snapshot_key=snapshot_key)

    repo_chunks: list[str] = []
    rule_chunks: list[str] = []
    history_chunks: list[str] = []

    for kind, query_text in queries:
        dataset_name = _search_kind_dataset(
            kind,
            repo,
            code_dataset_name=code_dataset_name,
            rules_dataset_name=rules_dataset_name,
        )
        try:
            texts = _search_once(query_text, dataset_name, SearchType.CHUNKS, SEARCH_TOP_K)
            if kind in {"repo", "rules"}:
                texts.extend(_maybe_lexical_search(query_text, dataset_name, SEARCH_TOP_K))
                texts = _filter_current_snapshot(texts, snapshot_key=snapshot_key)
        except Exception as e:
            print(f"[WARN] Cognee search failed for {kind} dataset {dataset_name}: {e}", flush=True)
            continue

        if kind == "repo":
            repo_chunks.extend(texts)
        elif kind == "rules":
            rule_chunks.extend(texts)
        elif kind == "history":
            history_chunks.extend(texts)

    repo_chunks = _unique_keep_order(repo_chunks)
    rule_chunks = _unique_keep_order(rule_chunks)
    history_chunks = _unique_keep_order(history_chunks)

    repo_budget = max(8000, int(MAX_CONTEXT_CHARS * 0.72))
    rule_budget = max(3000, int(MAX_CONTEXT_CHARS * 0.14))
    history_budget = max(3000, MAX_CONTEXT_CHARS - repo_budget - rule_budget)

    parts: list[str] = []
    repo_section = _section("Current repo context", repo_chunks, MAX_CONTEXT_CHUNKS, repo_budget)
    rule_section = _section("Current coding rules", rule_chunks, MAX_RULE_CHUNKS, rule_budget)
    history_section = _section("Historical task sidecar", history_chunks, MAX_HISTORY_CHUNKS, history_budget)

    if repo_section:
        parts.append(repo_section)
    if rule_section:
        parts.append(rule_section)
    if history_section:
        parts.append(history_section)

    result = "\n\n====================\n\n".join(parts)
    if len(result) > MAX_CONTEXT_CHARS:
        result = result[:MAX_CONTEXT_CHARS] + "\n...[Cognee context truncated]"
    return result


def shutdown_cognee_runner() -> None:
    _RUNNER.close()


def cognee_status() -> dict[str, Any]:
    return {
        "enabled": COGNEE_ENABLED,
        "required": COGNEE_REQUIRED,
        "llm_provider": os.environ.get("LLM_PROVIDER"),
        "llm_model": os.environ.get("LLM_MODEL"),
        "embedding_provider": os.environ.get("EMBEDDING_PROVIDER"),
        "embedding_model": os.environ.get("EMBEDDING_MODEL"),
        "embedding_dimensions": os.environ.get("EMBEDDING_DIMENSIONS"),
        "repo_dataset_mode": REPO_DATASET_MODE,
        "current_snapshot": _CURRENT_REPO_SNAPSHOT_KEY,
        "current_code_dataset": _CURRENT_REPO_CODE_DATASET,
        "current_rules_dataset": _CURRENT_REPO_RULES_DATASET,
    }