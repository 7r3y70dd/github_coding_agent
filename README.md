# Cognimoss

Cognimoss is a repository-aware coding-agent platform that combines:

* a FastAPI web application,
* GitHub repository automation,
* asynchronous worker queues,
* Cognee-backed repository memory,
* LLM-powered planning, coding, chat, and debugging,
* local Ollama inference or Amazon Bedrock,
* and optional PostgreSQL + pgvector persistence.

Cognimoss can run in two primary modes:

1. **Local mode** — Docker Compose, Moto, Ollama, and optional PostgreSQL.
2. **AWS mode** — Elastic Beanstalk, EC2 workers, SQS, DynamoDB, S3, Cognito, and Amazon Bedrock.

The recommended development setup is a normal workstation with an NVIDIA CUDA-capable GPU. A conservative CPU-only configuration is also documented for smaller machines.

---

# Architecture

At a high level:

```text
                         ┌──────────────────────┐
                         │      Browser         │
                         └──────────┬───────────┘
                                    │
                                    ▼
                         ┌──────────────────────┐
                         │ FastAPI Frontend/API │
                         └──────────┬───────────┘
                                    │
                   ┌────────────────┼────────────────┐
                   │                │                │
                   ▼                ▼                ▼
             Agent Queue       Cognee Queue      Debug Queue
                   │                │                │
                   ▼                ▼                ▼
            Agent Worker      Cognee Indexer     Debug Worker
                   │                │                │
                   └──────────┬─────┴────────────────┘
                              │
                    ┌─────────┴─────────┐
                    │                   │
                    ▼                   ▼
               GitHub API          LLM / Embeddings
                                   │
                         ┌─────────┴─────────┐
                         │                   │
                    Local Ollama       Amazon Bedrock
```

State is stored using:

```text
Local:
  AWS API emulation     -> Moto
  Runs/events           -> Moto DynamoDB
  Queues                -> Moto SQS
  Chat context          -> Moto S3
  LLM                   -> Ollama
  Embeddings            -> Ollama
  Cognee vectors        -> LanceDB by default
                          or PostgreSQL + pgvector

AWS:
  Runs/events           -> DynamoDB
  Queues                -> SQS
  Chat context          -> S3
  LLM                   -> Amazon Bedrock
  Embeddings            -> Amazon Bedrock
  Authentication        -> Amazon Cognito
  Web application       -> Elastic Beanstalk
  Background workers    -> EC2
```

---

# Repository Services

The local Docker Compose stack contains these services:

```text
moto
local-init
ollama-check
frontend
agent-worker
cognee-indexer
debug-worker
```

Their responsibilities are:

| Service          | Purpose                                        |
| ---------------- | ---------------------------------------------- |
| `moto`           | Local SQS, DynamoDB, and S3 replacement        |
| `local-init`     | Creates the local queues, table, and bucket    |
| `ollama-check`   | Verifies that containers can reach host Ollama |
| `frontend`       | FastAPI dashboard and API                      |
| `agent-worker`   | Executes coding-agent jobs                     |
| `cognee-indexer` | Creates and maintains repository memory        |
| `debug-worker`   | Runs repository debugging jobs                 |

---

# Requirements

## Nix Development Environment

Cognimoss includes a Nix flake that provides a reproducible development shell with the tools needed to build, run, inspect, and debug the local application.

The flake supports:

- `x86_64-linux`
- `aarch64-linux`

Two development shells are available:

- `default` — portable CPU Ollama
- `cuda` — NVIDIA CUDA-enabled Ollama on `x86_64-linux`

Docker remains the authoritative runtime for the full Cognimoss stack. The Nix shell provides the host-side development tools, Ollama runtime, Python tooling, and `cognimoss-*` helper commands used to manage the project.

### NixOS Is Not Required

You do **not** need to run NixOS to use the Cognimoss development environment.

Cognimoss only requires the **Nix package manager** on a supported Linux system.

The current flake supports:

```text
x86_64-linux
aarch64-linux
```

This means the development shell can be used on distributions such as:

- Ubuntu
- Debian
- Fedora
- Arch Linux
- NixOS
- other compatible Linux distributions

If Nix is not already installed, install the Nix package manager first and enable the `nix-command` and `flakes` experimental features.

Nix instalation command:
```bash
curl --proto '=https' --tlsv1.2 -L https://nixos.org/nix/install | sh -s -- --daemon
```
After Nix is installed, the Cognimoss development environment is entered from the repository with:

```bash
nix develop
```

or, for the NVIDIA CUDA development shell on `x86_64-linux`:

```bash
nix develop .#cuda
```

The project flake then provides the development packages and `cognimoss-*` helper commands described below.

> **Note:** The Nix development shell provides the Docker CLI and Docker Compose, but a working Docker daemon must still be available on the host. Likewise, the flake provides Ollama itself, while host GPU drivers and other hardware-level requirements must already be configured by the operating system.

### Enable Nix Flakes

Your Nix installation must have the following experimental features enabled:

```text
nix-command
flakes
```

On NixOS:

```nix
nix.settings.experimental-features = [
  "nix-command"
  "flakes"
];
```

### Enter the Development Shell

From the repository:

```bash
cd github_coding_agent
```

For the portable CPU environment:

```bash
nix develop
```

For the NVIDIA CUDA environment on `x86_64-linux`:

```bash
nix develop .#cuda
```

The CUDA shell uses `ollama-cuda`.

The current flake is configured for CUDA compute capability:

```text
8.9
```

which corresponds to the project's RTX 4070 Ti / Ada Lovelace development target.

On non-`x86_64-linux` systems, the `cuda` shell falls back to the normal CPU Ollama package.

### Development Tools Provided

The Nix development environment includes:

#### Python

- Python 3.11
- pip
- setuptools
- wheel
- virtualenv
- pytest

#### Git and GitHub

- git
- git-lfs
- GitHub CLI (`gh`)
- OpenSSH

#### Containers

- Docker
- Docker Compose

#### AWS

- AWS CLI v2

#### HTTP, Data, and Debugging

- curl
- wget
- jq
- yq
- ripgrep
- fd
- tree
- lsof
- iproute2

#### Native Build Tools

- GCC
- Make
- CMake
- Ninja
- pkg-config
- patchelf
- Rust
- Cargo

#### Native Libraries

- OpenSSL
- libffi
- zlib
- SQLite
- libxml2
- libxslt
- libjpeg
- libpng

#### Quality and Formatting

- ruff
- shellcheck
- shfmt
- yamllint
- nixfmt

The development shell also configures `LD_LIBRARY_PATH` so pip-installed native extensions can locate common libraries provided by Nix.

### Environment Configured by `nix develop`

Entering the development shell automatically configures the project's Python import path:

```text
cognimoss-core/
backend_package/
frontend_package/
```

