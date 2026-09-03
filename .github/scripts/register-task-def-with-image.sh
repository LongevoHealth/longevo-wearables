#!/bin/bash
set -euo pipefail

# Re-registers an ECS task definition family with a new image, preserving
# everything else Terraform already configured (env vars, secrets, roles,
# sizing). Terraform registers the family once with a placeholder image and
# `lifecycle { ignore_changes = [container_definitions] }` — this script is
# what supplies the real one on every deploy.
#
# Usage: register-task-def-with-image.sh <family> <image-uri> [command-json-array] [env-upsert-json-array] [secret-upsert-json-array]
# The optional third argument overrides containerDefinitions[0].command — for
# families where Terraform's initial command is a placeholder that must be
# replaced too (e.g. db-bootstrap, which runs the same backend image as api
# but invokes a one-off script rather than the server). Omit it to leave the
# already-registered command untouched, which is what every other family wants.
# The optional fourth argument upserts entries into
# containerDefinitions[0].environment, matching on `name`: listed variables are
# added or overwritten, and every other already-registered variable is left
# alone. Needed for the same reason as the command: Terraform's
# `ignore_changes = [container_definitions]` means a variable added to a
# family's .tf environment block after its first registration never reaches the
# real task definition on its own. It upserts rather than replaces because a
# family's registered environment holds values Terraform resolved at apply time
# (Aurora and Valkey endpoints), which the pipeline has no business restating —
# dropping them by supplying a partial list would break the container.
# The optional fifth argument does the same upsert for
# containerDefinitions[0].secrets, matching on `name` and carrying `valueFrom`
# ARNs rather than values — the secret material itself never passes through
# here, only the pointer ECS resolves at task start.
# Prints the new task definition ARN to stdout on success.

# patch_image reads a task definition JSON object from stdin, replaces the
# first container's image (and, optionally, its command; and upserts
# environment variables), and strips the fields register-task-definition
# rejects (they only exist on a *registered* revision, not an input to create
# one). Pure transformation — no AWS calls — so it's unit-testable on its own.
patch_image() {
  local new_image="$1"
  local new_command="${2:-}"
  local env_upsert="${3:-}"
  local secret_upsert="${4:-}"
  jq --arg image "$new_image" \
     --argjson command "${new_command:-null}" \
     --argjson upsert "${env_upsert:-null}" \
     --argjson secrets "${secret_upsert:-null}" '
    .containerDefinitions[0].image = $image
    | (if $command != null then .containerDefinitions[0].command = $command else . end)
    | (if $upsert != null then
         ($upsert | map(.name)) as $names
         | .containerDefinitions[0].environment =
             (((.containerDefinitions[0].environment // [])
               | map(select(.name as $n | ($names | index($n)) == null)))
              + $upsert)
       else . end)
    | (if $secrets != null then
         ($secrets | map(.name)) as $snames
         | .containerDefinitions[0].secrets =
             (((.containerDefinitions[0].secrets // [])
               | map(select(.name as $n | ($snames | index($n)) == null)))
              + $secrets)
       else . end)
    | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities, .registeredAt, .registeredBy)
  '
}

main() {
  local family="$1"
  local image="$2"
  local command="${3:-}"
  local env_upsert="${4:-}"
  local secret_upsert="${5:-}"

  local current
  current=$(aws ecs describe-task-definition --task-definition "$family" --query 'taskDefinition' --output json)

  local patched
  patched=$(echo "$current" | patch_image "$image" "$command" "$env_upsert" "$secret_upsert")

  aws ecs register-task-definition --cli-input-json "$patched" --query 'taskDefinition.taskDefinitionArn' --output text
}

# `--source-only` lets the test suite load patch_image() without triggering
# main() or requiring AWS credentials.
if [ "${1:-}" != "--source-only" ]; then
  main "$@"
fi
