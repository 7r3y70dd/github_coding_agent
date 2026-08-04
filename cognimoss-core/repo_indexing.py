from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

DENY_PREFIXES = tuple(
    p.strip().strip("/") + "/"
    for p in os.environ.get("DENY_PREFIXES", "agent_runner/,agent/,scripts/,systemd/,.github/").split(",")
    if p.strip()
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

DEFAULT_IGNORE_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "dist",
    "build",
    "coverage",
    ".next",
    ".nuxt",
    ".turbo",
    ".cache",
    ".pytest_cache",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "vendor",
}

IGNORE_DIRS = set(
    d.strip()
    for d in os.environ.get("COGNEE_INDEX_IGNORE_DIRS", ",".join(sorted(DEFAULT_IGNORE_DIRS))).split(",")
    if d.strip()
)

DEFAULT_ALLOWED_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".kts",
    ".cs",
    ".cpp",
    ".cc",
    ".c",
    ".h",
    ".hpp",
    ".rb",
    ".php",
    ".swift",
    ".scala",
    ".sh",
    ".bash",
    ".zsh",
    ".ps1",
    ".sql",
    ".html",
    ".css",
    ".scss",
    ".sass",
    ".less",
    ".vue",
    ".svelte",
    ".md",
    ".mdx",
    ".txt",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".xml",
    ".proto",
    ".graphql",
    ".gql",
}

ALLOWED_EXTENSIONS = set(
    e.strip().lower()
    for e in os.environ.get("COGNEE_INDEX_ALLOWED_EXTENSIONS", ",".join(sorted(DEFAULT_ALLOWED_EXTENSIONS))).split(",")
    if e.strip()
)

ALLOWED_BASENAMES = {
    "Dockerfile",
    "Makefile",
    "Procfile",
    "README",
    "LICENSE",
    "CHANGELOG",
}

MAX_INDEX_FILE_BYTES = int(os.environ.get("COGNEE_INDEX_MAX_FILE_BYTES", "400000"))


def normalize_repo(repo: str) -> str:
    repo = (repo or "").strip()
    if not REPO_RE.match(repo):
        raise ValueError("Repository must be in owner/repo format.")
    return repo


def normalize_ref(ref: str | None) -> str:
    ref = (ref or "main").strip()
    if not ref:
        return "main"
    if not re.match(r"^[A-Za-z0-9_./-]+$", ref):
        raise ValueError("Invalid git ref.")
    if ".." in ref or ref.startswith("/") or ref.endswith("/"):
        raise ValueError("Invalid git ref.")
    return ref


def normalize_relpath(rel: str) -> str:
    rel = (rel or "").replace("\\", "/").strip().lstrip("./")
    while "//" in rel:
        rel = rel.replace("//", "/")
    if rel == ".":
        return ""
    return rel


def is_safe_relpath(rel: str) -> bool:
    rel = normalize_relpath(rel)
    if rel == "":
        return True
    if rel.startswith("/") or ".." in rel.split("/"):
        return False
    if any(rel.startswith(p) for p in DENY_PREFIXES):
        return False
    name = os.path.basename(rel.rstrip("/"))
    if name in DENY_FILENAMES:
        return False
    if name.endswith((".pem", ".key", ".p12", ".pfx")):
        return False
    return True


def normalize_scope_paths(paths: Iterable[str] | None) -> list[str]:
    cleaned: list[str] = []
    for raw in paths or []:
        raw_s = str(raw)
        p = normalize_relpath(raw_s)
        if p in {"", "/", "."}:
            p = ""
        if not is_safe_relpath(p):
            continue
        if p and raw_s.endswith("/") and not p.endswith("/"):
            p += "/"
        if p not in cleaned:
            cleaned.append(p)
    return cleaned


