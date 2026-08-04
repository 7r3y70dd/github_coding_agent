from __future__ import annotations

import os
import time

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError

REGION = os.environ.get("AWS_REGION", "us-east-1")
ENDPOINT = os.environ.get("AWS_ENDPOINT_URL", "http://moto:5000")
TABLE = os.environ.get("RUNS_TABLE", "agent_runs")
BUCKET = os.environ.get("CHAT_CONTEXT_BUCKET", "cognimoss-local-chat-context")
QUEUES = {
    os.environ.get("AGENT_QUEUE_NAME", "cognimoss-agent-runs"): "3600",
    os.environ.get("COGNEE_QUEUE_NAME", "cognimoss-cognee-index"): "14400",
    os.environ.get("DEBUG_QUEUE_NAME", "cognimoss-debug-runs"): "14400",
}

kwargs = {"region_name": REGION, "endpoint_url": ENDPOINT}

for attempt in range(60):
    try:
        boto3.client("sts", **kwargs).get_caller_identity()
        break
    except Exception as exc:
        if attempt == 59:
            raise RuntimeError(f"Local AWS emulator never became ready: {exc}") from exc
        time.sleep(1)

sqs = boto3.client("sqs", **kwargs)
dynamodb = boto3.client("dynamodb", **kwargs)
s3 = boto3.client("s3", **kwargs)

for queue_name, visibility in QUEUES.items():
    response = sqs.create_queue(
        QueueName=queue_name,
        Attributes={
            "VisibilityTimeout": visibility,
            "ReceiveMessageWaitTimeSeconds": "20",
        },
    )
    print(f"queue {queue_name}: {response['QueueUrl']}", flush=True)

try:
    dynamodb.describe_table(TableName=TABLE)
    print(f"table {TABLE}: already exists", flush=True)
except dynamodb.exceptions.ResourceNotFoundException:
    dynamodb.create_table(
        TableName=TABLE,
        AttributeDefinitions=[
            {"AttributeName": "pk", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "pk", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    print(f"table {TABLE}: created", flush=True)

try:
    s3.head_bucket(Bucket=BUCKET)
    print(f"bucket {BUCKET}: already exists", flush=True)
except ClientError:
    s3.create_bucket(Bucket=BUCKET)
    print(f"bucket {BUCKET}: created", flush=True)

print("Local AWS resources are ready.", flush=True)
