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

# --- test: no environment argument leaves the existing environment untouched ---
result_no_env_override=$(echo "$FIXTURE_WITH_COMMAND" | jq '.taskDefinition' | patch_image "111111111111.dkr.ecr.us-west-2.amazonaws.com/backend:abc123")
env_value=$(echo "$result_no_env_override" | jq -c '.containerDefinitions[0].environment')
if [ "$env_value" != '[{"name":"ENVIRONMENT","value":"qa"}]' ]; then
  echo "FAIL: environment should be untouched without an override, got: $env_value"
  exit 1
fi

# --- test: an env upsert ADDS a new variable and keeps the existing ones ---
# This is the whole point of upserting rather than replacing: a family's
# registered environment holds endpoints Terraform resolved at apply time, and
# the pipeline must not drop them by supplying a partial list.
result_add=$(echo "$FIXTURE_WITH_COMMAND" | jq '.taskDefinition' | patch_image "img:1" "" '[{"name": "CORS_ORIGINS", "value": "[\"https://app.example.com\"]"}]')
env_names=$(echo "$result_add" | jq -c '[.containerDefinitions[0].environment[].name] | sort')
if [ "$env_names" != '["CORS_ORIGINS","ENVIRONMENT"]' ]; then
  echo "FAIL: upsert should add CORS_ORIGINS and keep ENVIRONMENT, got: $env_names"
  exit 1
fi
kept=$(echo "$result_add" | jq -r '.containerDefinitions[0].environment[] | select(.name=="ENVIRONMENT") | .value')
if [ "$kept" != "qa" ]; then
  echo "FAIL: pre-existing ENVIRONMENT value was not preserved, got: $kept"
  exit 1
fi

# --- test: an env upsert OVERWRITES a variable that is already present, once ---
FIXTURE_MULTI=$(echo "$FIXTURE" | jq '.taskDefinition.containerDefinitions[0].environment = [
  {"name":"ENVIRONMENT","value":"qa"},
  {"name":"DB_HOST","value":"aurora.internal"},
  {"name":"CORS_ORIGINS","value":"[]"}
]')
result_over=$(echo "$FIXTURE_MULTI" | jq '.taskDefinition' | patch_image "img:1" "" '[{"name": "CORS_ORIGINS", "value": "[\"https://new.example.com\"]"}]')
occurrences=$(echo "$result_over" | jq '[.containerDefinitions[0].environment[] | select(.name=="CORS_ORIGINS")] | length')
if [ "$occurrences" != "1" ]; then
  echo "FAIL: CORS_ORIGINS should appear exactly once after an upsert, got: $occurrences"
  exit 1
fi
new_value=$(echo "$result_over" | jq -r '.containerDefinitions[0].environment[] | select(.name=="CORS_ORIGINS") | .value')
if [ "$new_value" != '["https://new.example.com"]' ]; then
  echo "FAIL: CORS_ORIGINS was not overwritten, got: $new_value"
  exit 1
fi
db_host=$(echo "$result_over" | jq -r '.containerDefinitions[0].environment[] | select(.name=="DB_HOST") | .value')
if [ "$db_host" != "aurora.internal" ]; then
  echo "FAIL: a Terraform-resolved endpoint (DB_HOST) must survive an upsert, got: $db_host"
  exit 1
fi

# --- test: an upsert onto a container with no environment at all works ---
FIXTURE_NO_ENV=$(echo "$FIXTURE" | jq 'del(.taskDefinition.containerDefinitions[0].environment)')
result_none=$(echo "$FIXTURE_NO_ENV" | jq '.taskDefinition' | patch_image "img:1" "" '[{"name": "DB_NAME", "value": "open_wearables"}]')
env_value=$(echo "$result_none" | jq -c '.containerDefinitions[0].environment')
if [ "$env_value" != '[{"name":"DB_NAME","value":"open_wearables"}]' ]; then
  echo "FAIL: upsert onto a container with no environment failed, got: $env_value"
  exit 1
fi

