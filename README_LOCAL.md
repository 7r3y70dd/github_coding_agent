# Full local Cognimoss stack

This setup runs the real FastAPI application, agent worker, Cognee indexer, and debugger locally. Moto replaces SQS, DynamoDB, and S3. Ollama replaces Bedrock for planner, writer, Cognee, embeddings, and repository chat. GitHub remains the source repository and PR/issue API.

## 1. Prepare Ollama on the host

Pull the configured models:

```bash
ollama pull qwen2.5-coder:14b
ollama pull nomic-embed-text
```

Docker must be able to reach Ollama. Ollama normally binds only to loopback, so start or configure it to listen on all local interfaces:

```bash
OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

Keep port 11434 firewalled from untrusted networks.

## 2. Create the local environment file

```bash
cp .env.local.example .env.local
$EDITOR .env.local
```

At minimum replace `GH_TOKEN`, `GITHUB_TOKEN`, and `SESSION_SECRET`. The token should be restricted to repositories Cognimoss is allowed to access.

## 3. Start the complete stack

```bash
docker compose -f docker-compose.local.yml up --build
```

Open `http://localhost:8000`.

Run the checks from another terminal:

```bash
./scripts/local-check.sh
```

## 4. Inspect or reset local state

Moto dashboard:

```text
http://localhost:5000/moto-api/
```

Stop containers but retain Cognee/work directories:

```bash
docker compose -f docker-compose.local.yml down
```

Delete all Docker volumes and start clean:

```bash
docker compose -f docker-compose.local.yml down -v
```

## AWS compatibility

The local behavior is activated only by `.env.local`:

- `APP_MODE=local`
- `AWS_ENDPOINT_URL=http://moto:5000`
- queue names instead of production queue URLs
- `MODEL_PROVIDER=ollama`
- `CHAT_MODEL_PROVIDER=ollama`
- Cognee LLM and embedding providers set to Ollama

Production remains on AWS when its existing environment uses `APP_MODE=aws`, real queue URLs, no `AWS_ENDPOINT_URL`, Bedrock providers, and IAM credentials/roles. `.env.aws.example` documents those selectors.

## Reproducibility warning

`backend_package/requirements.txt` currently contains unpinned `cognee[all]`. A new Docker build can therefore install a different Cognee release than the AWS worker. For exact parity, capture the working AWS environment and commit a constraints file:

```bash
python -m pip freeze | sort > constraints-aws-working.txt
```

Then change the backend Docker install step to use `-c constraints-aws-working.txt`. Do this after confirming the constraints do not contain machine-specific editable paths.