The Compose file is exposed as:

```text
COGNIMOSS_COMPOSE_FILE=$PWD/docker-compose.local.yml
```

Ollama models are stored locally under the repository instead of inside the Nix store:

```text
.local/ollama-models/
```

The corresponding environment variable is:

```text
OLLAMA_MODELS=$PWD/.local/ollama-models
```

The shell creates the `.local` directory automatically.

Docker BuildKit is also enabled:

```text
DOCKER_BUILDKIT=1
COMPOSE_DOCKER_CLI_BUILD=1
```

### First-Time Setup

After entering the development shell, create the local environment file:

```bash
cp .env.local.example .env.local
```

Then edit `.env.local` and replace the required secrets and configuration values described later in this README.

Most `cognimoss-*` Docker commands require `.env.local`.

If it does not exist, the helper commands stop and display:

```text
ERROR: .env.local does not exist.
Create it with:
  cp .env.local.example .env.local
```

### Cognimoss Helper Commands

The Nix development shell defines the following project-management commands.

#### `cognimoss-ollama`

Start the host Ollama server so Docker containers can reach it:

```bash
cognimoss-ollama
```

This starts Ollama with:

```text
OLLAMA_HOST=0.0.0.0:11434
```

Cognimoss containers reach the host server through `host.docker.internal`.

> **Security:** Ollama listens on all host interfaces when started this way. Make sure TCP port `11434` is not exposed to untrusted networks.

Keep this process running while using the local Ollama-backed stack.

#### `cognimoss-models`

Pull the local models expected by the development environment:

```bash
cognimoss-models
```

The current flake pulls:

```text
qwen2.5-coder:14b
nomic-embed-text
```

These provide the default local coding/LLM and embedding workloads.

#### `cognimoss-up`

Build the Docker images and start the complete local Cognimoss stack:

```bash
cognimoss-up
```

Equivalent Compose operation:

```bash
docker compose \
  --env-file "$PWD/.env.local" \
  -f "$PWD/docker-compose.local.yml" \
  up -d --build
```

Use this for the initial startup and after changes that require rebuilding Docker images.

#### `cognimoss-ps`

Show all Cognimoss Compose containers, including stopped initialization containers:

```bash
cognimoss-ps
```

Equivalent Compose operation:

```bash
docker compose \
  --env-file "$PWD/.env.local" \
  -f "$PWD/docker-compose.local.yml" \
  ps -a
```

This is one of the first commands to use when diagnosing startup problems.

#### `cognimoss-logs`

Follow logs from the entire stack:

```bash
cognimoss-logs
```

Follow a specific service:

```bash
cognimoss-logs frontend
```

Other useful examples:

```bash
cognimoss-logs cognee-indexer
cognimoss-logs agent-worker
cognimoss-logs debug-worker
```

Additional arguments are passed directly to:

```text
docker compose logs -f
```

#### `cognimoss-check`

Run the project's local verification script:

```bash
cognimoss-check
```

This executes:

```text
scripts/local-check.sh
```

The file must exist and be executable.

If not, the helper reports:

```text
scripts/local-check.sh is missing or not executable.
```

Use this after initial setup and when diagnosing Docker, Moto, Ollama, or local service wiring.

#### `cognimoss-down`

Stop and remove the local Compose containers and network:

```bash
cognimoss-down
```

Equivalent operation:

```bash
docker compose \
  --env-file "$PWD/.env.local" \
  -f "$PWD/docker-compose.local.yml" \
  down
```

Named Docker volumes are preserved.

Use this for a normal shutdown.

#### `cognimoss-reset`

Completely reset the local Compose stack:

```bash
cognimoss-reset
```

Equivalent operation:

```bash
docker compose \
  --env-file "$PWD/.env.local" \
  -f "$PWD/docker-compose.local.yml" \
  down -v
```

> **Warning: `cognimoss-reset` is destructive.**

The `-v` option removes Compose-managed named volumes in addition to containers and networks.

Do not use `cognimoss-reset` as a normal restart command if you want to preserve PostgreSQL, Cognee, Moto, or other volume-backed local state.

Use this instead for normal shutdown:

```bash
cognimoss-down
```

#### `cognimoss-venv`

Create an optional host-side Python virtual environment:

```bash
cognimoss-venv
```

This creates:

```text
.venv/
```

if necessary, activates it, upgrades:

```text
pip
setuptools
wheel
```

and installs:

```text
frontend_package/requirements.txt
backend_package/requirements.txt
```

Docker remains the authoritative full-stack runtime.

The host virtual environment is optional and is useful for direct Python development, testing, editor integration, and debugging outside the containers.

### Command Reference

| Command | Purpose |
|---|---|
| `nix develop` | Enter the portable/CPU Cognimoss development shell |
| `nix develop .#cuda` | Enter the NVIDIA CUDA shell on `x86_64-linux` |
| `cognimoss-ollama` | Start host Ollama on `0.0.0.0:11434` |
| `cognimoss-models` | Pull `qwen2.5-coder:14b` and `nomic-embed-text` |
| `cognimoss-up` | Build and start the complete local Docker stack |
| `cognimoss-ps` | Show all local Cognimoss container states |
| `cognimoss-logs` | Follow logs from the complete stack |
| `cognimoss-logs <service>` | Follow logs from a specific Compose service |
| `cognimoss-check` | Run `scripts/local-check.sh` |
| `cognimoss-down` | Stop the stack while preserving named volumes |
| `cognimoss-reset` | Stop the stack and delete Compose-managed named volumes |
| `cognimoss-venv` | Create and populate the optional host Python `.venv` |

### Typical CPU Startup

For a new local CPU-based installation:

```bash
git clone <YOUR_COGNIMOSS_REPOSITORY_URL>
cd github_coding_agent

nix develop

cp .env.local.example .env.local
$EDITOR .env.local
```

Start Ollama in the first terminal:

```bash
cognimoss-ollama
```

Open a second terminal and enter the development environment again:

```bash
cd github_coding_agent
nix develop
```

Then:

```bash
cognimoss-models
cognimoss-up
cognimoss-ps
cognimoss-check
```

Follow logs with:

```bash
cognimoss-logs
```

### Typical NVIDIA CUDA Startup

On a supported `x86_64-linux` NVIDIA system:

```bash
git clone <YOUR_COGNIMOSS_REPOSITORY_URL>
cd github_coding_agent

nix develop .#cuda

cp .env.local.example .env.local
$EDITOR .env.local
```

Start CUDA-enabled Ollama:

```bash
cognimoss-ollama
```

Then open another terminal:

```bash
cd github_coding_agent
nix develop .#cuda
```

Pull the models and start Cognimoss:

```bash
cognimoss-models
cognimoss-up
cognimoss-check
```

The difference between the two development shells is the Ollama package:

