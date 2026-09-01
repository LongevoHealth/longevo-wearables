#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/../register-task-def-with-image.sh" --source-only

FIXTURE='{
  "taskDefinition": {
    "family": "qa-open-wearables-api",
    "revision": 3,
    "status": "ACTIVE",
    "taskDefinitionArn": "arn:aws:ecs:us-west-2:000000000000:task-definition/qa-open-wearables-api:3",
    "requiresAttributes": [{"name": "com.amazonaws.ecs.capability.docker-remote-api.1.19"}],
    "compatibilities": ["FARGATE"],
    "registeredAt": "2026-01-01T00:00:00Z",
    "registeredBy": "arn:aws:iam::000000000000:role/someone",
    "cpu": "256",
    "memory": "512",
    "networkMode": "awsvpc",
    "containerDefinitions": [
      {
        "name": "api",
        "image": "public.ecr.aws/docker/library/busybox:latest",
        "essential": true,
        "environment": [{"name": "ENVIRONMENT", "value": "qa"}]
      }
    ]
  }
}'

# --- test: the new image replaces the placeholder ---
result=$(echo "$FIXTURE" | jq '.taskDefinition' | patch_image "111111111111.dkr.ecr.us-west-2.amazonaws.com/backend:abc123")
new_image=$(echo "$result" | jq -r '.containerDefinitions[0].image')
if [ "$new_image" != "111111111111.dkr.ecr.us-west-2.amazonaws.com/backend:abc123" ]; then
  echo "FAIL: image not replaced, got: $new_image"
  exit 1
fi

# --- test: register-time-only fields are stripped ---
for field in taskDefinitionArn revision status requiresAttributes compatibilities registeredAt registeredBy; do
  if echo "$result" | jq -e "has(\"$field\")" > /dev/null; then
    echo "FAIL: field '$field' should have been stripped, but is present"
    exit 1
  fi
done

# --- test: everything else survives untouched ---
env_value=$(echo "$result" | jq -r '.containerDefinitions[0].environment[0].value')
if [ "$env_value" != "qa" ]; then
  echo "FAIL: unrelated field (environment) got mangled, got: $env_value"
  exit 1
fi
family=$(echo "$result" | jq -r '.family')
if [ "$family" != "qa-open-wearables-api" ]; then
  echo "FAIL: family got mangled, got: $family"
  exit 1
fi

# --- test: no command argument leaves an existing command untouched ---
FIXTURE_WITH_COMMAND=$(echo "$FIXTURE" | jq '.taskDefinition.containerDefinitions[0].command = ["old", "command"]')
result_no_override=$(echo "$FIXTURE_WITH_COMMAND" | jq '.taskDefinition' | patch_image "111111111111.dkr.ecr.us-west-2.amazonaws.com/backend:abc123")
command_value=$(echo "$result_no_override" | jq -c '.containerDefinitions[0].command')
if [ "$command_value" != '["old","command"]' ]; then
  echo "FAIL: command should be untouched without an override, got: $command_value"
  exit 1
fi

# --- test: a command argument replaces the existing command ---
result_override=$(echo "$FIXTURE_WITH_COMMAND" | jq '.taskDefinition' | patch_image "111111111111.dkr.ecr.us-west-2.amazonaws.com/backend:abc123" '["python", "scripts/bootstrap_db_roles.py"]')
command_value=$(echo "$result_override" | jq -c '.containerDefinitions[0].command')
if [ "$command_value" != '["python","scripts/bootstrap_db_roles.py"]' ]; then
  echo "FAIL: command override did not apply, got: $command_value"
  exit 1
fi

echo "All tests passed"
