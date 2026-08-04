#!/usr/bin/env bash
set -euo pipefail

compose=(docker compose -f docker-compose.local.yml)

printf '\n== containers ==\n'
"${compose[@]}" ps

printf '\n== web health ==\n'
curl -fsS http://127.0.0.1:8000/health | python -m json.tool

printf '\n== Ollama ==\n'
curl -fsS http://127.0.0.1:11434/api/tags >/dev/null
echo "Ollama API reachable"

printf '\n== local queues ==\n'
"${compose[@]}" exec -T frontend python - <<'PY'
import os, boto3
kwargs={"region_name":os.environ["AWS_REGION"], "endpoint_url":os.environ["AWS_ENDPOINT_URL"]}
sqs=boto3.client("sqs", **kwargs)
for name in (os.environ["AGENT_QUEUE_NAME"], os.environ["COGNEE_QUEUE_NAME"], os.environ["DEBUG_QUEUE_NAME"]):
    print(name, "->", sqs.get_queue_url(QueueName=name)["QueueUrl"])
PY

printf '\n== DynamoDB and S3 ==\n'
"${compose[@]}" exec -T frontend python - <<'PY'
import os, boto3
kwargs={"region_name":os.environ["AWS_REGION"], "endpoint_url":os.environ["AWS_ENDPOINT_URL"]}
print(boto3.client("dynamodb", **kwargs).describe_table(TableName=os.environ["RUNS_TABLE"])["Table"]["TableStatus"])
print(boto3.client("s3", **kwargs).head_bucket(Bucket=os.environ["CHAT_CONTEXT_BUCKET"])["ResponseMetadata"]["HTTPStatusCode"])
PY

printf '\n== recent service logs ==\n'
"${compose[@]}" logs --tail=25 frontend agent-worker cognee-indexer debug-worker