```text
nix develop          -> pkgs.ollama
nix develop .#cuda   -> pkgs.ollama-cuda on x86_64-linux
```

The remainder of the Cognimoss development tooling is shared between both environments.

## Common

Install:

* Git
* Docker
* Docker Compose v2+
* Python 3.11+ for direct development
* Ollama for local inference
* a GitHub account and token

Cognee currently supports Python 3.10 through 3.14.

Clone the repository:

```bash
git clone <YOUR_COGNIMOSS_REPOSITORY_URL>
cd github_coding_agent
```

Verify Docker:

```bash
docker --version
docker compose version
```

---

# Recommended Local Setup: GPU Workstation

This is the preferred local configuration.

The model server runs directly on the host while Cognimoss runs in Docker.

```text
Host
├── Ollama
│   ├── qwen2.5-coder:14b
│   └── nomic-embed-text:latest
│
└── Docker
    ├── frontend
    ├── agent-worker
    ├── cognee-indexer
    ├── debug-worker
    └── Moto
```

Keeping Ollama outside Docker simplifies GPU access and lets every Cognimoss container reach the same inference server.

---

# 1. Configure GPU Support

For an NVIDIA system, first confirm that the driver can see the GPU:

```bash
nvidia-smi
```

Install Ollama:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Ollama supports NVIDIA acceleration on supported NVIDIA hardware and automatically uses available GPU resources when the appropriate driver is installed.

Verify:

```bash
ollama --version
systemctl status ollama
```

---

# 2. Allow Docker to Reach Ollama

Cognimoss containers access Ollama through:

```text
http://host.docker.internal:11434
```

Ollama therefore needs to listen beyond `127.0.0.1`.

For a systemd installation:

```bash
sudo systemctl edit ollama
```

Add:

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart ollama
```

Verify:

```bash
ss -ltnp | grep 11434
```

You should see Ollama listening on port `11434`.

Test:

```bash
curl http://127.0.0.1:11434/api/tags
```

On Linux, `docker-compose.local.yml` should map:

```yaml
extra_hosts:
  - "host.docker.internal:host-gateway"
```

If a host firewall is enabled, allow Docker's bridge network to reach TCP port `11434`.

---

# 3. Download the Default Models

The recommended baseline models are:

```text
Coding / reasoning:  qwen2.5-coder:14b
Embeddings:           nomic-embed-text:latest
```

Pull them:

```bash
ollama pull qwen2.5-coder:14b
ollama pull nomic-embed-text:latest
```

Verify:

```bash
ollama list
```

Nomic is intentionally the default local embedding model because it is small, fast, and works well with local Ollama. Cognee's current Ollama documentation also uses `nomic-embed-text:latest` with 768-dimensional embeddings.

---

# 4. Create the Local Environment File

Copy the template:

```bash
cp .env.local.example .env.local
```

Do not commit `.env.local`.

Add it to `.gitignore` if necessary:

```gitignore
.env.local
.env.aws
.env.production
```

## Values you MUST replace

### `SESSION_SECRET`

Generate one:

```bash
openssl rand -hex 32
```

Then:

```env
SESSION_SECRET=<generated-value>
```

### `GH_TOKEN` and `GITHUB_TOKEN`

Create a fine-grained GitHub token with access only to repositories Cognimoss should modify.

Set both variables to the same token:

```env
GH_TOKEN=<github-token>
GITHUB_TOKEN=<github-token>
```

Both are defined because different tools and subprocesses may look for different conventional token names.

For a coding-agent repository, the token normally needs repository-specific permissions sufficient for the features you enable, such as:

* repository contents,
* pull requests,
* and issues/comments.

Prefer a fine-grained token scoped only to the repositories Cognimoss is allowed to modify.

---

# 5. Local `.env.local` Baseline

The normal GPU development configuration should resemble:

```env
# ============================================================
# Application
# ============================================================

APP_MODE=local
MOCK_BACKEND=0
AUTH_ENABLED=0
COOKIE_HTTPS_ONLY=0
DASHBOARD_ON_DEFAULT_HOST=1

PUBLIC_BASE_URL=http://localhost:8000
APP_BASE_URL=http://localhost:8000

SESSION_SECRET=<REPLACE_ME>


# ============================================================
# Local AWS emulation
# ============================================================

AWS_REGION=us-east-1
AWS_DEFAULT_REGION=us-east-1

AWS_ACCESS_KEY_ID=local
AWS_SECRET_ACCESS_KEY=local
AWS_SESSION_TOKEN=local

AWS_ENDPOINT_URL=http://moto:5000

RUNS_TABLE=agent_runs

CHAT_CONTEXT_BUCKET=cognimoss-local-chat-context
CHAT_CONTEXT_PREFIX=repo-chat-context

AGENT_QUEUE_URL=
COGNEE_QUEUE_URL=
DEBUG_QUEUE_URL=

AGENT_QUEUE_NAME=cognimoss-agent-runs
COGNEE_QUEUE_NAME=cognimoss-cognee-index
DEBUG_QUEUE_NAME=cognimoss-debug-runs


# ============================================================
# GitHub
# ============================================================

GH_TOKEN=<REPLACE_ME>
GITHUB_TOKEN=<REPLACE_ME>

BASE_BRANCH=main
BRANCH_PREFIX=agent/
NO_PR=0


# ============================================================
# Main coding model
# ============================================================

MODEL_PROVIDER=ollama
CHAT_MODEL_PROVIDER=ollama

OLLAMA_URL=http://host.docker.internal:11434

OLLAMA_MODEL=qwen2.5-coder:14b
PLANNER_MODEL=qwen2.5-coder:14b
WRITER_MODEL=qwen2.5-coder:14b
OLLAMA_CHAT_MODEL=qwen2.5-coder:14b

OLLAMA_KEEP_ALIVE=30m

OLLAMA_NUM_CTX=16384
PLANNER_NUM_CTX=16384
WRITER_NUM_CTX=16384
CHAT_NUM_CTX=16384

OLLAMA_NUM_PREDICT=2200
OLLAMA_TEMPERATURE=0.1
OLLAMA_TIMEOUT=1800
OLLAMA_RETRIES=2

PLANNER_THINK=0
WRITER_THINK=0

CHAT_MAX_TOKENS=2200
CHAT_TEMPERATURE=0.1
CHAT_TIMEOUT=1800


# ============================================================
# Cognee
# ============================================================

COGNEE_ENABLED=1
COGNEE_REQUIRED=0

COGNEE_ROOT=/var/lib/agent-runner/cognee
SYSTEM_ROOT_DIRECTORY=/var/lib/agent-runner/cognee/system
DATA_ROOT_DIRECTORY=/var/lib/agent-runner/cognee/data
COGNEE_LOGS_DIR=/var/lib/agent-runner/cognee/logs


# Cognee LLM

