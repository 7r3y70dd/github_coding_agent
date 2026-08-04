# Cognimoss frontend with mock backend

This build keeps the existing FastAPI-rendered frontend and replaces the expensive AWS worker path with a local SQLite-backed mock backend.

## What is simulated

- Git Agent run creation, status progression, and run events
- Cognee repository tree, reseed jobs, index status, and ready scopes
- Repository chat responses with production-compatible JSON fields
- Repo Debugger lifecycle, events, summary, and simulated issue counts
- DynamoDB, SQS, Bedrock, Cognee workers, repo cloning, and pytest workers are not called

The mock backend stores data in SQLite and advances jobs when the frontend polls them. No background worker process is required.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export APP_MODE=mock
export MOCK_BACKEND=1
export AUTH_ENABLED=0

uvicorn app:app --reload --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000/`. Non-production hostnames show the dashboard automatically in mock mode.

## Elastic Beanstalk deployment

Upload `cognimoss-mock-backend-eb.zip` as a new application version under:

- Application: `git-agent-control-pannel-prod`
- Recommended new environment: `Git-agent-control-pannel-mock-env`

Do not replace the production environment until the mock environment has been tested.

Set these environment properties:

```text
APP_MODE=mock
MOCK_BACKEND=1
MOCK_USE_GITHUB=0
MOCK_SEED_DEMO_DATA=1
DASHBOARD_ON_DEFAULT_HOST=1
AUTH_ENABLED=0
COOKIE_HTTPS_ONLY=1
```

For a publicly reachable team environment, enable Cognito after confirming its callback URL:

```text
AUTH_ENABLED=1
SESSION_SECRET=<long-random-secret>
COGNITO_REGION=us-east-1
COGNITO_USER_POOL_ID=<pool-id>
COGNITO_CLIENT_ID=<client-id>
COGNITO_CLIENT_SECRET=<client-secret>
COGNITO_DOMAIN=https://<your-cognito-domain>
APP_BASE_URL=https://<mock-environment-domain>
COOKIE_HTTPS_ONLY=1
```

## Useful settings

```text
MOCK_DB_PATH=/tmp/cognimoss-mock-backend.sqlite3
MOCK_RUN_STEP_SECONDS=4
MOCK_INDEX_STEP_SECONDS=3
MOCK_DEBUG_STEP_SECONDS=4
MOCK_SEED_DEMO_DATA=1
MOCK_USE_GITHUB=0
```

`MOCK_USE_GITHUB=1` makes the repository-tree endpoint call GitHub while all compute-heavy actions remain mocked. Set `GITHUB_TOKEN` for private repositories or better GitHub API limits.

The default SQLite path is intentionally ephemeral. Data survives normal requests and process activity, but can disappear when Elastic Beanstalk replaces the EC2 instance or redeploys the application.

## Health and reset endpoints

```bash
curl https://<environment>/health
curl https://<environment>/api/mock/state
curl -X POST https://<environment>/api/mock/reset
```

The reset endpoint deletes mock-created data and restores the seeded demonstration records when `MOCK_SEED_DEMO_DATA=1`.

## Switching back to AWS services

The original AWS code remains present. Set:

```text
APP_MODE=aws
MOCK_BACKEND=0
```

Then configure the original DynamoDB table, SQS queue URLs, IAM permissions, Bedrock settings, and Cognito settings. Test this separately; mock mode does not validate the real worker infrastructure.