# --- test: a secret upsert adds without dropping the already-registered ones ---
# Same reasoning as the environment upsert: the registered secrets carry ARNs
# Terraform resolved at apply time, and a partial list would silently strip
# DB_PASSWORD or REDIS_PASSWORD and leave the container unable to start.
FIXTURE_SECRETS=$(echo "$FIXTURE" | jq '.taskDefinition.containerDefinitions[0].secrets = [
  {"name":"DB_PASSWORD","valueFrom":"arn:aws:secretsmanager:us-west-2:0:secret:db"},
  {"name":"SECRET_KEY","valueFrom":"arn:aws:secretsmanager:us-west-2:0:secret:sk"}
]')
result_sec=$(echo "$FIXTURE_SECRETS" | jq '.taskDefinition' | patch_image "img:1" "" "" '[{"name":"WHOOP_CLIENT_SECRET","valueFrom":"arn:aws:secretsmanager:us-west-2:0:secret:whoop"}]')
sec_names=$(echo "$result_sec" | jq -c '[.containerDefinitions[0].secrets[].name] | sort')
if [ "$sec_names" != '["DB_PASSWORD","SECRET_KEY","WHOOP_CLIENT_SECRET"]' ]; then
  echo "FAIL: secret upsert should add without dropping, got: $sec_names"
  exit 1
fi
db_arn=$(echo "$result_sec" | jq -r '.containerDefinitions[0].secrets[] | select(.name=="DB_PASSWORD") | .valueFrom')
if [ "$db_arn" != "arn:aws:secretsmanager:us-west-2:0:secret:db" ]; then
  echo "FAIL: an existing secret ARN must survive the upsert, got: $db_arn"
  exit 1
fi

# --- test: a secret upsert replaces a rotated ARN exactly once ---
result_sec2=$(echo "$FIXTURE_SECRETS" | jq '.taskDefinition' | patch_image "img:1" "" "" '[{"name":"DB_PASSWORD","valueFrom":"arn:aws:secretsmanager:us-west-2:0:secret:db-NEW"}]')
occ=$(echo "$result_sec2" | jq '[.containerDefinitions[0].secrets[] | select(.name=="DB_PASSWORD")] | length')
new_arn=$(echo "$result_sec2" | jq -r '.containerDefinitions[0].secrets[] | select(.name=="DB_PASSWORD") | .valueFrom')
if [ "$occ" != "1" ] || [ "$new_arn" != "arn:aws:secretsmanager:us-west-2:0:secret:db-NEW" ]; then
  echo "FAIL: secret overwrite should leave exactly one updated entry, got occ=$occ arn=$new_arn"
  exit 1
fi

# --- test: no secret argument leaves the registered secrets untouched ---
result_nosec=$(echo "$FIXTURE_SECRETS" | jq '.taskDefinition' | patch_image "img:1")
sec_names=$(echo "$result_nosec" | jq -c '[.containerDefinitions[0].secrets[].name] | sort')
if [ "$sec_names" != '["DB_PASSWORD","SECRET_KEY"]' ]; then
  echo "FAIL: secrets should be untouched without an upsert, got: $sec_names"
  exit 1
fi

# --- test: env and secret upserts compose without interfering ---
result_both=$(echo "$FIXTURE_SECRETS" | jq '.taskDefinition' | patch_image "img:1" "" '[{"name":"API_BASE_URL","value":"https://api.example.com"}]' '[{"name":"OURA_CLIENT_SECRET","valueFrom":"arn:oura"}]')
env_ok=$(echo "$result_both" | jq -c '[.containerDefinitions[0].environment[].name] | sort')
sec_ok=$(echo "$result_both" | jq -c '[.containerDefinitions[0].secrets[].name] | sort')
if [ "$env_ok" != '["API_BASE_URL","ENVIRONMENT"]' ] || [ "$sec_ok" != '["DB_PASSWORD","OURA_CLIENT_SECRET","SECRET_KEY"]' ]; then
  echo "FAIL: env and secret upserts interfered, env=$env_ok sec=$sec_ok"
  exit 1
fi

echo "All tests passed"