LLM_PROVIDER=ollama
LLM_MODEL=qwen2.5-coder:14b
LLM_API_KEY=ollama
LLM_ENDPOINT=http://host.docker.internal:11434/v1
LLM_TEMPERATURE=0.1

COGNEE_LLM_PROVIDER=ollama
COGNEE_LLM_MODEL=qwen2.5-coder:14b
COGNEE_LLM_API_KEY=ollama
COGNEE_LLM_ENDPOINT=http://host.docker.internal:11434/v1


# Cognee embeddings

EMBEDDING_PROVIDER=ollama
EMBEDDING_MODEL=nomic-embed-text:latest
EMBEDDING_API_KEY=ollama
EMBEDDING_ENDPOINT=http://host.docker.internal:11434/api/embed
EMBEDDING_DIMENSIONS=768

COGNEE_EMBEDDING_PROVIDER=ollama
COGNEE_EMBEDDING_MODEL=nomic-embed-text:latest
COGNEE_EMBEDDING_API_KEY=ollama
COGNEE_EMBEDDING_ENDPOINT=http://host.docker.internal:11434/api/embed
COGNEE_EMBEDDING_DIMENSIONS=768

HUGGINGFACE_TOKENIZER=nomic-ai/nomic-embed-text-v1.5


# ============================================================
# Cognee locking
# ============================================================

COGNEE_PROCESS_LOCK=1
COGNEE_LOCK_PATH=/var/lib/agent-runner/cognee/cognee-process.lock

GLOBAL_TASK_LOCK_ENABLED=1
GLOBAL_TASK_LOCK_PATH=/var/lib/agent-runner/cognimoss-global-task.lock


# ============================================================
# Workers
# ============================================================

WORKDIR=/var/lib/agent-runner/work
COGNEE_WORKDIR=/var/lib/cognee-indexer/work
DEBUG_WORKDIR=/var/lib/debug-runner/work

POLL_SECONDS=2

COMMAND_TIMEOUT=900

SQS_VISIBILITY_TIMEOUT=3600
COGNEE_SQS_VISIBILITY_TIMEOUT=14400
DEBUG_SQS_VISIBILITY_TIMEOUT=14400

AGENT_WORKER_SINGLE_JOB=1
COGNEE_INDEXER_SINGLE_JOB=1
DEBUG_WORKER_SINGLE_JOB=1

SKIP_COGNEE_PRESEED=1
ALLOW_AGENT_COGNEE_SEEDING=0
USE_LATEST_COGNEE_INDEX=1


# ============================================================
# Validation
# ============================================================

VALIDATION_PYTHON=/usr/local/bin/python3.11

VALIDATION_CREATE_VENV=1
VALIDATION_INSTALL_PYTEST=1
VALIDATION_INSTALL_REQUIREMENTS=0

DEBUG_INSTALL_PYTEST=1
DEBUG_INSTALL_REQUIREMENTS=0


# ============================================================
# Context limits
# ============================================================

MAX_FILES_TO_READ=8
MAX_FILE_CHARS=12000

COGNEE_MAX_REPO_FILES=160
COGNEE_MAX_RULE_FILES=8
COGNEE_MAX_FILE_CHARS=12000

COGNEE_MAX_CONTEXT_CHUNKS=16
COGNEE_MAX_RULE_CHUNKS=3
COGNEE_MAX_HISTORY_CHUNKS=3

COGNEE_MAX_CONTEXT_CHARS=25000
COGNEE_MAX_CHUNK_CHARS=3500

COGNEE_MAX_SEARCH_QUERIES=6
COGNEE_MAX_SYMBOL_TERMS=6
COGNEE_MAX_HINT_FILES=6
```

---

# 6. Start Cognimoss

Build and start:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  up -d --build
```

Inspect the services:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  ps -a
```

Expected services include:

```text
moto
local-init
ollama-check
frontend
agent-worker
cognee-indexer
debug-worker
```

`local-init` and `ollama-check` are expected to exit successfully after completing their initialization work.

---

# 7. Verify the Application

Check the frontend:

```bash
curl http://127.0.0.1:8000/health
```

Then open:

```text
http://localhost:8000
```

Inspect logs:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  logs -f
```

Or a single service:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  logs -f cognee-indexer
```

---

# 8. Verify Ollama From Inside Docker

The host can reach Ollama:

```bash
curl http://127.0.0.1:11434/api/tags
```

The more important test is Docker -> host Ollama:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  exec -T cognee-indexer \
  curl -sS http://host.docker.internal:11434/api/tags
```

Test Nomic directly:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  exec -T cognee-indexer \
  curl -sS \
    http://host.docker.internal:11434/api/embed \
    -H 'Content-Type: application/json' \
    -d '{
      "model": "nomic-embed-text:latest",
      "input": "Cognimoss embedding test"
    }'
```

---

# Important: Applying `.env.local` Changes

Changing `.env.local` does not modify the environment of an already-running container.

Cognee also loads configuration when the Python process starts, so restart/recreate the affected process after configuration changes.

Use:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  up -d --force-recreate \
  frontend agent-worker cognee-indexer debug-worker
```

Verify what the container actually received:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  exec -T cognee-indexer \
  env | sort | grep -E \
  'EMBEDDING_|LLM_|COGNEE_|DB_|VECTOR_'
```

Also inspect Compose's resolved configuration:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  config
```

This is useful when an environment variable appears to be ignored.

---

# Optional: PostgreSQL + pgvector

Cognee can use its local storage backends without PostgreSQL.

PostgreSQL is recommended when you want:

* a more inspectable database,
* stronger persistence,
* PostgreSQL-backed vector storage,
* easier backup/restore,
* or future multi-user/shared storage.

Cognee officially supports PostgreSQL and pgvector through its `postgres` or `postgres-binary` extras.

## Install Cognee's PostgreSQL dependencies

The backend Python environment should contain:

```bash
pip install "cognee[postgres-binary]"
```

For Docker, normally add the equivalent dependency to the backend requirements and rebuild rather than installing it manually into a running container.

---

## PostgreSQL Compose Service

Add a service similar to:

```yaml
postgres:
  image: pgvector/pgvector:0.8.2-pg16
  container_name: cognimoss-postgres

  environment:
    POSTGRES_DB: ${DB_NAME:-cognee}
    POSTGRES_USER: ${DB_USERNAME:-cognee}
    POSTGRES_PASSWORD: ${DB_PASSWORD}

  volumes:
    - cognimoss-postgres-data:/var/lib/postgresql/data

  ports:
    - "127.0.0.1:5432:5432"

  healthcheck:
    test:
      [
        "CMD-SHELL",
        "pg_isready -U ${DB_USERNAME:-cognee} -d ${DB_NAME:-cognee}"
      ]
    interval: 3s
    timeout: 5s
    retries: 30

  restart: unless-stopped
```

