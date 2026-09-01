#!/bin/bash
set -euo pipefail

# Re-registers an ECS task definition family with a new image, preserving
# everything else Terraform already configured (env vars, secrets, roles,
# sizing). Terraform registers the family once with a placeholder image and
# `lifecycle { ignore_changes = [container_definitions] }` — this script is
# what supplies the real one on every deploy.
#
# Usage: register-task-def-with-image.sh <family> <image-uri> [command-json-array]
# The optional third argument overrides containerDefinitions[0].command — for
# families where Terraform's initial command is a placeholder that must be
# replaced too (e.g. db-bootstrap, which runs the same backend image as api
# but invokes a one-off script rather than the server). Omit it to leave the
# already-registered command untouched, which is what every other family wants.
# Prints the new task definition ARN to stdout on success.

# patch_image reads a task definition JSON object from stdin, replaces the
# first container's image (and, optionally, its command), and strips the
# fields register-task-definition rejects (they only exist on a *registered*
# revision, not an input to create one). Pure transformation — no AWS calls —
# so it's unit-testable on its own.
patch_image() {
  local new_image="$1"
  local new_command="${2:-}"
  jq --arg image "$new_image" --argjson command "${new_command:-null}" '
    .containerDefinitions[0].image = $image
    | (if $command != null then .containerDefinitions[0].command = $command else . end)
    | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities, .registeredAt, .registeredBy)
  '
}

main() {
  local family="$1"
  local image="$2"
  local command="${3:-}"

  local current
  current=$(aws ecs describe-task-definition --task-definition "$family" --query 'taskDefinition' --output json)

  local patched
  patched=$(echo "$current" | patch_image "$image" "$command")

  aws ecs register-task-definition --cli-input-json "$patched" --query 'taskDefinition.taskDefinitionArn' --output text
}

# `--source-only` lets the test suite load patch_image() without triggering
# main() or requiring AWS credentials.
if [ "${1:-}" != "--source-only" ]; then
  main "$@"
fi
