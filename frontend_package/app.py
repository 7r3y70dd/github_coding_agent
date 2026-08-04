import json
import html
import os
import re
import time
import uuid
from decimal import Decimal
from typing import Any

APP_MODE = os.environ.get("APP_MODE", "mock").strip().lower()
MOCK_BACKEND = os.environ.get(
    "MOCK_BACKEND", "1" if APP_MODE in {"mock", "preview", "demo"} else "0"
) == "1"

if MOCK_BACKEND:
    boto3 = None
    Key = None
else:
    import boto3
    from boto3.dynamodb.conditions import Key

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from urllib.parse import urlencode

try:
    from authlib.integrations.starlette_client import OAuth, OAuthError
except ModuleNotFoundError:
    if not MOCK_BACKEND:
        raise

    class OAuthError(Exception):
        error = "oauth_unavailable"
        description = "Authlib is not installed in this local mock environment."

    class OAuth:
        def register(self, *args: Any, **kwargs: Any) -> None:
            return None

from fastapi.responses import JSONResponse
from starlette.middleware.sessions import SessionMiddleware

from repo_indexing import (
    dataset_name_for_scope,
    dataset_names_for_scope,
    index_pk,
    index_sk,
    is_safe_relpath,
    normalize_ref,
    normalize_scope_paths,
    scope_hash as make_scope_hash,
    to_dynamo_value,
)


MOCK_USE_GITHUB = os.environ.get("MOCK_USE_GITHUB", "0") == "1"
DASHBOARD_ON_DEFAULT_HOST = os.environ.get(
    "DASHBOARD_ON_DEFAULT_HOST", "1" if MOCK_BACKEND else "0"
) == "1"
COOKIE_HTTPS_ONLY = os.environ.get(
    "COOKIE_HTTPS_ONLY", "0" if MOCK_BACKEND else "1"
) == "1"

if MOCK_BACKEND:
    import mock_backend
    answer_repo_chat = None
else:
    from repo_chat import answer_repo_chat

AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
AWS_ENDPOINT_URL = os.environ.get("AWS_ENDPOINT_URL", "").strip() or None
RUNS_TABLE = os.environ.get("RUNS_TABLE", "agent_runs")
AGENT_QUEUE_URL = os.environ.get("AGENT_QUEUE_URL", "").strip()
COGNEE_QUEUE_URL = os.environ.get("COGNEE_QUEUE_URL", "").strip()
DEBUG_QUEUE_URL = os.environ.get("DEBUG_QUEUE_URL", "").strip()
AGENT_QUEUE_NAME = os.environ.get("AGENT_QUEUE_NAME", "").strip()
COGNEE_QUEUE_NAME = os.environ.get("COGNEE_QUEUE_NAME", "").strip()
DEBUG_QUEUE_NAME = os.environ.get("DEBUG_QUEUE_NAME", "").strip()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", os.environ.get("GH_TOKEN", ""))
MAX_REPO_TREE_ITEMS = int(os.environ.get("MAX_REPO_TREE_ITEMS", "10000"))

SESSION_SECRET = os.environ.get("SESSION_SECRET", "dev-only-change-me")
COGNITO_REGION = os.environ.get("COGNITO_REGION", "us-east-1")
COGNITO_USER_POOL_ID = os.environ.get("COGNITO_USER_POOL_ID", "")
COGNITO_CLIENT_ID = os.environ.get("COGNITO_CLIENT_ID", "")
COGNITO_CLIENT_SECRET = os.environ.get("COGNITO_CLIENT_SECRET", "")
COGNITO_DOMAIN = os.environ.get("COGNITO_DOMAIN", "").rstrip("/")

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://cognimoss.com").rstrip("/")
APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://app.cognimoss.com").rstrip("/")
AUTH_ENABLED = os.environ.get("AUTH_ENABLED", "0") == "1"

REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def normalize_repo(repo: str) -> str:
    repo = (repo or "").strip()
    if not REPO_RE.match(repo):
        raise HTTPException(
            status_code=400,
            detail="Repository must be in owner/repo format.",
        )
    return repo

if MOCK_BACKEND:
    dynamodb = None
    sqs = None
    table = None
else:
    _aws_kwargs = {"region_name": AWS_REGION}
    if AWS_ENDPOINT_URL:
        _aws_kwargs["endpoint_url"] = AWS_ENDPOINT_URL

    dynamodb = boto3.resource("dynamodb", **_aws_kwargs)
    sqs = boto3.client("sqs", **_aws_kwargs)
    table = dynamodb.Table(RUNS_TABLE)

    if not AGENT_QUEUE_URL and AGENT_QUEUE_NAME:
        AGENT_QUEUE_URL = sqs.get_queue_url(QueueName=AGENT_QUEUE_NAME)["QueueUrl"]
    if not COGNEE_QUEUE_URL and COGNEE_QUEUE_NAME:
        COGNEE_QUEUE_URL = sqs.get_queue_url(QueueName=COGNEE_QUEUE_NAME)["QueueUrl"]
    if not DEBUG_QUEUE_URL and DEBUG_QUEUE_NAME:
        DEBUG_QUEUE_URL = sqs.get_queue_url(QueueName=DEBUG_QUEUE_NAME)["QueueUrl"]

app = FastAPI(title="Cognimoss Agent Platform")

oauth = OAuth()

if COGNITO_USER_POOL_ID and COGNITO_CLIENT_ID and COGNITO_CLIENT_SECRET:
    oauth.register(
        name="cognito",
        client_id=COGNITO_CLIENT_ID,
        client_secret=COGNITO_CLIENT_SECRET,
        server_metadata_url=(
            f"https://cognito-idp.{COGNITO_REGION}.amazonaws.com/"
            f"{COGNITO_USER_POOL_ID}/.well-known/openid-configuration"
        ),
        client_kwargs={"scope": "openid email profile"},
    )


def current_user(request: Request) -> dict[str, Any] | None:
    try:
        user = request.session.get("user")
    except AssertionError:
        return None

    return user if isinstance(user, dict) else None


def current_user_email(request: Request) -> str:
    user = current_user(request) or {}
    return str(user.get("email") or user.get("username") or "").strip().lower()


def is_protected_request(request: Request) -> bool:
    host = request.headers.get("host", "").split(":")[0].lower()
    path = request.url.path

    public_paths = {
        "/health",
        "/login",
        "/auth/callback",
        "/logout",
    }

    if path in public_paths:
        return False

    # Public marketing site stays public.
    if host in {"cognimoss.com", "www.cognimoss.com"}:
        return False

    # App dashboard host is protected.
    if host == "app.cognimoss.com":
        return True

    return False


@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    if is_app_host(request):
        return HTMLResponse(dashboard_home(request))

    body = """
      <section class="hero">
        <span class="eyebrow">AI software agents for code workflows</span>
        <h1>Cognimoss</h1>
        <p>
          Cognimoss helps teams queue software tasks for coding agents that can
          read repository context, plan changes, and open pull requests for review.
        </p>
        <div class="actions">
          <a class="button" href="https://app.cognimoss.com">Open Dashboard</a>
          <a class="button secondary" href="https://app.cognimoss.com/applications">Open Coding Agent</a>
        </div>
      </section>

      <section class="grid">
        <div class="card">
          <h2>Git Agent</h2>
          <p>Queue GitHub issues and have an agent create a reviewable pull request.</p>
        </div>
        <div class="card">
          <h2>Repository context</h2>
          <p>Use project files and memory context to help agents make targeted changes.</p>
        </div>
        <div class="card">
          <h2>Human review</h2>
          <p>Agents open branches and PRs so maintainers stay in control.</p>
        </div>
      </section>
    """
    return HTMLResponse(shell("Cognimoss", body, app_nav=False))

@app.middleware("http")
async def auth_gate(request: Request, call_next):
    if not AUTH_ENABLED:
        return await call_next(request)

    if not is_protected_request(request):
        return await call_next(request)

    if current_user(request):
        return await call_next(request)

    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Login required"}, status_code=401)

    return RedirectResponse(url="/login", status_code=302)

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    https_only=COOKIE_HTTPS_ONLY,
    same_site="lax",
)


class CreateRunRequest(BaseModel):
    repo: str
    issue_number: int
    title: str
    cognee_scope_hash: str = ""


class CreateCogneeSeedRequest(BaseModel):
    repo: str
    ref: str = "main"
    mode: str = "auto"
    include_paths: list[str]
    exclude_paths: list[str] = []


class RepoChatRequest(BaseModel):
    repo: str
    message: str
    cognee_scope_hash: str = ""


class DebugRunRequest(BaseModel):
    repo: str
    ref: str = "main"
    cognee_scope_hash: str = ""


def now_ms() -> int:
    return int(time.time() * 1000)


def host_from_request(request: Request) -> str:
    return request.headers.get("host", "").split(":")[0].lower()


def is_public_host(request: Request) -> bool:
    host = host_from_request(request)
    return host == "cognimoss.com" or host == "www.cognimoss.com"


def is_app_host(request: Request) -> bool:
    host = host_from_request(request)
    if host == "app.cognimoss.com":
        return True
    if DASHBOARD_ON_DEFAULT_HOST and not is_public_host(request):
        return True
    return False