Add the volume:

```yaml
volumes:
  cognimoss-postgres-data:
    name: cognimoss-postgres-data
```

Backend services should wait for PostgreSQL:

```yaml
depends_on:
  postgres:
    condition: service_healthy
```

---

## PostgreSQL Environment Variables

Generate a password:

```bash
openssl rand -hex 32
```

Add:

```env
DB_PROVIDER=postgres
DB_HOST=postgres
DB_PORT=5432
DB_NAME=cognee
DB_USERNAME=cognee
DB_PASSWORD=<REPLACE_ME>

VECTOR_DB_PROVIDER=pgvector
VECTOR_DATASET_DATABASE_HANDLER=pgvector

VECTOR_DB_HOST=postgres
VECTOR_DB_PORT=5432
VECTOR_DB_NAME=cognee
VECTOR_DB_USERNAME=cognee
VECTOR_DB_PASSWORD=<SAME_PASSWORD>
```

Cognee's pgvector adapter can use the same PostgreSQL connection as the relational database.

Start PostgreSQL:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  up -d postgres
```

Verify:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  exec -T postgres \
  pg_isready
```

Verify pgvector:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  exec -T postgres \
  sh -lc '
psql -U "$POSTGRES_USER" \
     -d "$POSTGRES_DB" \
     -c "CREATE EXTENSION IF NOT EXISTS vector;"

psql -U "$POSTGRES_USER" \
     -d "$POSTGRES_DB" \
     -c "\dx"
'
```

---

# Changing Embedding Models or Dimensions

This is important.

A vector store created with:

```text
768 dimensions
```

cannot simply be reused as:

```text
1024 dimensions
```

or:

```text
2560 dimensions
```

without rebuilding the affected vector collections/schema.

Cognee explicitly requires `EMBEDDING_DIMENSIONS` to match the vector-store schema and recommends pruning/rebuilding persisted vector state when changing embedding providers or dimensions.

For example:

```text
nomic-embed-text     -> 768
Titan Embed v2       -> 1024
Qwen3-Embedding-4B   -> up to 2560
```

Do not reuse an old pgvector schema after changing dimensions.

---

## Reset Only the Local PostgreSQL Cognee Database

Stop the workers:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  stop cognee-indexer agent-worker debug-worker
```

Then:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  exec -T postgres sh -lc '
DB="${POSTGRES_DB:-cognee}"
USER="${POSTGRES_USER:-cognee}"

psql -U "$USER" -d postgres -v ON_ERROR_STOP=1 \
  -c "SELECT pg_terminate_backend(pid)
      FROM pg_stat_activity
      WHERE datname = '\''$DB'\''
        AND pid <> pg_backend_pid();"

psql -U "$USER" -d postgres -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE IF EXISTS \"$DB\";"

psql -U "$USER" -d postgres -v ON_ERROR_STOP=1 \
  -c "CREATE DATABASE \"$DB\" OWNER \"$USER\";"

psql -U "$USER" -d "$DB" -v ON_ERROR_STOP=1 \
  -c "CREATE EXTENSION IF NOT EXISTS vector;"
'
```

Then restart:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  up -d cognee-indexer agent-worker debug-worker
```

This is intentionally narrower than deleting all Docker volumes.

Avoid:

```bash
docker compose down -v
```

unless you intentionally want to destroy **all** Compose-managed persistent volumes.

---

# CPU-Only / Small-PC Configuration

The normal GPU configuration above should be used when adequate GPU resources are available.

For CPU-only systems, the main goal is different:

> Keep the models available without allowing simultaneous model workloads to saturate the entire machine.

A tested conservative combination is:

```text
LLM:        qwen2.5-coder:14b
Embedding:  nomic-embed-text:latest
```

Nomic is particularly useful here because its resource footprint is much smaller than large embedding models.

---

## Ollama CPU Profile

Configure Ollama approximately as:

```text
OLLAMA_MAX_LOADED_MODELS=2
OLLAMA_NUM_PARALLEL=1
OLLAMA_MAX_QUEUE=128
OLLAMA_KEEP_ALIVE=10m
OLLAMA_CONTEXT_LENGTH=8192
```

The important distinction is:

```text
MAX_LOADED_MODELS=2
```

allows both the coding model and embedding model to stay resident.

```text
NUM_PARALLEL=1
```

prevents excessive request parallelism for a model.

This avoids repeated:

```text
unload coding model
load embedding model
unload embedding model
load coding model
```

cycles.

Those model swaps can be particularly painful on CPU-only systems.

---

## CPU `.env.local` Changes

Reduce context:

```env
OLLAMA_NUM_CTX=8192
PLANNER_NUM_CTX=8192
WRITER_NUM_CTX=8192
CHAT_NUM_CTX=8192
```

Serialize expensive Cognee work:

```env
DATASET_QUEUE_ENABLED=true
DATASET_QUEUE_MAX_CONCURRENT=1

COGNEE_COGNIFY_CHUNKS_PER_BATCH=1
COGNEE_COGNIFY_DATA_PER_BATCH=1

EMBEDDING_BATCH_SIZE=1

TOKENIZERS_PARALLELISM=false
```

Use moderate chunks:

```env
CHUNK_SIZE=800
CHUNK_OVERLAP=10
COGNEE_COGNIFY_CHUNK_SIZE=800
```

Cognee notes that local Ollama servers can fall behind under embedding load and recommends reducing `EMBEDDING_BATCH_SIZE`; the default is much larger than is appropriate for constrained local hardware.

After the machine is proven stable, cautiously increase:

```text
EMBEDDING_BATCH_SIZE:
1 -> 2 -> 3
```

rather than jumping directly to large batches.

---

# Diagnosing Local Ollama Timeouts

If indexing fails with:

```text
OllamaEmbeddingEngine._get_embedding
TimeoutError
```

the failure is occurring on:

```text
Cognee
   ↓
HTTP request
   ↓
Ollama /api/embed
```

before pgvector receives the embedding.

Test the endpoint directly from the indexer:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  exec -T cognee-indexer \
  sh -c '
time curl -sS \
  --max-time 120 \
  http://host.docker.internal:11434/api/embed \
  -H "Content-Type: application/json" \
  -d '"'"'{
    "model": "nomic-embed-text:latest",
    "input": "Cognimoss embedding timing test"
  }'"'"' >/dev/null
'
```

Inspect Ollama:

```bash
ollama ps
```

Watch CPU and models:

```bash
watch -n0.5 '
ollama ps
echo
ps -eo pid,pcpu,pmem,rss,etime,args | grep "[l]lama-server"
'
```

Ollama service logs:

```bash
journalctl -u ollama -f
```

Cognee currently applies a 30-second per-attempt embedding timeout with a finite retry window, and those timeout values are not configurable through environment variables. Reducing embedding workload is therefore especially important on slow local inference servers.

---

# AWS Deployment

AWS mode replaces the local emulation/inference components:

```text
Moto       -> AWS SQS + DynamoDB + S3
Ollama     -> Amazon Bedrock
Local web  -> Elastic Beanstalk
Workers    -> EC2
```

A practical deployment uses:

```text
Internet
   │
   ▼
