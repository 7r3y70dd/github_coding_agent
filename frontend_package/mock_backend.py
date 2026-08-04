from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from repo_indexing import dataset_names_for_scope, scope_hash as make_scope_hash

DB_PATH = os.environ.get("MOCK_DB_PATH", "/tmp/cognimoss-mock-backend.sqlite3")
SEED_DEMO_DATA = os.environ.get("MOCK_SEED_DEMO_DATA", "1") == "1"
RUN_STEP_SECONDS = max(1, int(os.environ.get("MOCK_RUN_STEP_SECONDS", "4")))
INDEX_STEP_SECONDS = max(1, int(os.environ.get("MOCK_INDEX_STEP_SECONDS", "3")))
DEBUG_STEP_SECONDS = max(1, int(os.environ.get("MOCK_DEBUG_STEP_SECONDS", "4")))

_LOCK = threading.RLock()


def now_ms() -> int:
    return int(time.time() * 1000)


def _connect() -> sqlite3.Connection:
    path = Path(DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def init_db() -> None:
    with _LOCK, _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                repo TEXT NOT NULL,
                issue_number INTEGER NOT NULL,
                title TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                cognee_scope_hash TEXT NOT NULL DEFAULT '',
                cognee_snapshot_key TEXT NOT NULL DEFAULT '',
                cognee_dataset_name TEXT NOT NULL DEFAULT '',
                cognee_code_dataset_name TEXT NOT NULL DEFAULT '',
                cognee_rules_dataset_name TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS indexes (
                repo TEXT NOT NULL,
                scope_hash TEXT NOT NULL,
                ref TEXT NOT NULL,
                snapshot_key TEXT NOT NULL,
                dataset_name TEXT NOT NULL,
                code_dataset_name TEXT NOT NULL,
                rules_dataset_name TEXT NOT NULL,
                status TEXT NOT NULL,
                status_message TEXT NOT NULL,
                requested_mode TEXT NOT NULL,
                effective_mode TEXT NOT NULL,
                include_paths TEXT NOT NULL,
                exclude_paths TEXT NOT NULL,
                selected_file_count INTEGER NOT NULL DEFAULT 0,
                changed_count INTEGER NOT NULL DEFAULT 0,
                removed_count INTEGER NOT NULL DEFAULT 0,
                unchanged_count INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_indexed_at INTEGER NOT NULL DEFAULT 0,
                last_job_id TEXT NOT NULL,
                PRIMARY KEY (repo, scope_hash)
            );

            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                repo TEXT NOT NULL,
                ref TEXT NOT NULL,
                scope_hash TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                payload TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS debug_runs (
                debug_run_id TEXT PRIMARY KEY,
                repo TEXT NOT NULL,
                ref TEXT NOT NULL,
                cognee_scope_hash TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                summary TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_type TEXT NOT NULL,
                parent_id TEXT NOT NULL,
                ts INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                message TEXT NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE(parent_type, parent_id, event_type)
            );

            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_email TEXT NOT NULL,
                repo TEXT NOT NULL,
                scope_hash TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )
        conn.commit()

    if SEED_DEMO_DATA:
        seed_demo_data()


def _add_event(
    conn: sqlite3.Connection,
    parent_type: str,
    parent_id: str,
    event_type: str,
    message: str,
    payload: dict[str, Any] | None = None,
    ts: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO events(parent_type, parent_id, ts, event_type, message, payload)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (parent_type, parent_id, ts or now_ms(), event_type, message, _json(payload or {})),
    )