def put_event(
    run_id: str,
    event_type: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> None:
    if MOCK_BACKEND:
        return
    ts = now_ms()
    table.put_item(
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


def put_cognee_job_event(
    job_id: str,
    event_type: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> None:
    if MOCK_BACKEND:
        return
    ts = now_ms()
    table.put_item(
        Item=to_dynamo_value(
            {
                "pk": f"COGNEE_JOB#{job_id}",
                "sk": f"EVENT#{ts}",
                "job_id": job_id,
                "ts": ts,
                "event_type": event_type,
                "message": message,
                "payload": payload or {},
            }
        )
    )


def latest_ready_cognee_index(repo: str, scope_hash: str = "") -> dict[str, Any] | None:
    repo = normalize_repo(repo)
    scope_hash = (scope_hash or "").strip()

    if MOCK_BACKEND:
        return mock_backend.latest_ready_index(repo, scope_hash)

    if scope_hash:
        resp = table.get_item(Key={"pk": index_pk(repo), "sk": index_sk(scope_hash)})
        item = resp.get("Item") or {}
        if item.get("status") == "ready" and item.get("dataset_name"):
            return item
        return None

    resp = table.query(KeyConditionExpression=Key("pk").eq(index_pk(repo)))
    items = [i for i in resp.get("Items", []) if i.get("status") == "ready" and i.get("dataset_name")]
    if not items:
        return None
    items.sort(key=lambda x: int(x.get("last_indexed_at") or x.get("updated_at") or 0), reverse=True)
    return items[0]


def clean_index_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "repo": item.get("repo", ""),
        "ref": item.get("ref", ""),
        "scope_hash": item.get("scope_hash", ""),
        "snapshot_key": item.get("snapshot_key", ""),
        "dataset_name": item.get("dataset_name", ""),
        "code_dataset_name": item.get("code_dataset_name", item.get("dataset_name", "")),
        "rules_dataset_name": item.get("rules_dataset_name", ""),
        "data_ids_supported": bool(item.get("data_ids_supported", False)),
        "status": item.get("status", ""),
        "status_message": item.get("status_message", ""),
        "include_paths": item.get("include_paths", []),
        "exclude_paths": item.get("exclude_paths", []),
        "head_sha": item.get("head_sha", ""),
        "effective_mode": item.get("effective_mode", ""),
        "selected_file_count": int(item.get("selected_file_count") or 0),
        "changed_count": int(item.get("changed_count") or 0),
        "removed_count": int(item.get("removed_count") or 0),
        "unchanged_count": int(item.get("unchanged_count") or 0),
        "created_at": int(item.get("created_at") or 0),
        "updated_at": int(item.get("updated_at") or 0),
        "last_indexed_at": int(item.get("last_indexed_at") or 0),
        "last_job_id": item.get("last_job_id", ""),
    }


def github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "Cognimoss-Agent-Platform",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def shell(title: str, body: str, app_nav: bool = False) -> str:
    if app_nav:
        nav = f"""
          <a href="/applications">Coding Agent</a>
          <a href="/code-projects">Code Projects</a>
          <a href="{PUBLIC_BASE_URL}">Public Site</a>
        """
    else:
        nav = f"""
          <a href="{PUBLIC_BASE_URL}">Home</a>
          <a href="{APP_BASE_URL}">Dashboard</a>
        """

    mode_banner = ""
    if MOCK_BACKEND:
        mode_banner = """
          <div class="mode-banner">
            <strong>Mock backend enabled</strong>
            <span>AWS queues, DynamoDB, Bedrock, Cognee, and debugger workers are being simulated locally.</span>
          </div>
        """

    return f"""
<!doctype html>
<html>
  <head>
    <title>{title}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>
      :root {{
        --bg: #03140d;
        --panel: #062016;
        --card: #08281b;
        --text: #e6f4ea;
        --muted: #9bbfa3;
        --accent: #34d399;
        --accent2: #a78bfa;
        --border: #1f4f35;
      }}
      * {{ box-sizing: border-box; }}
      body {{
        margin: 0;
        font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        color: var(--text);
        background:
          radial-gradient(circle at top left, rgba(125, 211, 252, 0.18), transparent 30%),
          radial-gradient(circle at top right, rgba(167, 139, 250, 0.18), transparent 30%),
          linear-gradient(135deg, #03140d, #062016);
        min-height: 100vh;
      }}
      header {{
        border-bottom: 1px solid var(--border);
        background: rgba(2, 6, 23, 0.78);
        backdrop-filter: blur(12px);
        position: sticky;
        top: 0;
        z-index: 10;
      }}
      .nav {{
        max-width: 1120px;
        margin: 0 auto;
        padding: 16px 24px;
        display: flex;
        align-items: center;
        justify-content: space-between;
      }}
      .brand {{
        color: white;
        text-decoration: none;
        font-weight: 850;
        letter-spacing: -0.04em;
        font-size: 20px;
      }}
      .nav-links a {{
        color: var(--muted);
        text-decoration: none;
        margin-left: 18px;
        font-size: 14px;
      }}
      .nav-links a:hover {{ color: white; }}
      main {{
        max-width: 1120px;
        margin: 0 auto;
        padding: 56px 24px;
      }}
      h1 {{
        font-size: clamp(42px, 7vw, 72px);
        line-height: 0.98;
        letter-spacing: -0.07em;
        margin: 0 0 20px;
      }}
      h2 {{
        margin: 0 0 12px;
        letter-spacing: -0.03em;
      }}
      p {{
        color: var(--muted);
        line-height: 1.7;
        font-size: 17px;
      }}
      .hero {{
        max-width: 820px;
        margin-bottom: 34px;
      }}
      .eyebrow {{
        display: inline-block;
        border: 1px solid var(--border);
        border-radius: 999px;
        padding: 6px 12px;
        color: var(--accent);
        margin-bottom: 20px;
        font-size: 13px;
        font-weight: 700;
      }}
      .grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
        gap: 20px;
      }}
      .card {{
        border: 1px solid var(--border);
        background: rgba(15, 23, 42, 0.78);
        border-radius: 22px;
        padding: 24px;
        box-shadow: 0 24px 55px rgba(0, 0, 0, 0.25);
      }}
      .button, button {{
        display: inline-block;
        border: 0;
        border-radius: 14px;
        background: linear-gradient(135deg, var(--accent), var(--accent2));
        color: #03140d;
        padding: 12px 16px;
        font-weight: 800;
        text-decoration: none;
        cursor: pointer;
      }}
      .secondary {{
        background: transparent;
        border: 1px solid var(--border);
        color: var(--text);
      }}
      input, select, textarea {{
        width: 100%;
        border: 1px solid var(--border);
        background: #03140d;
        color: var(--text);
        border-radius: 12px;
        padding: 12px;
        margin: 6px 0 14px;
      }}
      label {{
        color: var(--muted);
        font-size: 14px;
      }}
      pre {{
        background: #03140d;
        border: 1px solid var(--border);
        color: #d1d5db;
        padding: 16px;
        border-radius: 14px;
        overflow-x: auto;
        min-height: 160px;
      }}
      .status {{
        display: inline-block;
        border: 1px solid var(--border);
        color: var(--muted);
        border-radius: 999px;
        padding: 4px 10px;
        font-size: 13px;
      }}
      .actions {{
        display: flex;
        gap: 12px;
        flex-wrap: wrap;
        margin-top: 24px;
      }}
      .mode-banner {{
        border: 1px solid rgba(125, 211, 252, 0.45);
        background: rgba(14, 116, 144, 0.18);
        border-radius: 16px;
        padding: 14px 16px;
        margin-bottom: 26px;
        display: flex;
        flex-wrap: wrap;
        gap: 8px 14px;
        align-items: center;
      }}
      .mode-banner strong {{ color: var(--accent); }}
      .mode-banner span {{ color: var(--muted); font-size: 14px; }}
    </style>
  </head>
  <body>
    <header>
      <div class="nav">
        <a class="brand" href="/">Cognimoss</a>
        <div class="nav-links">
          {nav}
        </div>
      </div>
    </header>
    <main>{mode_banner}{body}</main>
  </body>
</html>
"""


def coding_agent_sidebar(active_href: str, main_body: str) -> str:
    def link_class(href: str) -> str:
        return "side-link active" if href == active_href else "side-link"

    return f"""
      <style>
        .agent-layout {{
          display: grid;
          grid-template-columns: 260px minmax(0, 1fr);
          gap: 20px;
          align-items: start;
        }}
        .agent-sidebar {{ position: sticky; top: 88px; }}
        .agent-sidebar h2 {{ margin-bottom: 16px; }}
        .side-link {{
          display: block;
          border: 1px solid var(--border);
          border-radius: 14px;
          padding: 14px;
          margin-bottom: 10px;
          color: var(--text);
          text-decoration: none;
          background: rgba(2, 6, 23, 0.35);
        }}
        .side-link span {{
          display: block;
          color: var(--muted);
          font-size: 12px;
          line-height: 1.45;
          margin-top: 4px;
        }}
        .side-link:hover, .side-link.active {{
          border-color: rgba(125, 211, 252, 0.75);
          background: rgba(14, 116, 144, 0.18);
        }}
        .repo-toolbar {{
          display: grid;
          grid-template-columns: minmax(220px, 1fr) 140px auto;
          gap: 12px;
          align-items: end;
        }}
        .repo-card {{
          border: 1px solid var(--border);
          background: rgba(2, 6, 23, 0.35);
          border-radius: 18px;
          padding: 18px;
          margin-top: 14px;
        }}
        .repo-card h3 {{ margin: 0 0 8px; }}
        .repo-meta {{
          display: flex;
          gap: 8px;
          flex-wrap: wrap;
          margin: 10px 0 12px;
        }}
        .repo-meta .status {{ background: rgba(15, 23, 42, 0.75); }}
        .lang-bar {{
          display: flex;
          height: 10px;
          overflow: hidden;
          border-radius: 999px;
          background: #03140d;
          border: 1px solid var(--border);
          margin: 12px 0;
        }}
        .lang-seg {{ min-width: 3px; }}
        .lang-list {{
          display: flex;
          gap: 8px;
          flex-wrap: wrap;
          color: var(--muted);
          font-size: 13px;
        }}
        @media (max-width: 780px) {{
          .agent-layout {{ grid-template-columns: 1fr; }}
          .agent-sidebar {{ position: static; }}
          .repo-toolbar {{ grid-template-columns: 1fr; }}
        }}
      </style>

      <section class="agent-layout">
        <aside class="card agent-sidebar">
          <h2>Coding Agent</h2>
          <a class="{link_class('/applications')}" href="/applications">Repositories<span>Added repositories and context status.</span></a>
          <a class="{link_class('/applications/git-agent')}" href="/applications/git-agent">Git Agent<span>Queue issue-to-PR coding runs.</span></a>
          <a class="{link_class('/applications/cognee-ops')}" href="/applications/cognee-ops">Cognee Ops<span>Seed and inspect repository memory.</span></a>
          <a class="{link_class('/applications/repo-chat')}" href="/applications/repo-chat">Repo Chat<span>Ask questions against saved repo context.</span></a>
        </aside>

        <div class="agent-main">
          {main_body}
        </div>
      </section>
    """


@app.get("/health")
def health() -> dict[str, Any]:
    response: dict[str, Any] = {
        "status": "ok",
        "mode": "mock" if MOCK_BACKEND else APP_MODE,
        "mock_backend": MOCK_BACKEND,
    }
    if MOCK_BACKEND:
        response["mock_state"] = mock_backend.state_summary()
    return response


@app.get("/api/mock/state")
def mock_state() -> dict[str, Any]:
    if not MOCK_BACKEND:
        raise HTTPException(status_code=404, detail="Mock backend is not enabled.")
    return mock_backend.state_summary()


@app.post("/api/mock/reset")
def reset_mock_state() -> dict[str, Any]:
    if not MOCK_BACKEND:
        raise HTTPException(status_code=404, detail="Mock backend is not enabled.")
    return mock_backend.reset()

@app.get("/login")
async def login(request: Request):
    if not AUTH_ENABLED:
        return RedirectResponse(url="/")

    if not COGNITO_USER_POOL_ID or not COGNITO_CLIENT_ID or not COGNITO_CLIENT_SECRET:
        return HTMLResponse(
            "<h1>Cognito is not configured</h1><p>Missing Cognito environment variables.</p>",
            status_code=500,
        )

    redirect_uri = f"{APP_BASE_URL}/auth/callback"
    return await oauth.cognito.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback")
async def auth_callback(request: Request):
    if not AUTH_ENABLED:
        return RedirectResponse(url="/")

    try:
        token = await oauth.cognito.authorize_access_token(request)
    except OAuthError as exc:
        return HTMLResponse(
            f"<h1>Login failed</h1><p>{exc.error}: {exc.description}</p>",
            status_code=400,
        )

    userinfo = token.get("userinfo")
    if not userinfo:
        userinfo = await oauth.cognito.userinfo(token=token)

    request.session["user"] = dict(userinfo)
    return RedirectResponse(url="/")


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()

    if not COGNITO_DOMAIN or not COGNITO_CLIENT_ID:
        return RedirectResponse(url=PUBLIC_BASE_URL)

    query = urlencode(
        {
            "client_id": COGNITO_CLIENT_ID,
            "logout_uri": f"{APP_BASE_URL}/",
        }
    )

    return RedirectResponse(url=f"{COGNITO_DOMAIN}/logout?{query}")


def dashboard_home(request: Request) -> str:
    body = """
      <section class="hero">
        <span class="eyebrow">Dashboard</span>
        <h1>Cognimoss Dashboard</h1>
        <p>
          Manage agent applications, code projects, and queued automation work
          from a secure control center.
        </p>
      </section>

      <section class="grid">
        <div class="card">
          <h2>Coding Agent</h2>
          <p>Manage repositories, queue coding-agent work, seed memory, and chat with repo context.</p>
          <a class="button" href="/applications">Open Coding Agent</a>
        </div>
        <div class="card">
          <h2>Code Projects</h2>
          <p>Review repositories connected to Cognimoss and manage access.</p>
          <a class="button" href="/code-projects">View Code Projects</a>
        </div>
      </section>
    """
    return shell("Dashboard | Cognimoss", body, app_nav=True)

@app.get("/applications", response_class=HTMLResponse)
def applications(request: Request) -> str:
    body = """
      <section class="hero">
        <span class="eyebrow">Coding Agent</span>
        <h1>Repository workspace</h1>
        <p>
          Start from repositories first. Add a repo, review basic file counts and
          language mix, then jump into issue runs, repository memory, or repo chat.
        </p>
      </section>

      <section class="card">
        <h2>Added repositories</h2>
        <p>
          Repositories added here are saved in this browser for now. Recent run
          repositories are also discovered automatically.
        </p>

        <div class="repo-toolbar">
          <div>
            <label>Repository</label>
            <input id="repo-input" placeholder="owner/repo" />
          </div>
          <div>
            <label>Ref</label>
            <input id="repo-ref" value="main" />
          </div>
          <button onclick="addRepository()">Add repository</button>
        </div>

        <p id="repo-message"></p>
        <div id="repo-list"></div>
      </section>

      <script>
        const STORAGE_KEY = "cognimoss.addedRepos.v1";
        const LANG_COLORS = ["#34d399", "#a78bfa", "#34d399", "#fbbf24", "#fb7185", "#c084fc"];
        const EXT_LANGUAGE = {
          ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript",
          ".html": "HTML", ".css": "CSS", ".scss": "CSS", ".sass": "CSS", ".md": "Markdown",
          ".json": "JSON", ".yml": "YAML", ".yaml": "YAML", ".toml": "TOML", ".sh": "Shell",
          ".bash": "Shell", ".zsh": "Shell", ".sql": "SQL", ".go": "Go", ".rs": "Rust", ".java": "Java",
          ".cpp": "C++", ".cc": "C++", ".c": "C", ".h": "C/C++", ".hpp": "C++"
        };

        function escapeHtml(value) {
          return String(value ?? "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#039;");
        }

        function normalizeRepo(value) {
          return String(value || "").trim();
        }

        function loadSavedRepos() {
          try {
            const parsed = JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]");
            return Array.isArray(parsed) ? parsed.filter(Boolean) : [];
          } catch (_) {
            return [];
          }
        }

        function saveRepos(repos) {
          localStorage.setItem(
            STORAGE_KEY,
            JSON.stringify([...new Set(repos.map(normalizeRepo).filter(Boolean))])
          );
        }

        function extname(path) {
          const name = String(path || "").split("/").pop() || "";
          const idx = name.lastIndexOf(".");
          return idx > 0 ? name.slice(idx).toLowerCase() : "";
        }

        function languageFor(path) {
          const base = String(path || "").split("/").pop() || "";
          if (["Dockerfile", "Makefile", "Procfile"].includes(base)) return base;
          return EXT_LANGUAGE[extname(path)] || "Other";
        }

        function summarizeTree(data) {
          const items = data.items || [];
          const files = items.filter(i => i.type === "blob");
          const dirs = items.filter(i => i.type === "tree");
          const bytes = files.reduce((sum, i) => sum + Number(i.size || 0), 0);
          const langs = {};

          for (const file of files) {
            const lang = languageFor(file.path);
            langs[lang] = (langs[lang] || 0) + 1;
          }

          return {
            files,
            dirs,
            bytes,
            distribution: Object.entries(langs).sort((a, b) => b[1] - a[1])
          };
        }

        function renderDistribution(distribution, totalFiles) {
          if (!distribution.length || !totalFiles) {
            return `<p>No file distribution available.</p>`;
          }

          const top = distribution.slice(0, 6);
          const bar = top.map(([lang, count], idx) => {
            const pct = Math.max(2, (count / totalFiles) * 100);
            return `<div class="lang-seg" title="${escapeHtml(lang)} ${count}" style="width:${pct}%; background:${LANG_COLORS[idx % LANG_COLORS.length]};"></div>`;
          }).join("");

          const labels = top
            .map(([lang, count]) => `<span>${escapeHtml(lang)} · ${count}</span>`)
            .join("");

          return `<div class="lang-bar">${bar}</div><div class="lang-list">${labels}</div>`;
        }

        function repoActions(repo) {
          const safeRepo = encodeURIComponent(repo);
          return `
            <div class="actions">
              <a class="button secondary" href="/applications/git-agent?repo=${safeRepo}">Queue issue</a>
              <a class="button secondary" href="/applications/cognee-ops?repo=${safeRepo}">Seed memory</a>
              <a class="button secondary" href="/applications/repo-chat?repo=${safeRepo}">Repo chat</a>
              <a class="button secondary" href="/applications/cognee-graph?repo=${safeRepo}">Knowledge graph</a>
            </div>
          `;
        }

        async function renderRepoCard(repo, ref = "main") {
          const id = `repo-${repo.replace(/[^A-Za-z0-9_-]/g, "-")}`;
          const target = document.getElementById(id);

          target.innerHTML = `
            <h3>${escapeHtml(repo)}</h3>
            <p>Loading repository metadata from the tree API...</p>
          `;

          try {
            const res = await fetch(`/api/repos/tree?repo=${encodeURIComponent(repo)}&ref=${encodeURIComponent(ref || "main")}`);
            const data = await res.json();

            if (!res.ok) {
              throw new Error(data.detail || "Unable to load repository tree.");
            }

            const summary = summarizeTree(data);

            target.innerHTML = `
              <h3>${escapeHtml(repo)}</h3>
              <div class="repo-meta">
                <span class="status">${escapeHtml(data.ref || ref || "main")}</span>
                <span class="status">${summary.files.length} files</span>
                <span class="status">${summary.dirs.length} folders</span>
                <span class="status">${Math.round(summary.bytes / 1024)} KB tree size</span>
                ${data.truncated ? `<span class="status">truncated</span>` : ""}
              </div>
              ${renderDistribution(summary.distribution, summary.files.length)}
              ${repoActions(repo)}
            `;
          } catch (err) {
            target.innerHTML = `
              <h3>${escapeHtml(repo)}</h3>
              <p>${escapeHtml(err.message || "Repository metadata unavailable.")}</p>
              ${repoActions(repo)}
            `;
          }
        }

        async function discoverRunRepos() {
          try {
            const res = await fetch("/api/runs");
            const data = await res.json();

            if (!res.ok) return [];

            return [...new Set((data.runs || []).map(r => normalizeRepo(r.repo)).filter(Boolean))];
          } catch (_) {
            return [];
          }
        }

        async function loadRepositories() {
          const target = document.getElementById("repo-list");
          const saved = loadSavedRepos();
          const runRepos = await discoverRunRepos();
          const repos = [...new Set([...saved, ...runRepos])];

          if (!repos.length) {
            target.innerHTML = `<p>No repositories added yet. Add an owner/repo above.</p>`;
            return;
          }

          target.innerHTML = repos
            .map(repo => `<div class="repo-card" id="repo-${repo.replace(/[^A-Za-z0-9_-]/g, "-")}"></div>`)
            .join("");

          for (const repo of repos) {
            renderRepoCard(repo, document.getElementById("repo-ref").value.trim() || "main");
          }
        }

        function addRepository() {
          const input = document.getElementById("repo-input");
          const message = document.getElementById("repo-message");
          const repo = normalizeRepo(input.value);

          if (!/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repo)) {
            message.textContent = "Use owner/repo format.";
            return;
          }

          const repos = loadSavedRepos();
          saveRepos([...repos, repo]);
          input.value = "";
          message.textContent = `Added ${repo}.`;
          loadRepositories();
        }

        loadRepositories();
      </script>
    """
    return shell("Coding Agent | Cognimoss", coding_agent_sidebar("/applications", body), app_nav=True)


@app.get("/coding-agent", response_class=HTMLResponse)
def coding_agent_redirect() -> RedirectResponse:
    return RedirectResponse(url="/applications", status_code=302)


@app.get("/code-projects", response_class=HTMLResponse)
def code_projects(request: Request) -> str:
    body = """
      <section class="hero">
        <span class="eyebrow">Dashboard</span>
        <h1>Code Projects</h1>
        <p>
          Manage repositories that Cognimoss can work with. During the beta,
          repository owners invite the Cognimoss bot account as a collaborator.
        </p>
      </section>

      <section class="grid">
        <div class="card">
          <h2>Repository access</h2>
          <p>Only approved repositories should be queued for automation.</p>
          <span class="status">Beta access model</span>
        </div>
        <div class="card">
          <h2>Bot collaborator</h2>
          <p>The GitHub bot credential remains server-side and is never exposed to users.</p>
          <span class="status">Protected credential</span>
        </div>
      </section>
    """
    return shell("Code Projects | Cognimoss", body, app_nav=True)

@app.get("/applications/git-agent", response_class=HTMLResponse)
def git_agent(request: Request) -> str:
    body = """
      <section class="hero">
        <span class="eyebrow">Application</span>
        <h1>Git Agent</h1>
        <p>
          Queue GitHub issue work for the coding agent. The agent uses the approved
          bot account to create a branch and open a pull request for review.
        </p>
      </section>

      <section class="grid">
        <div class="card">
          <h2>Queue issue</h2>

          <label>GitHub repository</label>
          <input id="repo" placeholder="owner/repo" />

          <label>Issue number</label>
          <input id="issue" type="number" placeholder="123" />

          <label>Run title</label>
          <input id="title" placeholder="Short description of the requested change" />

          <label>Cognee context scope</label>
          <select id="cognee-scope">
            <option value="">Use latest ready scope for this repo</option>
          </select>
          <button class="secondary" onclick="loadCogneeScopes()" style="margin-bottom: 14px;">Load ready scopes</button>
          <p id="scope-message"></p>

          <button onclick="createRun()">Queue issue</button>
          <p id="queue-message"></p>
        </div>

        <div class="card">
          <h2>Queue status</h2>
          <p>
            Submitted issues remain queued until a worker starts processing them.
            Refresh is automatic.
          </p>
          <button onclick="loadAll()">Refresh</button>
        </div>
      </section>

      <section class="card" style="margin-top: 20px;">
        <h2>Queued issues</h2>
        <div id="queued-table"></div>
      </section>

      <section class="card" style="margin-top: 20px;">
        <h2>Recent runs</h2>
        <div id="runs-table"></div>
      </section>

      <section class="card" style="margin-top: 20px;">
        <h2>Run events</h2>
        <pre id="run-events" style="white-space:pre-wrap; min-height:100px;">Select a run to inspect its mock lifecycle.</pre>
      </section>

      <script>
        function escapeHtml(value) {
          return String(value ?? "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#039;");
        }

        function formatTime(ms) {
          if (!ms) return "";
          return new Date(ms).toLocaleString();
        }

        function renderTable(targetId, rows, emptyMessage) {
          const target = document.getElementById(targetId);

          if (!rows || rows.length === 0) {
            target.innerHTML = `<p>${escapeHtml(emptyMessage)}</p>`;
            return;
          }

          const html = `
            <div style="overflow-x:auto;">
              <table style="width:100%; border-collapse: collapse;">
                <thead>
                  <tr>
                    <th style="text-align:left; padding:10px; border-bottom:1px solid var(--border);">Status</th>
                    <th style="text-align:left; padding:10px; border-bottom:1px solid var(--border);">Repo</th>
                    <th style="text-align:left; padding:10px; border-bottom:1px solid var(--border);">Issue</th>
                    <th style="text-align:left; padding:10px; border-bottom:1px solid var(--border);">Title</th>
                    <th style="text-align:left; padding:10px; border-bottom:1px solid var(--border);">Created</th>
                    <th style="text-align:left; padding:10px; border-bottom:1px solid var(--border);">Details</th>
                  </tr>
                </thead>
                <tbody>
                  ${rows.map(r => `
                    <tr>
                      <td style="padding:10px; border-bottom:1px solid var(--border);">
                        <span class="status">${escapeHtml(r.status)}</span>
                      </td>
                      <td style="padding:10px; border-bottom:1px solid var(--border);">${escapeHtml(r.repo)}</td>
                      <td style="padding:10px; border-bottom:1px solid var(--border);">#${escapeHtml(r.issue_number)}</td>
                      <td style="padding:10px; border-bottom:1px solid var(--border);">${escapeHtml(r.title)}</td>
                      <td style="padding:10px; border-bottom:1px solid var(--border);">${escapeHtml(formatTime(r.created_at))}</td>
                      <td style="padding:10px; border-bottom:1px solid var(--border);">
                        <button class="secondary" onclick="loadRunEvents('${escapeHtml(r.run_id)}')">View</button>
                      </td>
                    </tr>
                  `).join("")}
                </tbody>
              </table>
            </div>
          `;

          target.innerHTML = html;
        }

        async function loadCogneeScopes() {
          const repo = document.getElementById("repo").value.trim();
          const select = document.getElementById("cognee-scope");
          const message = document.getElementById("scope-message");

          if (!repo) {
            message.textContent = "Enter a repository first.";
            return;
          }

          message.textContent = "Loading ready Cognee scopes...";
          const res = await fetch(`/api/cognee/indexes?repo=${encodeURIComponent(repo)}`);
          const data = await res.json();

          if (!res.ok) {
            message.textContent = data.detail || "Unable to load Cognee scopes.";
            return;
          }

          const ready = (data.indexes || []).filter(i => i.status === "ready");
          select.innerHTML = `<option value="">Use latest ready scope for this repo</option>` +
            ready.map(i => `<option value="${escapeHtml(i.scope_hash)}">${escapeHtml(i.scope_hash)} - ${escapeHtml(i.ref || "main")} - ${escapeHtml(i.selected_file_count)} files</option>`).join("");
          message.textContent = ready.length ? `Loaded ${ready.length} ready scope(s).` : "No ready Cognee scopes yet.";
        }

        async function createRun() {
          const repo = document.getElementById("repo").value.trim();
          const issue_number = Number(document.getElementById("issue").value);
          const title = document.getElementById("title").value.trim();
          const cognee_scope_hash = document.getElementById("cognee-scope").value;
          const message = document.getElementById("queue-message");

          message.textContent = "Queueing issue...";

          const res = await fetch("/api/runs", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ repo, issue_number, title, cognee_scope_hash })
          });

          const data = await res.json();

          if (!res.ok) {
            message.textContent = data.detail || "Unable to queue issue.";
            return;
          }

          message.textContent = `Queued run ${data.run_id}`;
          document.getElementById("issue").value = "";
          document.getElementById("title").value = "";
          await loadAll();
        }

        async function loadQueued() {
          const res = await fetch("/api/runs?status=queued");
          const data = await res.json();
          renderTable("queued-table", data.runs || [], "No queued issues.");
        }

        async function loadRuns() {
          const res = await fetch("/api/runs");
          const data = await res.json();
          renderTable("runs-table", data.runs || [], "No recent runs yet.");
        }

        async function loadRunEvents(runId) {
          const target = document.getElementById("run-events");
          target.textContent = `Loading events for ${runId}...`;
          const res = await fetch(`/api/runs/${encodeURIComponent(runId)}/events`);
          const data = await res.json();
          if (!res.ok) {
            target.textContent = data.detail || "Unable to load run events.";
            return;
          }
          const events = data.events || [];
          target.textContent = events.length
            ? events.map(e => `${formatTime(e.ts)}  [${e.event_type}] ${e.message}${Object.keys(e.payload || {}).length ? `\\n${JSON.stringify(e.payload, null, 2)}` : ""}`).join("\\n\\n")
            : "No events for this run yet.";
        }

        async function loadAll() {
          await Promise.all([loadQueued(), loadRuns()]);
        }

        const initialRepo = new URLSearchParams(window.location.search).get("repo") || "";
        if (initialRepo) {
          document.getElementById("repo").value = initialRepo;
          loadCogneeScopes();
        }

        loadAll();
        setInterval(loadAll, 10000);
      </script>
    """
    return shell("Git Agent | Cognimoss", coding_agent_sidebar("/applications/git-agent", body), app_nav=True)


@app.get("/applications/cognee-ops", response_class=HTMLResponse)
def cognee_ops(request: Request) -> str:
    body = """
      <style>
        .tree-panel {
          border: 1px solid var(--border);
          background: #03140d;
          border-radius: 14px;
          max-height: 620px;
          overflow: auto;
          padding: 12px;
        }
        .tree-node { font-size: 14px; }
        .tree-row {
          display: flex;
          align-items: center;
          gap: 8px;
          min-height: 28px;
          white-space: nowrap;
        }
        .tree-row input { width: auto; margin: 0; }
        .tree-children { margin-left: 18px; border-left: 1px solid rgba(148, 163, 184, 0.18); padding-left: 8px; }
        .muted-small { color: var(--muted); font-size: 13px; }
        .inline-actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
        .inline-actions > * { flex: 1; min-width: 160px; }
      </style>

      <section class="hero">
        <span class="eyebrow">Cognee Operations</span>
        <h1>Repository memory seeding</h1>
        <p>
          Seed or update Cognee outside the agent worker path. Choose a repository,
          load its tree, select the directories/files that should become context,
          and queue a full or incremental index job.
        </p>
      </section>

      <section class="grid">
        <div class="card">
          <h2>Seed scope</h2>

          <label>GitHub repository</label>
          <input id="repo" placeholder="owner/repo" />

          <label>Git ref</label>
          <input id="ref" value="main" placeholder="main" />

          <label>Seed mode</label>
          <select id="mode">
            <option value="auto">Auto: full first time, incremental after</option>
            <option value="incremental">Incremental/update only</option>
            <option value="full">Force full reseed</option>
          </select>

          <div class="inline-actions">
            <button onclick="loadTree()">Load repo tree</button>
            <button class="secondary" onclick="queueSeed()">Re-seed selected scope</button>
          </div>
          <p id="seed-message"></p>
        </div>

        <div class="card">
          <h2>How auto mode decides</h2>
          <p>
            Auto mode computes a scope hash from repo/ref/include paths. If no
            file manifest exists for that scope, the indexer does a full seed.
            If a manifest exists, it hashes selected files and only updates new,
            changed, or removed files.
          </p>
          <span class="status">Agent workers do not seed repositories</span>
        </div>
      </section>

      <section class="card" style="margin-top: 20px;">
        <h2>Repository tree</h2>
        <p class="muted-small">
          Checking a directory checks its descendants. You can then uncheck individual files or subdirectories.
          Protected paths and oversized/binary files are filtered again server-side by the indexer.
        </p>
        <div id="repo-tree" class="tree-panel"><p>Load a repository tree to begin.</p></div>
      </section>

      <section class="card" style="margin-top: 20px;">
        <h2>Cognee index status</h2>
        <button onclick="loadIndexes()">Refresh status</button>
        <div id="indexes-table" style="margin-top: 12px;"></div>
      </section>

      <script>
        let treeRoot = null;

        function escapeHtml(value) {
          return String(value ?? "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#039;");
        }

        function formatTime(ms) {
          if (!ms) return "";
          return new Date(ms).toLocaleString();
        }

        function makeDir(name, path, parent) {
          return { name, path, type: "tree", children: {}, parent };
        }

        function buildTree(items) {
          const root = makeDir("/", "", null);
          for (const item of items || []) {
            if (!item.path) continue;
            const parts = item.path.split("/");
            let cur = root;
            for (let i = 0; i < parts.length; i++) {
              const name = parts[i];
              const path = parts.slice(0, i + 1).join("/");
              const isLeaf = i === parts.length - 1;
              const type = isLeaf ? item.type : "tree";
              if (!cur.children[name]) {
                cur.children[name] = type === "tree"
                  ? makeDir(name, path, cur)
                  : { name, path, type: "blob", size: item.size || 0, parent: cur };
              }
              cur = cur.children[name];
            }
          }
          return root;
        }

        function sortedChildren(node) {
          return Object.values(node.children || {}).sort((a, b) => {
            if (a.type !== b.type) return a.type === "tree" ? -1 : 1;
            return a.name.localeCompare(b.name);
          });
        }

        function renderTreeNode(node, depth) {
          const isDir = node.type === "tree";
          const value = isDir && node.path ? `${node.path}/` : node.path;
          const icon = isDir ? "📁" : "📄";
          const label = node.path ? node.name : "Repository root";
          const children = isDir
            ? `<div class="tree-children">${sortedChildren(node).map(child => renderTreeNode(child, depth + 1)).join("")}</div>`
            : "";
          return `
            <div class="tree-node" data-path="${escapeHtml(value)}" data-kind="${isDir ? "tree" : "blob"}">
              <div class="tree-row" style="padding-left:${depth === 0 ? 0 : 2}px;">
                <input type="checkbox" data-path="${escapeHtml(value)}" data-kind="${isDir ? "tree" : "blob"}" onchange="onTreeCheck(this)" />
                <span>${icon}</span>
                <span>${escapeHtml(label)}</span>
                ${!isDir && node.size ? `<span class="muted-small">${escapeHtml(node.size)} bytes</span>` : ""}
              </div>
              ${children}
            </div>
          `;
        }

        function directCheckbox(nodeEl) {
          return nodeEl.querySelector(":scope > .tree-row input[type=checkbox]");
        }

        function childrenContainer(nodeEl) {
          return nodeEl.querySelector(":scope > .tree-children");
        }

        function directChildNodes(nodeEl) {
          const container = childrenContainer(nodeEl);
          if (!container) return [];
          return Array.from(container.children).filter(el => el.classList.contains("tree-node"));
        }

        function parentTreeNode(nodeEl) {
          const parentChildren = nodeEl.parentElement;
          if (!parentChildren || !parentChildren.classList.contains("tree-children")) return null;
          return parentChildren.parentElement && parentChildren.parentElement.classList.contains("tree-node")
            ? parentChildren.parentElement
            : null;
        }

        function setDescendants(nodeEl, checked) {
          for (const child of directChildNodes(nodeEl)) {
            const cb = directCheckbox(child);
            if (cb) {
              cb.checked = checked;
              cb.indeterminate = false;
            }
            setDescendants(child, checked);
          }
        }

        function refreshParentState(nodeEl) {
          const cb = directCheckbox(nodeEl);
          const children = directChildNodes(nodeEl);
          if (!cb || children.length === 0) return;

          const childBoxes = children.map(directCheckbox).filter(Boolean);
          const allChecked = childBoxes.every(x => x.checked && !x.indeterminate);
          const noneChecked = childBoxes.every(x => !x.checked && !x.indeterminate);

          if (allChecked) {
            cb.checked = true;
            cb.indeterminate = false;
          } else if (noneChecked) {
            cb.checked = false;
            cb.indeterminate = false;
          } else {
            cb.checked = false;
            cb.indeterminate = true;
          }
        }

        function updateAncestors(nodeEl) {
          let parent = parentTreeNode(nodeEl);
          while (parent) {
            refreshParentState(parent);
            parent = parentTreeNode(parent);
          }
        }

        function onTreeCheck(cb) {
          const nodeEl = cb.closest(".tree-node");
          cb.indeterminate = false;
          setDescendants(nodeEl, cb.checked);
          updateAncestors(nodeEl);
        }

        function hasFullyCheckedAncestor(nodeEl) {
          let parent = parentTreeNode(nodeEl);
          while (parent) {
            const cb = directCheckbox(parent);
            if (cb && cb.checked && !cb.indeterminate) return true;
            parent = parentTreeNode(parent);
          }
          return false;
        }

        function selectedManifest() {
          const include = [];
          const exclude = [];
          const nodes = Array.from(document.querySelectorAll("#repo-tree .tree-node"));
          for (const node of nodes) {
            const cb = directCheckbox(node);
            if (!cb) continue;
            if (cb.checked && !cb.indeterminate && !hasFullyCheckedAncestor(node)) {
              include.push(cb.dataset.path || "");
            }
          }
          return { include_paths: include, exclude_paths: exclude };
        }

        async function loadTree() {
          const repo = document.getElementById("repo").value.trim();
          const ref = document.getElementById("ref").value.trim() || "main";
          const message = document.getElementById("seed-message");
          const target = document.getElementById("repo-tree");

          if (!repo) {
            message.textContent = "Enter a repository first.";
            return;
          }

          message.textContent = "Loading repository tree...";
          target.innerHTML = `<p>Loading ${escapeHtml(repo)}...</p>`;

          const res = await fetch(`/api/repos/tree?repo=${encodeURIComponent(repo)}&ref=${encodeURIComponent(ref)}`);
          const data = await res.json();

          if (!res.ok) {
            target.innerHTML = `<p>${escapeHtml(data.detail || "Unable to load tree.")}</p>`;
            message.textContent = data.detail || "Unable to load tree.";
            return;
          }

          treeRoot = buildTree(data.items || []);
          target.innerHTML = renderTreeNode(treeRoot, 0);
          message.textContent = data.truncated
            ? `Loaded ${data.items.length} tree items, but GitHub marked the tree as truncated. Narrow the repo/ref if needed.`
            : `Loaded ${data.items.length} tree items.`;
          await loadIndexes();
        }

        async function queueSeed() {
          const repo = document.getElementById("repo").value.trim();
          const ref = document.getElementById("ref").value.trim() || "main";
          const mode = document.getElementById("mode").value;
          const message = document.getElementById("seed-message");
          const manifest = selectedManifest();

          if (!repo) {
            message.textContent = "Enter a repository first.";
            return;
          }
          if (!manifest.include_paths.length) {
            message.textContent = "Select at least one file or directory in the tree.";
            return;
          }

          message.textContent = "Queueing Cognee reseed...";
          const res = await fetch("/api/cognee/reseed", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({ repo, ref, mode, ...manifest })
          });
          const data = await res.json();

          if (!res.ok) {
            message.textContent = data.detail || "Unable to queue Cognee reseed.";
            return;
          }

          message.textContent = `Queued Cognee job ${data.index_job_id} for scope ${data.scope_hash}.`;
          await loadIndexes();
        }

        function renderIndexes(rows) {
          const target = document.getElementById("indexes-table");
          if (!rows || rows.length === 0) {
            target.innerHTML = `<p>No Cognee indexes for this repo yet.</p>`;
            return;
          }
          target.innerHTML = `
            <div style="overflow-x:auto;">
              <table style="width:100%; border-collapse: collapse;">
                <thead>
                  <tr>
                    <th style="text-align:left; padding:10px; border-bottom:1px solid var(--border);">Status</th>
                    <th style="text-align:left; padding:10px; border-bottom:1px solid var(--border);">Scope</th>
                    <th style="text-align:left; padding:10px; border-bottom:1px solid var(--border);">Mode</th>
                    <th style="text-align:left; padding:10px; border-bottom:1px solid var(--border);">Files</th>
                    <th style="text-align:left; padding:10px; border-bottom:1px solid var(--border);">Updated</th>
                  </tr>
                </thead>
                <tbody>
                  ${rows.map(r => `
                    <tr>
                      <td style="padding:10px; border-bottom:1px solid var(--border);"><span class="status">${escapeHtml(r.status)}</span></td>
                      <td style="padding:10px; border-bottom:1px solid var(--border);"><code>${escapeHtml(r.scope_hash)}</code><br><span class="muted-small">${escapeHtml(r.dataset_name)}</span></td>
                      <td style="padding:10px; border-bottom:1px solid var(--border);">${escapeHtml(r.effective_mode || "queued")}</td>
                      <td style="padding:10px; border-bottom:1px solid var(--border);">${escapeHtml(r.selected_file_count)} selected<br><span class="muted-small">${escapeHtml(r.changed_count)} changed, ${escapeHtml(r.removed_count)} removed</span></td>
                      <td style="padding:10px; border-bottom:1px solid var(--border);">${escapeHtml(formatTime(r.last_indexed_at || r.updated_at || r.created_at))}</td>
                    </tr>
                  `).join("")}
                </tbody>
              </table>
            </div>
          `;
        }

        async function loadIndexes() {
          const repo = document.getElementById("repo").value.trim();
          if (!repo) return;
          const res = await fetch(`/api/cognee/indexes?repo=${encodeURIComponent(repo)}`);
          const data = await res.json();
          if (!res.ok) {
            document.getElementById("indexes-table").innerHTML = `<p>${escapeHtml(data.detail || "Unable to load indexes.")}</p>`;
            return;
          }
          renderIndexes(data.indexes || []);
        }

        const initialRepo = new URLSearchParams(window.location.search).get("repo") || "";
        if (initialRepo) {
          document.getElementById("repo").value = initialRepo;
          loadIndexes();
        }

        setInterval(() => {
          if (document.getElementById("repo").value.trim()) loadIndexes();
        }, 3000);
      </script>
    """
    return shell("Cognee Ops | Cognimoss", coding_agent_sidebar("/applications/cognee-ops", body), app_nav=True)



@app.get("/applications/repo-chat", response_class=HTMLResponse)
def repo_chat_page(request: Request) -> str:
    body = """
      <style>
        .chat-shell {
          display: grid;
          grid-template-rows: auto minmax(360px, 1fr) auto;
          gap: 14px;
        }
        .chat-topline {
          display: grid;
          grid-template-columns: minmax(220px, 1fr) auto;
          gap: 12px;
          align-items: end;
        }
        .chat-window {
          border: 1px solid var(--border);
          border-radius: 18px;
          background: rgba(2, 6, 23, 0.42);
          min-height: 420px;
          max-height: 620px;
          overflow: auto;
          padding: 18px;
        }
        .chat-empty {
          color: var(--muted);
          text-align: center;
          margin-top: 140px;
        }
        .bubble-row {
          display: flex;
          margin: 12px 0;
        }
        .bubble-row.user {
          justify-content: flex-end;
        }
        .bubble-row.assistant {
          justify-content: flex-start;
        }
        .bubble {
          max-width: min(760px, 82%);
          border: 1px solid var(--border);
          border-radius: 18px;
          padding: 14px 16px;
          white-space: pre-wrap;
          line-height: 1.55;
        }
        .bubble-row.user .bubble {
          background: rgba(14, 116, 144, 0.24);
          border-color: rgba(125, 211, 252, 0.4);
        }
        .bubble-row.assistant .bubble {
          background: rgba(15, 23, 42, 0.72);
        }
        .bubble-label {
          display: block;
          color: var(--muted);
          font-size: 12px;
          margin-bottom: 6px;
          text-transform: uppercase;
          letter-spacing: 0.08em;
        }
        .chat-compose {
          border: 1px solid var(--border);
          border-radius: 18px;
          padding: 14px;
          background: rgba(2, 6, 23, 0.35);
        }
        .chat-compose-row {
          display: grid;
          grid-template-columns: minmax(0, 1fr) auto;
          gap: 12px;
          align-items: end;
        }
        .scope-row {
          display: grid;
          grid-template-columns: minmax(220px, 1fr) auto;
          gap: 10px;
          align-items: end;
          margin-top: 10px;
        }
        .scope-row label {
          margin-top: 0;
        }
        @media (max-width: 780px) {
          .chat-topline,
          .chat-compose-row,
          .scope-row {
            grid-template-columns: 1fr;
          }
          .bubble {
            max-width: 94%;
          }
        }
      </style>

      <section class="hero">
        <span class="eyebrow">Repo Chat</span>
        <h1>Talk with repository context</h1>
        <p>
          Ask direct questions against the selected repository. The chat view only
          displays the latest three exchanges to keep the conversation focused.
        </p>
      </section>

      <section class="card chat-shell">
        <div class="chat-topline">
          <div>
            <label>GitHub repository</label>
            <input id="chat-repo" placeholder="owner/repo" />
          </div>
          <button class="secondary" onclick="loadChatScopes()">Refresh scopes</button>
        </div>

        <div id="chat-window" class="chat-window">
          <div class="chat-empty">Choose a repository and ask a question.</div>
        </div>

        <div class="chat-compose">
          <div class="chat-compose-row">
            <div>
              <label>Message</label>
              <textarea id="chat-message" rows="3" placeholder="Ask about architecture, files, tests, bugs..."></textarea>
            </div>
            <button onclick="sendChat()">Send</button>
          </div>

          <div class="scope-row">
            <div>
              <label>Cognee context scope</label>
              <select id="chat-scope">
                <option value="">Use latest ready scope for this repo</option>
              </select>
            </div>
            <p id="chat-status"></p>
          </div>
        </div>
      </section>

      <script>
        const chatMessages = [];

        function escapeHtml(value) {
          return String(value ?? "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#039;");
        }

        function queryParam(name) {
          return new URLSearchParams(window.location.search).get(name) || "";
        }

        function recentChatMessages() {
          // Keep only the latest three user/assistant exchanges visible.
          return chatMessages.slice(-6);
        }

        function renderChat() {
          const target = document.getElementById("chat-window");
          const recent = recentChatMessages();

          if (!recent.length) {
            target.innerHTML = `<div class="chat-empty">Choose a repository and ask a question.</div>`;
            return;
          }

          target.innerHTML = recent.map(m => `
            <div class="bubble-row ${escapeHtml(m.role)}">
              <div class="bubble">
                <span class="bubble-label">${m.role === "user" ? "You" : "Cognimoss"}</span>
                ${escapeHtml(m.content)}
              </div>
            </div>
          `).join("");

          target.scrollTop = target.scrollHeight;
        }

        async function loadChatScopes() {
          const repo = document.getElementById("chat-repo").value.trim();
          const select = document.getElementById("chat-scope");
          const status = document.getElementById("chat-status");

          select.innerHTML = `<option value="">Use latest ready scope for this repo</option>`;

          if (!repo) {
            status.textContent = "Enter a repository first.";
            return;
          }

          status.textContent = "Loading scopes...";
          const res = await fetch(`/api/cognee/indexes?repo=${encodeURIComponent(repo)}`);
          const data = await res.json();

          if (!res.ok) {
            status.textContent = data.detail || "Unable to load scopes.";
            return;
          }

          const ready = (data.indexes || [])
            .filter(i => i.status === "ready")
            .sort((a, b) => Number(b.last_indexed_at || b.updated_at || b.created_at || 0) - Number(a.last_indexed_at || a.updated_at || a.created_at || 0));

          select.innerHTML = `<option value="">Use latest ready scope for this repo</option>` +
            ready.map(i => `
              <option value="${escapeHtml(i.scope_hash)}">
                ${escapeHtml(i.scope_hash)} · ${escapeHtml(i.ref || "main")} · ${escapeHtml(i.selected_file_count || 0)} files
              </option>
            `).join("");

          status.textContent = ready.length
            ? `Loaded ${ready.length} ready scope(s). Blank uses the latest.`
            : "No ready Cognee scopes yet. Blank will use latest when available.";
        }

        async function sendChat() {
          const repo = document.getElementById("chat-repo").value.trim();
          const cognee_scope_hash = document.getElementById("chat-scope").value.trim();
          const message = document.getElementById("chat-message").value.trim();
          const status = document.getElementById("chat-status");

          if (!repo) {
            status.textContent = "Enter a repository first.";
            return;
          }

          if (!message) {
            status.textContent = "Enter a message.";
            return;
          }

          chatMessages.push({role: "user", content: message});
          document.getElementById("chat-message").value = "";
          renderChat();

          status.textContent = "Thinking...";

          const res = await fetch("/api/repo-chat/message", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({repo, cognee_scope_hash, message})
          });

          const data = await res.json();

          if (!res.ok) {
            status.textContent = data.detail || "Chat failed.";
            chatMessages.push({role: "assistant", content: data.detail || "Chat failed."});
            renderChat();
            return;
          }

          status.textContent = data.context_available
            ? "Answered with saved repo context."
            : "Answered without saved repo context.";

          chatMessages.push({role: "assistant", content: data.answer || ""});
          renderChat();
        }

        document.getElementById("chat-repo").addEventListener("change", loadChatScopes);
        document.getElementById("chat-message").addEventListener("keydown", event => {
          if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
            sendChat();
          }
        });

        const initialRepo = queryParam("repo");
        if (initialRepo) {
          document.getElementById("chat-repo").value = initialRepo;
          loadChatScopes();
        }

        renderChat();
      </script>
    """
    return shell("Repo Chat | Cognimoss", coding_agent_sidebar("/applications/repo-chat", body), app_nav=True)



@app.get("/applications/repo-debugger", response_class=HTMLResponse)
def repo_debugger_page(request: Request) -> RedirectResponse:
    # Debugger backend/API logic is preserved, but the UI page is hidden for now.
    return RedirectResponse(url="/applications", status_code=302)


@app.get("/applications/cognee-graph", response_class=HTMLResponse)
def cognee_graph_page(request: Request) -> str:
    body = """
      <style>
        .graph-toolbar {
          display: grid;
          grid-template-columns: minmax(240px, 1fr) minmax(260px, 1fr) auto;
          gap: 12px;
          align-items: end;
        }
        .graph-frame-wrap {
          border: 1px solid var(--border);
          border-radius: 18px;
          overflow: hidden;
          background: rgba(2, 6, 23, 0.1px solid var(--border);
          border-radius: 18px;
          overflow: hidden;
          background: rgba(2, 6, 23, 0.42);
          min-height: 620px;
        }
        .graph-frame {
          width: 100%;
          height: 720px;
          border: 0;
          display: block;
          background: #03140d;
        }
        @media (max-width: 900px) {
          .graph-toolbar {
            grid-template-columns: 1fr;
          }
        }
      </style>

      <section class="hero">
        <span class="eyebrow">Cognee Knowledge Graph</span>
        <h1>Repository graph preview</h1>
        <p>
          Display the selected repository's Cognee graph HTML. In mock mode this
          renders a lightweight placeholder graph from repository/index metadata.
        </p>
      </section>

      <section class="card">
        <div class="graph-toolbar">
          <div>
            <label>GitHub repository</label>
            <input id="graph-repo" placeholder="owner/repo" />
          </div>

          <div>
            <label>Cognee context scope</label>
            <select id="graph-scope">
              <option value="">Use latest ready scope for this repo</option>
            </select>
          </div>

          <button onclick="loadGraph()">Load graph</button>
        </div>

        <p id="graph-status"></p>
      </section>

      <section class="graph-frame-wrap" style="margin-top: 20px;">
        <iframe id="graph-frame" class="graph-frame" title="Cognee knowledge graph"></iframe>
      </section>

      <script>
        function escapeHtml(value) {
          return String(value ?? "")
            .replaceAll("&", "&amp;")
            .replaceAll("<", "&lt;")
            .replaceAll(">", "&gt;")
            .replaceAll('"', "&quot;")
            .replaceAll("'", "&#039;");
        }

        function queryParam(name) {
          return new URLSearchParams(window.location.search).get(name) || "";
        }

        async function loadGraphScopes() {
          const repo = document.getElementById("graph-repo").value.trim();
          const select = document.getElementById("graph-scope");
          const status = document.getElementById("graph-status");

          select.innerHTML = `<option value="">Use latest ready scope for this repo</option>`;

          if (!repo) {
            status.textContent = "Enter a repository first.";
            return;
          }

          status.textContent = "Loading Cognee scopes...";

          const res = await fetch(`/api/cognee/indexes?repo=${encodeURIComponent(repo)}`);
          const data = await res.json();

          if (!res.ok) {
            status.textContent = data.detail || "Unable to load Cognee scopes.";
            return;
          }

          const ready = (data.indexes || [])
            .filter(i => i.status === "ready")
            .sort((a, b) => Number(b.last_indexed_at || b.updated_at || b.created_at || 0) - Number(a.last_indexed_at || a.updated_at || a.created_at || 0));

          select.innerHTML = `<option value="">Use latest ready scope for this repo</option>` +
            ready.map(i => `
              <option value="${escapeHtml(i.scope_hash)}">
                ${escapeHtml(i.scope_hash)} · ${escapeHtml(i.ref || "main")} · ${escapeHtml(i.selected_file_count || 0)} files
              </option>
            `).join("");

          status.textContent = ready.length
            ? `Loaded ${ready.length} ready scope(s). Blank uses latest.`
            : "No ready Cognee scopes found. Mock graph can still render a placeholder.";
        }

        function loadGraph() {
          const repo = document.getElementById("graph-repo").value.trim();
          const scope = document.getElementById("graph-scope").value.trim();
          const status = document.getElementById("graph-status");
          const frame = document.getElementById("graph-frame");

          if (!repo) {
            status.textContent = "Enter a repository first.";
            return;
          }

          const url = `/api/cognee/graph-html?repo=${encodeURIComponent(repo)}&scope_hash=${encodeURIComponent(scope)}`;
          frame.src = url;
          status.textContent = scope
            ? `Loading graph for scope ${scope}...`
            : "Loading graph using latest ready scope...";
        }

        document.getElementById("graph-repo").addEventListener("change", async () => {
          await loadGraphScopes();
        });

        const initialRepo = queryParam("repo");
        if (initialRepo) {
          document.getElementById("graph-repo").value = initialRepo;
          loadGraphScopes().then(loadGraph);
        }
      </script>
    """
    return shell("Knowledge Graph | Cognimoss", coding_agent_sidebar("/applications", body), app_nav=True)




@app.get("/apps/git-agent", response_class=HTMLResponse)
def old_git_agent_redirect() -> RedirectResponse:
    return RedirectResponse(url="/applications/git-agent", status_code=302)



@app.get("/api/repos/tree")
def get_repo_tree(repo: str, ref: str = "main") -> dict[str, Any]:
    repo = normalize_repo(repo)
    try:
        ref = normalize_ref(ref)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    if MOCK_BACKEND and not MOCK_USE_GITHUB:
        return mock_backend.sample_repo_tree(repo, ref)

    url = f"https://api.github.com/repos/{repo}/git/trees/{ref}?recursive=1"
    try:
        res = requests.get(url, headers=github_headers(), timeout=30)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"GitHub tree request failed: {e}") from e

    if not res.ok:
        detail = "Unable to load repository tree from GitHub."
        try:
            detail = res.json().get("message") or detail
        except Exception:
            pass
        raise HTTPException(status_code=res.status_code, detail=detail)

    data = res.json()
    raw_items = data.get("tree", []) or []
    items: list[dict[str, Any]] = []

    for item in raw_items:
        path = str(item.get("path") or "")
        kind = str(item.get("type") or "")
        if kind not in {"blob", "tree"} or not path:
            continue
        # Hide protected paths from the selectable seed tree. The indexer also
        # validates again server-side before reading files.
        if not is_safe_relpath(path):
            continue
        items.append(
            {
                "path": path,
                "type": kind,
                "size": int(item.get("size") or 0),
            }
        )

    items.sort(key=lambda x: (x["path"].count("/"), x["path"]))
    limited = items[:MAX_REPO_TREE_ITEMS]

    return {
        "repo": repo,
        "ref": ref,
        "truncated": bool(data.get("truncated")) or len(items) > len(limited),
        "total_items": len(items),
        "items": limited,
    }


@app.post("/api/cognee/reseed")
def create_cognee_reseed(req: CreateCogneeSeedRequest, request: Request) -> dict[str, Any]:
    if not MOCK_BACKEND and not COGNEE_QUEUE_URL:
        raise HTTPException(status_code=500, detail="Cognee queue is not configured.")

    repo = normalize_repo(req.repo)
    try:
        ref = normalize_ref(req.ref)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    mode = (req.mode or "auto").strip().lower()
    if mode not in {"auto", "full", "incremental"}:
        raise HTTPException(status_code=400, detail="Mode must be auto, full, or incremental.")

    include_paths = normalize_scope_paths(req.include_paths)
    exclude_paths = normalize_scope_paths(req.exclude_paths)

    if not include_paths:
        raise HTTPException(status_code=400, detail="Select at least one file or directory to seed.")

    if MOCK_BACKEND:
        return mock_backend.create_cognee_reseed(
            repo=repo,
            ref=ref,
            mode=mode,
            include_paths=include_paths,
            exclude_paths=exclude_paths,
            created_by=current_user_email(request) or "mock-user@cognimoss.local",
        )

    scope = make_scope_hash(repo, ref, include_paths, exclude_paths)
    dataset_info = dataset_names_for_scope(repo, ref, scope)
    dataset_name = dataset_info["dataset_name"]
    code_dataset_name = dataset_info["code_dataset_name"]
    rules_dataset_name = dataset_info["rules_dataset_name"]
    snapshot_key = dataset_info["snapshot_key"]
    index_job_id = str(uuid.uuid4())
    user_email = current_user_email(request)
    ts = now_ms()

    table.put_item(
        Item=to_dynamo_value(
            {
                "pk": index_pk(repo),
                "sk": index_sk(scope),
                "repo": repo,
                "ref": ref,
                "scope_hash": scope,
                "snapshot_key": snapshot_key,
                "dataset_name": dataset_name,
                "code_dataset_name": code_dataset_name,
                "rules_dataset_name": rules_dataset_name,
                "status": "queued",
                "status_message": "Cognee reseed job queued.",
                "requested_mode": mode,
                "include_paths": include_paths,
                "exclude_paths": exclude_paths,
                "last_job_id": index_job_id,
                "created_by": user_email,
                "created_at": ts,
                "updated_at": ts,
            }
        )
    )

    table.put_item(
        Item=to_dynamo_value(
            {
                "pk": f"COGNEE_JOB#{index_job_id}",
                "sk": "META",
                "job_id": index_job_id,
                "repo": repo,
                "ref": ref,
                "scope_hash": scope,
                "snapshot_key": snapshot_key,
                "dataset_name": dataset_name,
                "code_dataset_name": code_dataset_name,
                "rules_dataset_name": rules_dataset_name,
                "status": "queued",
                "status_message": "Cognee reseed job queued.",
                "mode": mode,
                "include_paths": include_paths,
                "exclude_paths": exclude_paths,
                "created_by": user_email,
                "created_at": ts,
                "updated_at": ts,
            }
        )
    )

    put_cognee_job_event(
        index_job_id,
        "queued",
        "Cognee reseed job queued.",
        {
            "repo": repo,
            "ref": ref,
            "scope_hash": scope,
            "snapshot_key": snapshot_key,
            "dataset_name": dataset_name,
            "code_dataset_name": code_dataset_name,
            "rules_dataset_name": rules_dataset_name,
            "mode": mode,
            "include_paths": include_paths,
            "exclude_paths": exclude_paths,
            "created_by": user_email,
        },
    )

    sqs.send_message(
        QueueUrl=COGNEE_QUEUE_URL,
        MessageBody=json.dumps(
            {
                "type": "cognee_reseed",
                "index_job_id": index_job_id,
                "repo": repo,
                "ref": ref,
                "mode": mode,
                "scope_hash": scope,
                "snapshot_key": snapshot_key,
                "dataset_name": dataset_name,
                "code_dataset_name": code_dataset_name,
                "rules_dataset_name": rules_dataset_name,
                "include_paths": include_paths,
                "exclude_paths": exclude_paths,
                "created_by": user_email,
            }
        ),
    )

    return {
        "index_job_id": index_job_id,
        "repo": repo,
        "ref": ref,
        "scope_hash": scope,
        "snapshot_key": snapshot_key,
        "dataset_name": dataset_name,
        "code_dataset_name": code_dataset_name,
        "rules_dataset_name": rules_dataset_name,
        "status": "queued",
    }


def render_cognee_graph_document(repo: str, selected_index: dict[str, Any] | None, indexes: list[dict[str, Any]]) -> str:
    safe_repo = html.escape(repo)
    scope = html.escape(str((selected_index or {}).get("scope_hash") or "latest"))
    ref = html.escape(str((selected_index or {}).get("ref") or "main"))
    status = html.escape(str((selected_index or {}).get("status") or "mock"))
    selected_files = int((selected_index or {}).get("selected_file_count") or 0)
    changed = int((selected_index or {}).get("changed_count") or 0)
    removed = int((selected_index or {}).get("removed_count") or 0)
    index_count = len(indexes)

    # Frontend scaffold only:
    # Replace this function later with the real Cognee graph HTML export/read path.
    nodes = [
        ("Repository", safe_repo, 410, 90),
        ("Code dataset", html.escape(str((selected_index or {}).get("code_dataset_name") or "code graph")), 210, 250),
        ("Rules dataset", html.escape(str((selected_index or {}).get("rules_dataset_name") or "rules graph")), 610, 250),
        ("Files", f"{selected_files} selected", 210, 430),
        ("Scope", scope, 610, 430),
    ]

    node_html = ""
    for title, detail, x, y in nodes:
        node_html += f"""
          <g>
            <rect x="{x - 115}" y="{y - 42}" width="230" height="84" rx="18" class="node"/>
            <text x="{x}" y="{y - 8}" text-anchor="middle" class="node-title">{title}</text>
            <text x="{x}" y="{y + 18}" text-anchor="middle" class="node-detail">{detail}</text>
          </g>
        """

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <style>
    :root {{
      color-scheme: dark;
      --bg: #03140d;
      --panel: #062016;
      --border: #1f4f35;
      --text: #e6f4ea;
      --muted: #9bbfa3;
      --accent: #34d399;
    }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at 20% 20%, rgba(52, 211, 153, 0.14), transparent 28%),
        radial-gradient(circle at 80% 10%, rgba(45, 212, 191, 0.10), transparent 24%),
        var(--bg);
      color: var(--text);
    }}
    .wrap {{ padding: 28px; }}
    .top {{
      display: flex;
      justify-content: space-between;
      gap: 18px;
      align-items: flex-start;
      margin-bottom: 20px;
    }}
    h1 {{ margin: 0 0 8px; font-size: 28px; }}
    p {{ color: var(--muted); margin: 0; line-height: 1.5; }}
    .pill {{
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 8px 12px;
      background: rgba(6, 32, 22, 0.72);
      color: var(--accent);
      white-space: nowrap;
    }}
    .panel {{
      border: 1px solid var(--border);
      background: rgba(6, 32, 22, 0.72);
      border-radius: 22px;
      padding: 18px;
      overflow: hidden;
    }}
    svg {{ width: 100%; min-height: 540px; }}
    .edge {{
      stroke: rgba(52, 211, 153, 0.55);
      stroke-width: 2;
      fill: none;
    }}
    .node {{
      fill: rgba(8, 40, 27, 0.94);
      stroke: rgba(52, 211, 153, 0.74);
      stroke-width: 1.5;
      filter: drop-shadow(0 12px 22px rgba(0,0,0,0.28));
    }}
    .node-title {{
      fill: var(--text);
      font-size: 15px;
      font-weight: 700;
    }}
    .node-detail {{
      fill: var(--muted);
      font-size: 12px;
    }}
    .stats {{
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-top: 16px;
    }}
    .stat {{
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 14px;
      background: rgba(3, 20, 13, 0.7);
    }}
    .stat strong {{ display: block; color: var(--accent); font-size: 22px; }}
    .stat span {{ color: var(--muted); font-size: 12px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <div>
        <h1>Cognee Knowledge Graph</h1>
        <p>{safe_repo} · ref {ref} · scope {scope}</p>
      </div>
      <div class="pill">{status}</div>
    </div>

    <div class="panel">
      <svg viewBox="0 0 820 560" role="img" aria-label="Mock Cognee knowledge graph">
        <path d="M410 132 C350 178 270 195 210 208" class="edge"/>
        <path d="M410 132 C470 178 550 195 610 208" class="edge"/>
        <path d="M210 292 C210 340 210 375 210 388" class="edge"/>
        <path d="M610 292 C610 340 610 375 610 388" class="edge"/>
        <path d="M325 430 C410 470 495 470 610 430" class="edge"/>
        {node_html}
      </svg>

      <div class="stats">
        <div class="stat"><strong>{selected_files}</strong><span>selected files</span></div>
        <div class="stat"><strong>{changed}</strong><span>changed files</span></div>
        <div class="stat"><strong>{removed}</strong><span>removed files</span></div>
        <div class="stat"><strong>{index_count}</strong><span>known scopes</span></div>
      </div>
    </div>
  </div>
</body>
</html>"""


@app.get("/api/cognee/graph-html", response_class=HTMLResponse)
def cognee_graph_html(repo: str, scope_hash: str = "") -> HTMLResponse:
    repo = normalize_repo(repo)
    scope_hash = (scope_hash or "").strip()

    if MOCK_BACKEND:
        index_resp = mock_backend.list_cognee_indexes(repo)
        indexes = index_resp.get("indexes", [])
    else:
        resp = table.query(KeyConditionExpression=Key("pk").eq(index_pk(repo)))
        indexes = [clean_index_item(i) for i in resp.get("Items", [])]

    indexes = sorted(
        indexes,
        key=lambda x: int(x.get("last_indexed_at") or x.get("updated_at") or x.get("created_at") or 0),
        reverse=True,
    )

    selected = None
    if scope_hash:
        selected = next((i for i in indexes if str(i.get("scope_hash") or "") == scope_hash), None)
    if selected is None:
        selected = next((i for i in indexes if str(i.get("status") or "") == "ready"), None)
    if selected is None and indexes:
        selected = indexes[0]

    return HTMLResponse(render_cognee_graph_document(repo, selected, indexes))




@app.get("/api/cognee/indexes")
def list_cognee_indexes(repo: str) -> dict[str, Any]:
    repo = normalize_repo(repo)
    if MOCK_BACKEND:
        return mock_backend.list_cognee_indexes(repo)
    resp = table.query(KeyConditionExpression=Key("pk").eq(index_pk(repo)))
    items = [clean_index_item(i) for i in resp.get("Items", [])]
    items.sort(key=lambda x: int(x.get("last_indexed_at") or x.get("updated_at") or x.get("created_at") or 0), reverse=True)
    return {"repo": repo, "indexes": items}


@app.get("/api/cognee/jobs/{job_id}/events")
def get_cognee_job_events(job_id: str) -> dict[str, Any]:
    if MOCK_BACKEND:
        return mock_backend.get_cognee_job_events(job_id)
    resp = table.query(
        KeyConditionExpression=Key("pk").eq(f"COGNEE_JOB#{job_id}") & Key("sk").begins_with("EVENT#")
    )
    events = resp.get("Items", [])
    events.sort(key=lambda x: str(x.get("sk")))
    return {
        "job_id": job_id,
        "events": [
            {
                "ts": int(e.get("ts", 0)),
                "event_type": e.get("event_type"),
                "message": e.get("message"),
                "payload": e.get("payload", {}),
            }
            for e in events
        ],
    }



@app.post("/api/repo-chat/message")
def repo_chat_message(req: RepoChatRequest, request: Request) -> dict[str, Any]:
    repo = normalize_repo(req.repo)
    message = (req.message or "").strip()

    if not message:
        raise HTTPException(status_code=400, detail="Message is required.")

    requested_scope_hash = (req.cognee_scope_hash or "").strip()
    selected_index = latest_ready_cognee_index(repo, requested_scope_hash)

    if requested_scope_hash and not selected_index:
        raise HTTPException(status_code=400, detail="Selected Cognee scope is not ready.")

    scope_hash = selected_index.get("scope_hash", "") if selected_index else requested_scope_hash
    user_email = current_user_email(request) or "anonymous"

    if MOCK_BACKEND:
        return mock_backend.answer_repo_chat(
            user_email=user_email,
            repo=repo,
            scope_hash=scope_hash,
            message=message,
            selected_index=selected_index,
        )

    try:
        return answer_repo_chat(
            user_email=user_email,
            repo=repo,
            scope_hash=scope_hash,
            message=message,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/debug/runs")
def create_debug_run(req: DebugRunRequest, request: Request) -> dict[str, Any]:
    if not MOCK_BACKEND and not DEBUG_QUEUE_URL:
        raise HTTPException(status_code=500, detail="Debug queue is not configured.")

    repo = normalize_repo(req.repo)

    try:
        ref = normalize_ref(req.ref)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    requested_scope_hash = (req.cognee_scope_hash or "").strip()
    selected_index = latest_ready_cognee_index(repo, requested_scope_hash)

    if requested_scope_hash and not selected_index:
        raise HTTPException(status_code=400, detail="Selected Cognee scope is not ready.")

    if MOCK_BACKEND:
        return mock_backend.create_debug_run(
            repo=repo,
            ref=ref,
            created_by=current_user_email(request) or "mock-user@cognimoss.local",
            selected_index=selected_index,
        )

    debug_run_id = str(uuid.uuid4())
    user_email = current_user_email(request)
    ts = now_ms()

    item = {
        "pk": f"DEBUG_RUN#{debug_run_id}",
        "sk": "META",
        "debug_run_id": debug_run_id,
        "status": "queued",
        "repo": repo,
        "ref": ref,
        "created_by": user_email,
        "created_at": Decimal(ts),
        "updated_at": Decimal(ts),
    }

    if selected_index:
        item["cognee_scope_hash"] = selected_index.get("scope_hash", "")
        item["cognee_snapshot_key"] = selected_index.get("snapshot_key", "")
        item["cognee_dataset_name"] = selected_index.get("dataset_name", "")
        item["cognee_code_dataset_name"] = selected_index.get("code_dataset_name", selected_index.get("dataset_name", ""))
        item["cognee_rules_dataset_name"] = selected_index.get("rules_dataset_name", "")

    table.put_item(Item=item)

    table.put_item(
        Item={
            "pk": f"DEBUG_RUN#{debug_run_id}",
            "sk": f"EVENT#{ts}",
            "debug_run_id": debug_run_id,
            "ts": Decimal(ts),
            "event_type": "queued",
            "message": "Debug run was queued.",
            "payload": {
                "repo": repo,
                "ref": ref,
                "created_by": user_email,
            },
        }
    )

    sqs.send_message(
        QueueUrl=DEBUG_QUEUE_URL,
        MessageBody=json.dumps(
            {
                "type": "repo_debug",
                "debug_run_id": debug_run_id,
                "repo": repo,
                "ref": ref,
                "created_by": user_email,
                "cognee_scope_hash": selected_index.get("scope_hash", "") if selected_index else "",
                "cognee_snapshot_key": selected_index.get("snapshot_key", "") if selected_index else "",
                "cognee_dataset_name": selected_index.get("dataset_name", "") if selected_index else "",
                "cognee_code_dataset_name": selected_index.get("code_dataset_name", selected_index.get("dataset_name", "")) if selected_index else "",
                "cognee_rules_dataset_name": selected_index.get("rules_dataset_name", "") if selected_index else "",
            }
        ),
    )

    return {
        "debug_run_id": debug_run_id,
        "status": "queued",
    }


@app.get("/api/debug/runs/{debug_run_id}")
def get_debug_run(debug_run_id: str) -> dict[str, Any]:
    if MOCK_BACKEND:
        result = mock_backend.get_debug_run(debug_run_id)
        if not result:
            raise HTTPException(status_code=404, detail="Debug run not found.")
        return result

    meta = table.get_item(Key={"pk": f"DEBUG_RUN#{debug_run_id}", "sk": "META"}).get("Item")
    if not meta:
        raise HTTPException(status_code=404, detail="Debug run not found.")

    resp = table.query(
        KeyConditionExpression=Key("pk").eq(f"DEBUG_RUN#{debug_run_id}") & Key("sk").begins_with("EVENT#")
    )
    events = resp.get("Items", [])
    events.sort(key=lambda item: str(item.get("sk") or ""))

    return {
        "debug_run_id": debug_run_id,
        "repo": meta.get("repo", ""),
        "ref": meta.get("ref", "main"),
        "cognee_scope_hash": meta.get("cognee_scope_hash", ""),
        "status": meta.get("status", "unknown"),
        "summary": meta.get("status_message", ""),
        "created_at": int(meta.get("created_at", 0)),
        "updated_at": int(meta.get("updated_at", 0)),
        "suite_count": int(meta.get("suite_count", 0)),
        "failed_suite_count": int(meta.get("failed_suite_count", 0)),
        "issue_urls": meta.get("issue_urls", []),
        "events": [
            {
                "ts": int(event.get("ts", 0)),
                "event_type": event.get("event_type"),
                "message": event.get("message"),
                "payload": event.get("payload", {}),
            }
            for event in events
        ],
        "mock": False,
    }


@app.post("/api/runs")
def create_run(req: CreateRunRequest, request: Request) -> dict[str, Any]:
    if not MOCK_BACKEND and not AGENT_QUEUE_URL:
        raise HTTPException(status_code=500, detail="Agent queue is not configured.")

    repo = normalize_repo(req.repo)
    title = (req.title or "").strip()

    if req.issue_number <= 0:
        raise HTTPException(status_code=400, detail="Issue number must be greater than zero.")

    if not title:
        raise HTTPException(status_code=400, detail="Run title is required.")

    requested_scope_hash = (req.cognee_scope_hash or "").strip()
    selected_index = latest_ready_cognee_index(repo, requested_scope_hash)

    if requested_scope_hash and not selected_index:
        raise HTTPException(
            status_code=400,
            detail="Selected Cognee scope is not ready. Queue or finish a reseed first, or leave the scope blank to run without a specific scope.",
        )

    user_email = current_user_email(request) or "mock-user@cognimoss.local"

    if MOCK_BACKEND:
        return mock_backend.create_run(
            repo=repo,
            issue_number=req.issue_number,
            title=title,
            created_by=user_email,
            selected_index=selected_index,
        )
    run_id = str(uuid.uuid4())
    ts = now_ms()

    item = {
        "pk": f"RUN#{run_id}",
        "sk": "META",
        "run_id": run_id,
        "status": "queued",
        "repo": repo,
        "issue_number": Decimal(req.issue_number),
        "title": title,
        "created_by": user_email,
        "created_at": Decimal(ts),
        "updated_at": Decimal(ts),
    }

    if selected_index:
        item["cognee_scope_hash"] = selected_index.get("scope_hash", "")
        item["cognee_snapshot_key"] = selected_index.get("snapshot_key", "")
        item["cognee_dataset_name"] = selected_index.get("dataset_name", "")
        item["cognee_code_dataset_name"] = selected_index.get("code_dataset_name", selected_index.get("dataset_name", ""))
        item["cognee_rules_dataset_name"] = selected_index.get("rules_dataset_name", "")

    table.put_item(Item=item)

    put_event(
        run_id=run_id,
        event_type="queued",
        message="Issue was added to the agent queue.",
        payload={
            "repo": repo,
            "issue_number": req.issue_number,
            "title": title,
            "created_by": user_email,
            "cognee_scope_hash": selected_index.get("scope_hash", "") if selected_index else "",
            "cognee_snapshot_key": selected_index.get("snapshot_key", "") if selected_index else "",
            "cognee_dataset_name": selected_index.get("dataset_name", "") if selected_index else "",
            "cognee_code_dataset_name": selected_index.get("code_dataset_name", selected_index.get("dataset_name", "")) if selected_index else "",
            "cognee_rules_dataset_name": selected_index.get("rules_dataset_name", "") if selected_index else "",
        },
    )

    sqs.send_message(
        QueueUrl=AGENT_QUEUE_URL,
        MessageBody=json.dumps({
            "run_id": run_id,
            "repo": repo,
            "issue_number": req.issue_number,
            "title": title,
            "created_by": user_email,
            "cognee_scope_hash": selected_index.get("scope_hash", "") if selected_index else "",
            "cognee_snapshot_key": selected_index.get("snapshot_key", "") if selected_index else "",
            "cognee_dataset_name": selected_index.get("dataset_name", "") if selected_index else "",
            "cognee_code_dataset_name": selected_index.get("code_dataset_name", selected_index.get("dataset_name", "")) if selected_index else "",
            "cognee_rules_dataset_name": selected_index.get("rules_dataset_name", "") if selected_index else "",
        }),
    )

    return {
        "run_id": run_id,
        "status": "queued",
        "cognee_scope_hash": selected_index.get("scope_hash", "") if selected_index else "",
        "cognee_snapshot_key": selected_index.get("snapshot_key", "") if selected_index else "",
        "cognee_dataset_name": selected_index.get("dataset_name", "") if selected_index else "",
        "cognee_code_dataset_name": selected_index.get("code_dataset_name", selected_index.get("dataset_name", "")) if selected_index else "",
        "cognee_rules_dataset_name": selected_index.get("rules_dataset_name", "") if selected_index else "",
    }


@app.get("/api/runs")
def list_runs(status: str | None = None) -> dict[str, Any]:
    if MOCK_BACKEND:
        return mock_backend.list_runs(status)
    expression_values = {":meta": "META"}

    if status:
        expression_values[":status"] = status
        filter_expression = "sk = :meta and #status = :status"
        expression_names = {"#status": "status"}
    else:
        filter_expression = "sk = :meta"
        expression_names = None

    scan_kwargs = {
        "FilterExpression": filter_expression,
        "ExpressionAttributeValues": expression_values,
        "Limit": 50,
    }

    if expression_names:
        scan_kwargs["ExpressionAttributeNames"] = expression_names

    resp = table.scan(**scan_kwargs)

    items = resp.get("Items", [])
    items.sort(key=lambda x: int(x.get("created_at", 0)), reverse=True)

    cleaned = []
    for item in items:
        cleaned.append(
            {
                "run_id": item.get("run_id"),
                "status": item.get("status"),
                "repo": item.get("repo"),
                "issue_number": int(item.get("issue_number", 0)),
                "title": item.get("title", ""),
                "created_by": item.get("created_by", ""),
                "created_at": int(item.get("created_at", 0)),
                "updated_at": int(item.get("updated_at", 0)),
                "cognee_scope_hash": item.get("cognee_scope_hash", ""),
                "cognee_dataset_name": item.get("cognee_dataset_name", ""),
            }
        )

    return {"runs": cleaned}


@app.get("/api/runs/{run_id}/events")
def get_run_events(run_id: str) -> dict[str, Any]:
    if MOCK_BACKEND:
        return mock_backend.get_run_events(run_id)
    resp = table.query(
        KeyConditionExpression="pk = :pk and begins_with(sk, :prefix)",
        ExpressionAttributeValues={
            ":pk": f"RUN#{run_id}",
            ":prefix": "EVENT#",
        },
    )

    events = resp.get("Items", [])
    events.sort(key=lambda x: str(x.get("sk")))

    return {
        "run_id": run_id,
        "events": [
            {
                "ts": int(e.get("ts", 0)),
                "event_type": e.get("event_type"),
                "message": e.get("message"),
                "payload": e.get("payload", {}),
            }
            for e in events
        ],
    }