Elastic Beanstalk
FastAPI Frontend
   │
   ├── DynamoDB
   ├── S3
   └── SQS
        │
        ▼
   EC2 Worker Host
   ├── agent worker
   ├── Cognee indexer
   └── debug worker
        │
        ├── GitHub
        └── Amazon Bedrock
```

Workers do not require a public HTTP endpoint.

---

# Required AWS Resources

Create:

### Compute

* Elastic Beanstalk web environment
* one EC2 worker host initially

### Messaging

* `cognimoss-agent-runs`
* `cognimoss-cognee-index`
* `cognimoss-debug-runs`

Recommended DLQs:

* `cognimoss-agent-runs-dlq`
* `cognimoss-cognee-index-dlq`
* `cognimoss-debug-runs-dlq`

### State

* DynamoDB table `agent_runs`
* S3 chat-context bucket

### AI

* Amazon Bedrock model access/inference permissions

### Authentication

* Amazon Cognito user pool
* Cognito application client

### Security

* IAM role for Elastic Beanstalk
* IAM role for the EC2 worker
* Secrets Manager for sensitive production values

### Operations

* CloudWatch Logs
* CloudWatch alarms for DLQs/errors
* AWS Budget alarm

Optional:

* PostgreSQL + pgvector
* EFS if multiple workers must share filesystem-backed Cognee state

---

# AWS Environment File

Copy:

```bash
cp .env.aws.example .env.aws
```

If the checkout uses `.env.example` as the AWS template instead:

```bash
cp .env.example .env.aws
```

Never commit the resulting production environment file.

---

# AWS Variables That MUST Be Replaced

## Queue URLs

Replace:

```env
AGENT_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/ACCOUNT_ID/QUEUE_NAME
COGNEE_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/ACCOUNT_ID/QUEUE_NAME
DEBUG_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/ACCOUNT_ID/QUEUE_NAME
```

with the actual queue URLs.

---

## Chat Context Bucket

Replace:

```env
CHAT_CONTEXT_BUCKET=replace-with-production-bucket
```

The bucket name must be unique.

---

## Bedrock Models

Choose models available to your AWS account and Region:

```env
BEDROCK_MODEL=<MODEL_OR_INFERENCE_PROFILE>
BEDROCK_PLANNER_MODEL=<MODEL_OR_INFERENCE_PROFILE>
BEDROCK_WRITER_MODEL=<MODEL_OR_INFERENCE_PROFILE>
BEDROCK_CHAT_MODEL=<MODEL_OR_INFERENCE_PROFILE>

LLM_MODEL=<COGNEE_LLM_MODEL>
```

The existing template uses Amazon Titan Embed v2:

```env
EMBEDDING_PROVIDER=bedrock
EMBEDDING_MODEL=amazon.titan-embed-text-v2:0
EMBEDDING_DIMENSIONS=1024
```

Amazon Bedrock model inference requires `bedrock:InvokeModel`; streaming inference additionally requires `bedrock:InvokeModelWithResponseStream`. Converse uses these model-invocation permissions as well.

---

## GitHub Token

The worker still needs:

```env
GH_TOKEN=<REPLACE_ME>
GITHUB_TOKEN=<REPLACE_ME>
```

Store the production token in Secrets Manager or another protected runtime secret source rather than source control.

---

## Application Secret

Replace:

```env
SESSION_SECRET=load-from-secret-store
```

Generate:

```bash
openssl rand -hex 32
```

---

## Cognito

Replace:

```env
COGNITO_REGION=us-east-1
COGNITO_USER_POOL_ID=<REPLACE_ME>
COGNITO_CLIENT_ID=<REPLACE_ME>
COGNITO_CLIENT_SECRET=<REPLACE_ME>
COGNITO_DOMAIN=https://<REPLACE_ME>.auth.us-east-1.amazoncognito.com
```

For a Cognimoss deployment:

```text
Callback:
https://<APP_DOMAIN>/auth/callback

Logout:
https://<APP_DOMAIN>/
```

Cognito requires callback URLs to be pre-registered and requires HTTPS except for localhost development.

---

## Application URLs

For your own deployment, replace:

```env
PUBLIC_BASE_URL=https://<PUBLIC_DOMAIN>
APP_BASE_URL=https://<APP_DOMAIN>
```

For the existing Cognimoss deployment these may be:

```env
PUBLIC_BASE_URL=https://cognimoss.com
APP_BASE_URL=https://app.cognimoss.com
```

---

# Core AWS Infrastructure Bootstrap Script

The following script creates the application's basic AWS state plane:

* DynamoDB
* S3
* three SQS queues
* three dead-letter queues

It intentionally does **not** automatically create Cognito, IAM roles, Elastic Beanstalk, or EC2 because those resources usually need account-specific networking, domains, and security choices.

Create:

```text
scripts/bootstrap-aws.sh
```

with:

```bash
#!/usr/bin/env bash
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
PREFIX="${COGNIMOSS_PREFIX:-cognimoss}"
TABLE="${RUNS_TABLE:-agent_runs}"

ACCOUNT_ID="$(
  aws sts get-caller-identity \
    --query Account \
    --output text
)"

BUCKET="${CHAT_CONTEXT_BUCKET:-${PREFIX}-chat-context-${ACCOUNT_ID}-${REGION}}"

echo "Region:     $REGION"
echo "Account:    $ACCOUNT_ID"
echo "Table:      $TABLE"
echo "S3 bucket:  $BUCKET"


# ------------------------------------------------------------
# DynamoDB
# ------------------------------------------------------------

if ! aws dynamodb describe-table \
  --table-name "$TABLE" \
  --region "$REGION" \
  >/dev/null 2>&1
then

  aws dynamodb create-table \
    --region "$REGION" \
    --table-name "$TABLE" \
    --attribute-definitions \
      AttributeName=pk,AttributeType=S \
      AttributeName=sk,AttributeType=S \
    --key-schema \
      AttributeName=pk,KeyType=HASH \
      AttributeName=sk,KeyType=RANGE \
    --billing-mode PAY_PER_REQUEST

  aws dynamodb wait table-exists \
    --table-name "$TABLE" \
    --region "$REGION"
fi


# ------------------------------------------------------------
# S3
# ------------------------------------------------------------

if ! aws s3api head-bucket \
  --bucket "$BUCKET" \
  >/dev/null 2>&1