def scope_hash(repo: str, ref: str, include_paths: Iterable[str], exclude_paths: Iterable[str]) -> str:
    payload = {
        "repo": normalize_repo(repo),
        "ref": normalize_ref(ref),
        "include_paths": sorted(normalize_scope_paths(include_paths)),
        "exclude_paths": sorted(normalize_scope_paths(exclude_paths)),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


COGNEE_REPO_DATASET = os.environ.get("COGNEE_REPO_DATASET", "repo_memory")


def _safe_dataset_part(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9_.-]+", "_", value)
    value = value.strip("_")
    return value[:120] or "default"


def _base_repo_dataset_part(repo: str) -> str:
    return _safe_dataset_part(normalize_repo(repo))


def scope_snapshot_key(repo: str, ref: str, scope: str) -> str:
    # Stable per repo/ref/scope; intentionally does not include head SHA so unchanged files remain searchable.
    return f"repo_scope::{normalize_repo(repo)}::{normalize_ref(ref)}::{_safe_dataset_part(scope)}"


def code_dataset_name_for_scope(repo: str, scope: str) -> str:
    return f"{COGNEE_REPO_DATASET}__{_base_repo_dataset_part(repo)}__code__scope_{_safe_dataset_part(scope)}"


def rules_dataset_name_for_scope(repo: str, scope: str) -> str:
    return f"{COGNEE_REPO_DATASET}__{_base_repo_dataset_part(repo)}__rules__scope_{_safe_dataset_part(scope)}"


def dataset_names_for_scope(repo: str, ref_or_scope: str, scope: str | None = None) -> dict[str, str]:
    # Accept both old call style dataset_names_for_scope(repo, scope) and new style (repo, ref, scope).
    if scope is None:
        ref = "main"
        scope_value = ref_or_scope
    else:
        ref = ref_or_scope or "main"
        scope_value = scope
    code = code_dataset_name_for_scope(repo, scope_value)
    rules = rules_dataset_name_for_scope(repo, scope_value)
    return {
        "snapshot_key": scope_snapshot_key(repo, ref, scope_value),
        "dataset_name": code,
        "code_dataset_name": code,
        "rules_dataset_name": rules,
    }


def dataset_name_for_scope(repo: str, scope: str) -> str:
    return dataset_names_for_scope(repo, scope)["dataset_name"]


def index_pk(repo: str) -> str:
    return f"COGNEE_INDEX#{normalize_repo(repo)}"


def index_sk(scope: str) -> str:
    return f"SCOPE#{scope}"


def file_pk(repo: str, scope: str) -> str:
    return f"COGNEE_INDEX_FILE#{normalize_repo(repo)}#{scope}"


def file_sk(path: str) -> str:
    return "PATH#" + normalize_relpath(path)


def file_data_id(repo: str, scope: str, relpath: str) -> str:
    # Deterministic UUID-compatible identifier based on repo/scope/path.
    import uuid

    basis = f"{normalize_repo(repo)}:{scope}:{normalize_relpath(relpath)}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, basis))


def relpath_in_scope(path: str, include_paths: Iterable[str], exclude_paths: Iterable[str]) -> bool:
    path = normalize_relpath(path)
    if not path or not is_safe_relpath(path):
        return False

    includes = normalize_scope_paths(include_paths)
    excludes = normalize_scope_paths(exclude_paths)

    def matches(pattern: str) -> bool:
        pattern = normalize_relpath(pattern)
        if pattern == "":
            return True
        if pattern.endswith("/"):
            return path.startswith(pattern)
        return path == pattern

    if includes and not any(matches(p) for p in includes):
        return False
    if excludes and any(matches(p) for p in excludes):
        return False
    return True


def is_probably_text_file(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            sample = f.read(4096)
        return b"\x00" not in sample
    except Exception:
        return False


def is_indexable_file(path: str, rel: str) -> bool:
    rel = normalize_relpath(rel)
    if not is_safe_relpath(rel):
        return False
    name = os.path.basename(rel)
    suffix = Path(rel).suffix.lower()
    if name not in ALLOWED_BASENAMES and suffix not in ALLOWED_EXTENSIONS:
        return False
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    if size <= 0 or size > MAX_INDEX_FILE_BYTES:
        return False
    return is_probably_text_file(path)


def selected_files(repo_path: str, include_paths: Iterable[str], exclude_paths: Iterable[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    repo_path = os.path.abspath(repo_path)
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
        for name in files:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, repo_path).replace("\\", "/")
            if not relpath_in_scope(rel, include_paths, exclude_paths):
                continue
            if not is_indexable_file(full, rel):
                continue
            digest = sha256_file(full)
            out.append(
                {
                    "path": rel,
                    "full_path": full,
                    "sha256": digest,
                    "size": os.path.getsize(full),
                }
            )
    out.sort(key=lambda x: x["path"])
    return out


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def decimal_now_ms() -> Decimal:
    import time

    return Decimal(int(time.time() * 1000))


def to_dynamo_value(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, dict):
        return {k: to_dynamo_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [to_dynamo_value(v) for v in value]
    return value


def run(cmd: list[str], cwd: str | None = None, timeout: int = 900) -> str:
    try:
        res = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"Command timed out after {timeout}s: {' '.join(cmd)}") from e

    stdout = (res.stdout or "").strip()
    stderr = (res.stderr or "").strip()
    if res.returncode != 0:
        raise RuntimeError(
            f"Command failed ({res.returncode}): {' '.join(cmd)}\nSTDERR:\n{stderr}\nSTDOUT:\n{stdout}"
        )
    return stdout