def seed_demo_data() -> None:
    with _LOCK, _connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
        if count:
            return

        now = now_ms()
        demo_scope = "demo-ready-scope"
        datasets = dataset_names_for_scope("7r3y70dd/stock_options_project", "main", demo_scope)
        conn.execute(
            """
            INSERT INTO indexes(
                repo, scope_hash, ref, snapshot_key, dataset_name, code_dataset_name,
                rules_dataset_name, status, status_message, requested_mode, effective_mode,
                include_paths, exclude_paths, selected_file_count, changed_count,
                removed_count, unchanged_count, created_at, updated_at, last_indexed_at, last_job_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "7r3y70dd/stock_options_project",
                demo_scope,
                "main",
                datasets["snapshot_key"],
                datasets["dataset_name"],
                datasets["code_dataset_name"],
                datasets["rules_dataset_name"],
                "ready",
                "Mock repository context is ready.",
                "full",
                "full",
                _json(["app/", "services/", "tests/"]),
                _json([]),
                126,
                126,
                0,
                0,
                now - 120_000,
                now - 110_000,
                now - 110_000,
                "demo-index-job",
            ),
        )

        rows = [
            (
                "demo-completed-run",
                "completed",
                "7r3y70dd/stock_options_project",
                171,
                "Fix database configuration import error",
                "demo@cognimoss.local",
                now - 180_000,
                now - 150_000,
                demo_scope,
                datasets["snapshot_key"],
                datasets["dataset_name"],
                datasets["code_dataset_name"],
                datasets["rules_dataset_name"],
            ),
            (
                "demo-running-run",
                "running",
                "7r3y70dd/stock_options_project",
                172,
                "Repair strategy test fixtures",
                "demo@cognimoss.local",
                now - 8_000,
                now - 2_000,
                demo_scope,
                datasets["snapshot_key"],
                datasets["dataset_name"],
                datasets["code_dataset_name"],
                datasets["rules_dataset_name"],
            ),
        ]
        conn.executemany(
            """
            INSERT INTO runs(
                run_id, status, repo, issue_number, title, created_by, created_at, updated_at,
                cognee_scope_hash, cognee_snapshot_key, cognee_dataset_name,
                cognee_code_dataset_name, cognee_rules_dataset_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

        _add_event(conn, "run", "demo-completed-run", "queued", "Issue was added to the mock queue.")
        _add_event(conn, "run", "demo-completed-run", "planning", "Mock agent created an implementation plan.")
        _add_event(conn, "run", "demo-completed-run", "running", "Mock agent applied changes and ran tests.")
        _add_event(
            conn,
            "run",
            "demo-completed-run",
            "completed",
            "Mock run completed and produced a simulated pull request.",
            {"pull_request_url": "https://github.com/example/example/pull/42", "tests_passed": 37},
        )
        _add_event(conn, "run", "demo-running-run", "queued", "Issue was added to the mock queue.")
        _add_event(conn, "run", "demo-running-run", "planning", "Mock agent is reviewing repository context.")
        _add_event(conn, "run", "demo-running-run", "running", "Mock agent is running the affected tests.")
        conn.commit()


def _run_status(created_at: int) -> tuple[str, list[tuple[str, str, dict[str, Any]]]]:
    age = max(0.0, (now_ms() - created_at) / 1000)
    if age < RUN_STEP_SECONDS:
        return "queued", [("queued", "Issue was added to the mock queue.", {})]
    if age < RUN_STEP_SECONDS * 2:
        return "planning", [
            ("queued", "Issue was added to the mock queue.", {}),
            ("planning", "Mock agent is reading issue and repository context.", {}),
        ]
    if age < RUN_STEP_SECONDS * 3:
        return "running", [
            ("queued", "Issue was added to the mock queue.", {}),
            ("planning", "Mock agent created an implementation plan.", {}),
            ("running", "Mock agent is applying changes and running tests.", {"progress": 65}),
        ]
    return "completed", [
        ("queued", "Issue was added to the mock queue.", {}),
        ("planning", "Mock agent created an implementation plan.", {}),
        ("running", "Mock agent applied changes and ran tests.", {"progress": 100}),
        (
            "completed",
            "Mock run completed and produced a simulated pull request.",
            {"pull_request_url": "https://github.com/example/example/pull/42", "tests_passed": 24},
        ),
    ]


def _refresh_run(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    if row["run_id"].startswith("demo-"):
        return row["status"]
    status, event_specs = _run_status(int(row["created_at"]))
    if status != row["status"]:
        conn.execute(
            "UPDATE runs SET status=?, updated_at=? WHERE run_id=?",
            (status, now_ms(), row["run_id"]),
        )
    for event_type, message, payload in event_specs:
        _add_event(conn, "run", row["run_id"], event_type, message, payload)
    return status


def create_run(
    *,
    repo: str,
    issue_number: int,
    title: str,
    created_by: str,
    selected_index: dict[str, Any] | None,
) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    ts = now_ms()
    selected_index = selected_index or {}
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO runs(
                run_id, status, repo, issue_number, title, created_by, created_at, updated_at,
                cognee_scope_hash, cognee_snapshot_key, cognee_dataset_name,
                cognee_code_dataset_name, cognee_rules_dataset_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                "queued",
                repo,
                issue_number,
                title,
                created_by,
                ts,
                ts,
                selected_index.get("scope_hash", ""),
                selected_index.get("snapshot_key", ""),
                selected_index.get("dataset_name", ""),
                selected_index.get("code_dataset_name", selected_index.get("dataset_name", "")),
                selected_index.get("rules_dataset_name", ""),
            ),
        )
        _add_event(
            conn,
            "run",
            run_id,
            "queued",
            "Issue was added to the mock queue.",
            {"repo": repo, "issue_number": issue_number, "title": title, "mock": True},
        )
        conn.commit()
    return {
        "run_id": run_id,
        "status": "queued",
        "cognee_scope_hash": selected_index.get("scope_hash", ""),
        "cognee_snapshot_key": selected_index.get("snapshot_key", ""),
        "cognee_dataset_name": selected_index.get("dataset_name", ""),
        "cognee_code_dataset_name": selected_index.get("code_dataset_name", selected_index.get("dataset_name", "")),
        "cognee_rules_dataset_name": selected_index.get("rules_dataset_name", ""),
        "mock": True,
    }


def list_runs(status: str | None = None) -> dict[str, Any]:
    with _LOCK, _connect() as conn:
        rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 50").fetchall()
        cleaned: list[dict[str, Any]] = []
        for row in rows:
            actual = _refresh_run(conn, row)
            if status and actual != status:
                continue
            cleaned.append(
                {
                    "run_id": row["run_id"],
                    "status": actual,
                    "repo": row["repo"],
                    "issue_number": int(row["issue_number"]),
                    "title": row["title"],
                    "created_by": row["created_by"],
                    "created_at": int(row["created_at"]),
                    "updated_at": int(row["updated_at"]),
                    "cognee_scope_hash": row["cognee_scope_hash"],
                    "cognee_dataset_name": row["cognee_dataset_name"],
                    "mock": True,
                }
            )
        conn.commit()
        return {"runs": cleaned, "mock": True}


def get_run_events(run_id: str) -> dict[str, Any]:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row:
            _refresh_run(conn, row)
        events = conn.execute(
            "SELECT * FROM events WHERE parent_type='run' AND parent_id=? ORDER BY ts, id",
            (run_id,),
        ).fetchall()
        conn.commit()
    return {
        "run_id": run_id,
        "events": [
            {
                "ts": int(e["ts"]),
                "event_type": e["event_type"],
                "message": e["message"],
                "payload": _loads(e["payload"], {}),
            }
            for e in events
        ],
        "mock": True,
    }


def sample_repo_tree(repo: str, ref: str) -> dict[str, Any]:
    items = [
        {"path": ".github", "type": "tree", "size": 0},
        {"path": ".github/workflows", "type": "tree", "size": 0},
        {"path": ".github/workflows/tests.yml", "type": "blob", "size": 1294},
        {"path": "app", "type": "tree", "size": 0},
        {"path": "app/api", "type": "tree", "size": 0},
        {"path": "app/api/dashboard.py", "type": "blob", "size": 7420},
        {"path": "app/api/health.py", "type": "blob", "size": 632},
        {"path": "app/core", "type": "tree", "size": 0},
        {"path": "app/core/config.py", "type": "blob", "size": 4260},
        {"path": "app/core/database.py", "type": "blob", "size": 3115},
        {"path": "app/core/main.py", "type": "blob", "size": 2380},
        {"path": "app/models", "type": "tree", "size": 0},
        {"path": "app/models/database.py", "type": "blob", "size": 5690},
        {"path": "app/strategies", "type": "tree", "size": 0},
        {"path": "app/strategies/cash_secured_put.py", "type": "blob", "size": 8890},
        {"path": "app/strategies/covered_call.py", "type": "blob", "size": 7632},
        {"path": "services", "type": "tree", "size": 0},
        {"path": "services/options_service.py", "type": "blob", "size": 13842},
        {"path": "tests", "type": "tree", "size": 0},
        {"path": "tests/test_database.py", "type": "blob", "size": 4682},
        {"path": "tests/test_options_service.py", "type": "blob", "size": 8960},
        {"path": "tests/test_strategies.py", "type": "blob", "size": 10240},
        {"path": "Dockerfile", "type": "blob", "size": 844},
        {"path": "Procfile", "type": "blob", "size": 48},
        {"path": "README.md", "type": "blob", "size": 6320},
        {"path": "requirements.txt", "type": "blob", "size": 1740},
    ]
    return {
        "repo": repo,
        "ref": ref,
        "truncated": False,
        "total_items": len(items),
        "items": items,
        "source": "mock",
        "mock": True,
    }


def _estimated_file_count(include_paths: list[str]) -> int:
    count = 0
    for path in include_paths:
        if not path:
            count += 126
        elif path.endswith("/"):
            count += 18
        else:
            count += 1
    return max(1, min(count, 500))


def create_cognee_reseed(
    *,
    repo: str,
    ref: str,
    mode: str,
    include_paths: list[str],
    exclude_paths: list[str],
    created_by: str,
) -> dict[str, Any]:
    scope = make_scope_hash(repo, ref, include_paths, exclude_paths)
    datasets = dataset_names_for_scope(repo, ref, scope)
    job_id = str(uuid.uuid4())
    ts = now_ms()
    selected = _estimated_file_count(include_paths)
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO indexes(
                repo, scope_hash, ref, snapshot_key, dataset_name, code_dataset_name,
                rules_dataset_name, status, status_message, requested_mode, effective_mode,
                include_paths, exclude_paths, selected_file_count, changed_count,
                removed_count, unchanged_count, created_at, updated_at, last_indexed_at, last_job_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(repo, scope_hash) DO UPDATE SET
                status=excluded.status,
                status_message=excluded.status_message,
                requested_mode=excluded.requested_mode,
                effective_mode=excluded.effective_mode,
                include_paths=excluded.include_paths,
                exclude_paths=excluded.exclude_paths,
                selected_file_count=excluded.selected_file_count,
                changed_count=0,
                removed_count=0,
                unchanged_count=0,
                updated_at=excluded.updated_at,
                last_indexed_at=0,
                last_job_id=excluded.last_job_id
            """,
            (
                repo,
                scope,
                ref,
                datasets["snapshot_key"],
                datasets["dataset_name"],
                datasets["code_dataset_name"],
                datasets["rules_dataset_name"],
                "queued",
                "Mock Cognee reseed job queued.",
                mode,
                "full" if mode in {"auto", "full"} else "incremental",
                _json(include_paths),
                _json(exclude_paths),
                selected,
                0,
                0,
                0,
                ts,
                ts,
                0,
                job_id,
            ),
        )
        conn.execute(
            """
            INSERT INTO jobs(job_id, kind, repo, ref, scope_hash, status, created_at, updated_at, payload)
            VALUES (?, 'cognee', ?, ?, ?, 'queued', ?, ?, ?)
            """,
            (job_id, repo, ref, scope, ts, ts, _json({"mode": mode, "created_by": created_by})),
        )
        _add_event(
            conn,
            "cognee",
            job_id,
            "queued",
            "Mock Cognee reseed job queued.",
            {"repo": repo, "ref": ref, "scope_hash": scope, "mode": mode},
        )
        conn.commit()
    return {
        "index_job_id": job_id,
        "repo": repo,
        "ref": ref,
        "scope_hash": scope,
        "snapshot_key": datasets["snapshot_key"],
        "dataset_name": datasets["dataset_name"],
        "code_dataset_name": datasets["code_dataset_name"],
        "rules_dataset_name": datasets["rules_dataset_name"],
        "status": "queued",
        "mock": True,
    }


def _refresh_index(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    if row["scope_hash"] == "demo-ready-scope" or row["status"] == "ready":
        return "ready"

    age = max(0.0, (now_ms() - int(row["updated_at"])) / 1000)
    job_id = row["last_job_id"]
    current = str(row["status"])

    if current == "queued" and age < INDEX_STEP_SECONDS:
        status = "queued"
        message = "Mock Cognee reseed job queued."
        specs = [("queued", message, {})]
    elif current == "queued":
        status = "indexing"
        message = "Mock indexer is hashing and embedding selected files."
        specs = [
            ("queued", "Mock Cognee reseed job queued.", {}),
            ("indexing", message, {"progress": 55}),
        ]
    elif current == "indexing" and age < INDEX_STEP_SECONDS:
        status = "indexing"
        message = "Mock indexer is hashing and embedding selected files."
        specs = [
            ("queued", "Mock Cognee reseed job queued.", {}),
            ("indexing", message, {"progress": 55}),
        ]
    else:
        status = "ready"
        message = "Mock repository context is ready."
        specs = [
            ("queued", "Mock Cognee reseed job queued.", {}),
            ("indexing", "Mock indexer hashed and embedded selected files.", {"progress": 100}),
            ("ready", message, {"selected_file_count": int(row["selected_file_count"])}),
        ]

    if status != current:
        changed = int(row["selected_file_count"]) if status == "ready" else int(row["changed_count"])
        indexed_at = now_ms() if status == "ready" else int(row["last_indexed_at"])
        conn.execute(
            """
            UPDATE indexes SET status=?, status_message=?, changed_count=?,
                updated_at=?, last_indexed_at=? WHERE repo=? AND scope_hash=?
            """,
            (status, message, changed, now_ms(), indexed_at, row["repo"], row["scope_hash"]),
        )
        conn.execute("UPDATE jobs SET status=?, updated_at=? WHERE job_id=?", (status, now_ms(), job_id))

    for event_type, event_message, payload in specs:
        _add_event(conn, "cognee", job_id, event_type, event_message, payload)
    return status

def _index_dict(row: sqlite3.Row, status: str | None = None) -> dict[str, Any]:
    return {
        "repo": row["repo"],
        "ref": row["ref"],
        "scope_hash": row["scope_hash"],
        "snapshot_key": row["snapshot_key"],
        "dataset_name": row["dataset_name"],
        "code_dataset_name": row["code_dataset_name"],
        "rules_dataset_name": row["rules_dataset_name"],
        "data_ids_supported": True,
        "status": status or row["status"],
        "status_message": row["status_message"],
        "include_paths": _loads(row["include_paths"], []),
        "exclude_paths": _loads(row["exclude_paths"], []),
        "head_sha": "mocked1234567890abcdef",
        "effective_mode": row["effective_mode"],
        "selected_file_count": int(row["selected_file_count"]),
        "changed_count": int(row["changed_count"]),
        "removed_count": int(row["removed_count"]),
        "unchanged_count": int(row["unchanged_count"]),
        "created_at": int(row["created_at"]),
        "updated_at": int(row["updated_at"]),
        "last_indexed_at": int(row["last_indexed_at"]),
        "last_job_id": row["last_job_id"],
        "mock": True,
    }


def list_cognee_indexes(repo: str) -> dict[str, Any]:
    with _LOCK, _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM indexes WHERE repo=? ORDER BY updated_at DESC",
            (repo,),
        ).fetchall()
        items = []
        for row in rows:
            status = _refresh_index(conn, row)
            refreshed = conn.execute(
                "SELECT * FROM indexes WHERE repo=? AND scope_hash=?",
                (repo, row["scope_hash"]),
            ).fetchone()
            items.append(_index_dict(refreshed, status))
        conn.commit()
    return {"repo": repo, "indexes": items, "mock": True}


def latest_ready_index(repo: str, requested_scope_hash: str = "") -> dict[str, Any] | None:
    list_cognee_indexes(repo)
    with _LOCK, _connect() as conn:
        if requested_scope_hash:
            row = conn.execute(
                "SELECT * FROM indexes WHERE repo=? AND scope_hash=? AND status='ready'",
                (repo, requested_scope_hash),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM indexes WHERE repo=? AND status='ready'
                ORDER BY CASE WHEN last_indexed_at > 0 THEN last_indexed_at ELSE updated_at END DESC LIMIT 1
                """,
                (repo,),
            ).fetchone()
    return _index_dict(row) if row else None


def get_cognee_job_events(job_id: str) -> dict[str, Any]:
    with _LOCK, _connect() as conn:
        job = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if job and job["kind"] == "cognee":
            row = conn.execute(
                "SELECT * FROM indexes WHERE repo=? AND scope_hash=?",
                (job["repo"], job["scope_hash"]),
            ).fetchone()
            if row:
                _refresh_index(conn, row)
        events = conn.execute(
            "SELECT * FROM events WHERE parent_type='cognee' AND parent_id=? ORDER BY ts, id",
            (job_id,),
        ).fetchall()
        conn.commit()
    return {
        "job_id": job_id,
        "events": [
            {
                "ts": int(e["ts"]),
                "event_type": e["event_type"],
                "message": e["message"],
                "payload": _loads(e["payload"], {}),
            }
            for e in events
        ],
        "mock": True,
    }


def answer_repo_chat(
    *, user_email: str, repo: str, scope_hash: str, message: str, selected_index: dict[str, Any] | None
) -> dict[str, Any]:
    text = message.strip()
    lowered = text.lower()
    context_available = bool(selected_index)
    sources = [
        {"source_id": "S1", "path": "app/core/config.py"},
        {"source_id": "S2", "path": "services/options_service.py"},
        {"source_id": "S3", "path": "tests/test_options_service.py"},
    ]

    if any(word in lowered for word in ("architecture", "structure", "organized", "layout")):
        answer = (
            f"Mock analysis for {repo}: the project is organized around an application layer, shared core configuration, "
            "service modules, strategy implementations, and pytest suites. Start with app/core/config.py for runtime "
            "settings [S1], then follow calls into services/options_service.py [S2]."
        )
        cited = ["S1", "S2"]
    elif any(word in lowered for word in ("test", "pytest", "failure", "failing")):
        answer = (
            "The mock repository snapshot suggests checking whether test fixtures still match the service model. "
            "Compare the attributes consumed by services/options_service.py [S2] with the objects created in "
            "tests/test_options_service.py [S3]. Run `pytest -xvs tests/test_options_service.py` first."
        )
        cited = ["S2", "S3"]
    elif any(word in lowered for word in ("config", "environment", "env", "setting")):
        answer = (
            "Configuration should have one canonical object and one stable import path. Inspect app/core/config.py [S1], "
            "verify every referenced property exists, and avoid mixing dictionary-style `.get()` calls with attribute access."
        )
        cited = ["S1"]
    elif any(word in lowered for word in ("bug", "error", "fix", "crash")):
        answer = (
            "A practical mock debugging path is: reproduce one failing suite, inspect the first traceback, compare the "
            "service contract [S2] with its tests [S3], make the smallest compatible change, and rerun that suite before "
            "the full test set."
        )
        cited = ["S2", "S3"]
    else:
        answer = (
            f"This is a deterministic mock response for `{text}` in {repo}. In a live deployment, Cognimoss would answer "
            "from the selected repository snapshot. For frontend work, this response preserves the production JSON shape, "
            "source list, context flag, and model metadata."
        )
        cited = []

    ts = now_ms()
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO chats(user_email, repo, scope_hash, role, content, created_at) VALUES (?, ?, ?, 'user', ?, ?)",
            (user_email, repo, scope_hash, text, ts),
        )
        conn.execute(
            "INSERT INTO chats(user_email, repo, scope_hash, role, content, created_at) VALUES (?, ?, ?, 'assistant', ?, ?)",
            (user_email, repo, scope_hash, answer, ts + 1),
        )
        conn.commit()

    return {
        "repo": repo,
        "scope_hash": scope_hash,
        "answer": answer,
        "cited_source_ids": cited,
        "sources": [s for s in sources if not cited or s["source_id"] in cited],
        "context_available": context_available,
        "context_snapshot": {
            "source_count": 3 if context_available else 0,
            "snapshot_key": selected_index.get("snapshot_key", "") if selected_index else "",
            "mock": True,
        },
        "model": "mock-cognimoss-repo-chat-v1",
        "mock": True,
    }


def _refresh_debug(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    age = max(0.0, (now_ms() - int(row["created_at"])) / 1000)
    if age < DEBUG_STEP_SECONDS:
        status = "queued"
        summary = "Waiting for a mock debugger slot."
        specs = [("queued", "Mock debugger run queued.", {})]
    elif age < DEBUG_STEP_SECONDS * 2:
        status = "running"
        summary = "Mock debugger is collecting pytest suites."
        specs = [
            ("queued", "Mock debugger run queued.", {}),
            ("running", "Mock debugger is running pytest suites.", {"suites_discovered": 8}),
        ]
    else:
        status = "completed"
        summary = "Mock debugger found two failing suites and prepared simulated GitHub issues."
        specs = [
            ("queued", "Mock debugger run queued.", {}),
            ("running", "Mock debugger ran pytest suites.", {"suites_discovered": 8}),
            (
                "completed",
                summary,
                {"passing_suites": 6, "failing_suites": 2, "issues_created": 2},
            ),
        ]
    if status != row["status"]:
        conn.execute(
            "UPDATE debug_runs SET status=?, summary=?, updated_at=? WHERE debug_run_id=?",
            (status, summary, now_ms(), row["debug_run_id"]),
        )
    for event_type, message, payload in specs:
        _add_event(conn, "debug", row["debug_run_id"], event_type, message, payload)
    return status


def create_debug_run(
    *, repo: str, ref: str, created_by: str, selected_index: dict[str, Any] | None
) -> dict[str, Any]:
    debug_run_id = str(uuid.uuid4())
    ts = now_ms()
    scope = selected_index.get("scope_hash", "") if selected_index else ""
    with _LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO debug_runs(debug_run_id, repo, ref, cognee_scope_hash, status, created_by, created_at, updated_at, summary)
            VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)
            """,
            (debug_run_id, repo, ref, scope, created_by, ts, ts, "Waiting for a mock debugger slot."),
        )
        _add_event(conn, "debug", debug_run_id, "queued", "Mock debugger run queued.", {"repo": repo, "ref": ref})
        conn.commit()
    return {"debug_run_id": debug_run_id, "status": "queued", "mock": True}


def get_debug_run(debug_run_id: str) -> dict[str, Any] | None:
    with _LOCK, _connect() as conn:
        row = conn.execute("SELECT * FROM debug_runs WHERE debug_run_id=?", (debug_run_id,)).fetchone()
        if not row:
            return None
        status = _refresh_debug(conn, row)
        refreshed = conn.execute("SELECT * FROM debug_runs WHERE debug_run_id=?", (debug_run_id,)).fetchone()
        events = conn.execute(
            "SELECT * FROM events WHERE parent_type='debug' AND parent_id=? ORDER BY ts, id",
            (debug_run_id,),
        ).fetchall()
        conn.commit()
    return {
        "debug_run_id": debug_run_id,
        "repo": refreshed["repo"],
        "ref": refreshed["ref"],
        "cognee_scope_hash": refreshed["cognee_scope_hash"],
        "status": status,
        "summary": refreshed["summary"],
        "created_at": int(refreshed["created_at"]),
        "updated_at": int(refreshed["updated_at"]),
        "events": [
            {
                "ts": int(e["ts"]),
                "event_type": e["event_type"],
                "message": e["message"],
                "payload": _loads(e["payload"], {}),
            }
            for e in events
        ],
        "mock": True,
    }


def state_summary() -> dict[str, Any]:
    with _LOCK, _connect() as conn:
        return {
            "mode": "mock",
            "database": DB_PATH,
            "runs": int(conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]),
            "indexes": int(conn.execute("SELECT COUNT(*) AS n FROM indexes").fetchone()["n"]),
            "debug_runs": int(conn.execute("SELECT COUNT(*) AS n FROM debug_runs").fetchone()["n"]),
            "chat_messages": int(conn.execute("SELECT COUNT(*) AS n FROM chats").fetchone()["n"]),
        }


def reset() -> dict[str, Any]:
    with _LOCK:
        try:
            Path(DB_PATH).unlink(missing_ok=True)
            Path(DB_PATH + "-wal").unlink(missing_ok=True)
            Path(DB_PATH + "-shm").unlink(missing_ok=True)
        except Exception:
            pass
        init_db()
    return {"status": "reset", **state_summary()}


init_db()