then

  if [[ "$REGION" == "us-east-1" ]]; then
    aws s3api create-bucket \
      --bucket "$BUCKET" \
      --region "$REGION"
  else
    aws s3api create-bucket \
      --bucket "$BUCKET" \
      --region "$REGION" \
      --create-bucket-configuration \
        LocationConstraint="$REGION"
  fi
fi


# ------------------------------------------------------------
# SQS helper
# ------------------------------------------------------------

create_queue_pair() {
  local NAME="$1"
  local VISIBILITY="$2"

  local DLQ="${NAME}-dlq"

  echo "Creating/checking $DLQ"

  aws sqs create-queue \
    --region "$REGION" \
    --queue-name "$DLQ" \
    >/dev/null

  local DLQ_URL
  DLQ_URL="$(
    aws sqs get-queue-url \
      --region "$REGION" \
      --queue-name "$DLQ" \
      --query QueueUrl \
      --output text
  )"

  local DLQ_ARN
  DLQ_ARN="$(
    aws sqs get-queue-attributes \
      --region "$REGION" \
      --queue-url "$DLQ_URL" \
      --attribute-names QueueArn \
      --query 'Attributes.QueueArn' \
      --output text
  )"

  local REDRIVE
  REDRIVE="$(
    printf \
      '{"deadLetterTargetArn":"%s","maxReceiveCount":"5"}' \
      "$DLQ_ARN"
  )"

  aws sqs create-queue \
    --region "$REGION" \
    --queue-name "$NAME" \
    --attributes \
      VisibilityTimeout="$VISIBILITY",ReceiveMessageWaitTimeSeconds=20,RedrivePolicy="$REDRIVE" \
    >/dev/null

  aws sqs get-queue-url \
    --region "$REGION" \
    --queue-name "$NAME" \
    --query QueueUrl \
    --output text
}


AGENT_QUEUE_URL="$(
  create_queue_pair \
    "${PREFIX}-agent-runs" \
    3600
)"

COGNEE_QUEUE_URL="$(
  create_queue_pair \
    "${PREFIX}-cognee-index" \
    14400
)"

DEBUG_QUEUE_URL="$(
  create_queue_pair \
    "${PREFIX}-debug-runs" \
    14400
)"


echo
echo "============================================================"
echo "AWS resources ready"
echo "============================================================"
echo
echo "RUNS_TABLE=$TABLE"
echo "CHAT_CONTEXT_BUCKET=$BUCKET"
echo "AGENT_QUEUE_URL=$AGENT_QUEUE_URL"
echo "COGNEE_QUEUE_URL=$COGNEE_QUEUE_URL"
echo "DEBUG_QUEUE_URL=$DEBUG_QUEUE_URL"
```

Make executable:

```bash
chmod +x scripts/bootstrap-aws.sh
```

Run:

```bash
AWS_REGION=us-east-1 ./scripts/bootstrap-aws.sh
```

DynamoDB's on-demand `PAY_PER_REQUEST` mode is appropriate for workloads whose traffic is not predictable, and SQS supports queue-level visibility timeouts and redrive policies for DLQs.

---

# AWS IAM

Use separate roles for the frontend and workers.

## Elastic Beanstalk Instance Role

The frontend generally needs:

```text
DynamoDB
  GetItem
  PutItem
  UpdateItem
  Query

SQS
  SendMessage
  GetQueueAttributes

S3
  GetObject
  PutObject

Secrets Manager
  GetSecretValue
  only if secrets are loaded at runtime
```

Restrict resources to the Cognimoss table, queues, and bucket rather than granting account-wide permissions.

---

## Worker EC2 Role

Workers generally need:

```text
SQS
  ReceiveMessage
  DeleteMessage
  ChangeMessageVisibility
  GetQueueAttributes
  GetQueueUrl

DynamoDB
  GetItem
  PutItem
  UpdateItem
  Query

S3
  GetObject
  PutObject

Bedrock
  InvokeModel
  InvokeModelWithResponseStream

Secrets Manager
  GetSecretValue

CloudWatch Logs
  CreateLogStream
  PutLogEvents
```

Add additional permissions only when a feature requires them.

---

# Cognito Setup

In the AWS Console:

```text
Amazon Cognito
    ↓
User pools
    ↓
Create user pool
    ↓
Create web application client
```

Configure:

```text
Callback URL:
https://<APP_DOMAIN>/auth/callback

Sign-out URL:
https://<APP_DOMAIN>/

OAuth scopes:
openid
email
profile
```

Then copy the generated values into the AWS environment.

---

# Elastic Beanstalk Frontend

The frontend runs as a Python web application.

A compatible `Procfile` is:

```text
web: uvicorn app:app --host 0.0.0.0 --port 8000
```

AWS currently supports deploying Python applications to Elastic Beanstalk with either the EB CLI or AWS console.

Typical EB CLI flow:

```bash
eb init
eb create cognimoss-web
eb deploy
```

Production environment variables can be configured as Elastic Beanstalk environment properties.

---

# EC2 Worker Setup

A simple beta deployment can run all three workers on one EC2 instance.

Suggested filesystem:

```text
/opt/cognimoss/app
/opt/cognimoss/app/.venv

/etc/cognimoss/

/var/lib/agent-runner/work
/var/lib/agent-runner/cognee

/var/lib/cognee-indexer/work
/var/lib/debug-runner/work

/var/log/cognimoss/
```

Create a service account:

```bash
sudo useradd \
  --system \
  --create-home \
  --shell /sbin/nologin \
  cognimoss || true
```

Create directories:

```bash
sudo mkdir -p \
  /opt/cognimoss/app \
  /etc/cognimoss \
  /var/lib/agent-runner/work \
  /var/lib/agent-runner/cognee \
  /var/lib/cognee-indexer/work \
  /var/lib/debug-runner/work \
  /var/log/cognimoss

sudo chown -R cognimoss:cognimoss \
  /opt/cognimoss \
  /var/lib/agent-runner \
  /var/lib/cognee-indexer \
  /var/lib/debug-runner \
  /var/log/cognimoss
```

---

# AWS Worker Services

Run:

```text
worker.py
cognee_indexer.py
debug_worker.py
```

as separate systemd services.

Example:

```ini
[Unit]
Description=Cognimoss Agent Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=cognimoss
Group=cognimoss

WorkingDirectory=/opt/cognimoss/app

EnvironmentFile=/opt/cognimoss/app/.env.common
EnvironmentFile=/opt/cognimoss/app/.env.bedrock

ExecStart=/opt/cognimoss/app/.venv/bin/python /opt/cognimoss/app/worker.py

Restart=always
RestartSec=10

StandardOutput=append:/var/log/cognimoss/agent-worker.log
StandardError=append:/var/log/cognimoss/agent-worker.log

[Install]
WantedBy=multi-user.target
```

Create equivalent services for:

```text
cognee_indexer.py
debug_worker.py
```

Then:

```bash
sudo systemctl daemon-reload

sudo systemctl enable --now \
  cognimoss-agent-worker \
  cognimoss-cognee-indexer \
  cognimoss-debug-worker
```

---

# AWS Worker Design Notes

Workers do not need inbound public HTTP access.

They primarily require outbound access to:

```text
GitHub
Amazon SQS
Amazon DynamoDB
Amazon S3
Amazon Bedrock
Secrets Manager
CloudWatch
```

Do not place workers behind a public application load balancer simply to consume SQS.

For an initial deployment, avoid running multiple Cognee indexers against filesystem-local Cognee state unless that state is intentionally moved to shared storage.

Keep:

```env
ALLOW_AGENT_COGNEE_SEEDING=0
SKIP_COGNEE_PRESEED=1
```

The dedicated Cognee indexer should own repository indexing.

---

# Monitoring

## Local

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  logs -f
```

Ollama:

```bash
journalctl -u ollama -f
```

Models:

```bash
ollama ps
```

Resources:

```bash
btop
```

---

## AWS

Monitor:

* Elastic Beanstalk health
* EC2 systemd services
* CloudWatch logs
* SQS queue depth
* SQS DLQ depth
* DynamoDB errors/throttling
* Bedrock errors/throttling

The unified CloudWatch agent is the preferred current AWS agent for collecting EC2 logs and metrics.

---

# Troubleshooting

## `.env.local` changes do not appear

Recreate containers:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  up -d --force-recreate
```

Then:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  exec -T cognee-indexer \
  env | sort
```

---

## Ollama is unreachable from Docker

Host:

```bash
curl http://127.0.0.1:11434/api/tags
```

Container:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  exec -T cognee-indexer \
  curl http://host.docker.internal:11434/api/tags
```

If host works but container fails, check:

* `OLLAMA_HOST`
* Docker `extra_hosts`
* host firewall
* Docker bridge configuration

---

## `server busy`

Inspect:

```bash
journalctl -u ollama -f
ollama ps
```

Reduce parallel inference before increasing queue pressure.

For constrained systems, start from:

```text
OLLAMA_NUM_PARALLEL=1
```

---

## Embedding timeouts

Reduce:

```env
EMBEDDING_BATCH_SIZE=1
```

Verify that the embedding model is already resident if running CPU-only.

Test the embedding endpoint directly.

---

## Wrong embedding model keeps loading

Check all three layers:

```bash
grep -E \
'EMBEDDING_MODEL|COGNEE_EMBEDDING_MODEL' \
.env.local
```

Then:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  config \
  | grep -E \
  'EMBEDDING_MODEL|COGNEE_EMBEDDING_MODEL'
```

Then:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  exec -T cognee-indexer \
  env \
  | grep -E \
  'EMBEDDING_MODEL|COGNEE_EMBEDDING_MODEL'
```

All three should agree.

---

## PostgreSQL is still using the old embedding dimensions

Inspect vector columns:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  exec -T postgres \
  sh -lc '
psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
SELECT
    n.nspname AS schema,
    c.relname AS table_name,
    a.attname AS column_name,
    format_type(a.atttypid, a.atttypmod) AS column_type
FROM pg_attribute a
JOIN pg_class c ON a.attrelid = c.oid
JOIN pg_namespace n ON c.relnamespace = n.oid
WHERE a.attnum > 0
  AND NOT a.attisdropped
  AND format_type(a.atttypid, a.atttypmod) LIKE '\''vector(%'\''
ORDER BY 1,2,3;
"
'
```

If the schema dimension differs from `EMBEDDING_DIMENSIONS`, rebuild the Cognee vector database.

---

# Development Safety Notes

Do not commit:

```text
.env.local
.env.aws
GitHub tokens
Cognito client secrets
database passwords
session secrets
```

Do not use:

```bash
docker compose down -v
```

as a normal restart command.

That command deletes Compose volumes and can remove persisted Cognee/PostgreSQL state.

Normal stop:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  down
```

Normal restart after environment changes:

```bash
docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  up -d --force-recreate
```

---

# Local Setup Checklist

* [ ] Docker installed
* [ ] Docker Compose installed
* [ ] Ollama installed
* [ ] GPU visible with `nvidia-smi`, if applicable
* [ ] Ollama listening on `0.0.0.0:11434`
* [ ] `qwen2.5-coder:14b` downloaded
* [ ] `nomic-embed-text:latest` downloaded
* [ ] `.env.local` copied from `.env.local.example`
* [ ] `SESSION_SECRET` replaced
* [ ] `GH_TOKEN` replaced
* [ ] `GITHUB_TOKEN` replaced
* [ ] Docker stack starts
* [ ] `local-init` exits successfully
* [ ] `ollama-check` exits successfully
* [ ] `/health` responds
* [ ] Docker can reach Ollama
* [ ] repository index reaches ready state
* [ ] agent can create a run
* [ ] GitHub branch/PR flow works

---

# AWS Deployment Checklist

* [ ] `agent_runs` DynamoDB table exists
* [ ] agent SQS queue exists
* [ ] Cognee SQS queue exists
* [ ] debug SQS queue exists
* [ ] queue DLQs exist
* [ ] S3 chat-context bucket exists
* [ ] Bedrock models are accessible
* [ ] worker IAM role can invoke Bedrock
* [ ] worker IAM role can consume SQS
* [ ] worker IAM role can update DynamoDB
* [ ] Elastic Beanstalk role can send SQS messages
* [ ] Cognito user pool exists
* [ ] Cognito callback URL is configured
* [ ] Cognito logout URL is configured
* [ ] production `SESSION_SECRET` configured
* [ ] production GitHub token stored securely
* [ ] Elastic Beanstalk frontend is healthy
* [ ] agent worker is running
* [ ] Cognee indexer is running
* [ ] debug worker is running
* [ ] CloudWatch receives logs
* [ ] DLQ alarms configured
* [ ] AWS Budget alert configured
* [ ] repository indexing reaches ready
* [ ] coding-agent run successfully creates a GitHub branch/PR

---

# Suggested First Run

For a new local developer:

```bash
git clone <repo>
cd github_coding_agent

cp .env.local.example .env.local

ollama pull qwen2.5-coder:14b
ollama pull nomic-embed-text:latest

# Edit SESSION_SECRET, GH_TOKEN and GITHUB_TOKEN.
$EDITOR .env.local

docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  up -d --build

docker compose \
  --env-file .env.local \
  -f docker-compose.local.yml \
  ps -a

curl http://127.0.0.1:8000/health
```

Then open:

```text
http://localhost:8000
```

Select a repository, create its Cognee index, wait for the index to become ready, and then begin using repository chat, debugging, or coding-agent workflows.
