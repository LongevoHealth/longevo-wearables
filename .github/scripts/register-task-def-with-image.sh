#!/bin/bash
set -euo pipefail

# Re-registers an ECS task definition family with a new image, preserving
# everything else Terraform already configured (env vars, secrets, roles,
# sizing). Terraform registers the family once with a placeholder image and
# `lifecycle { ignore_changes = [container_definitions] }` — this script is
# what supplies the real one on every deploy.
#
# Usage: register-task-def-with-image.sh <family> <image-uri>
# Prints the new task definition ARN to stdout on success.

# patch_image reads a task definition JSON object from stdin, replaces the
# first container's image, and strips the fields register-task-definition
# rejects (they only exist on a *registered* revision, not an input to create
# one). Pure transformation — no AWS calls — so it's unit-testable on its own.
patch_image() {
  local new_image="$1"
  jq --arg image "$new_image" '
    .containerDefinitions[0].image = $image
    | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities, .registeredAt, .registeredBy)
  '
}

main() {
  local family="$1"
  local image="$2"

  local current
  current=$(aws ecs describe-task-definition --task-definition "$family" --query 'taskDefinition' --output json)

  local patched
  patched=$(echo "$current" | patch_image "$image")

  aws ecs register-task-definition --cli-input-json "$patched" --query 'taskDefinition.taskDefinitionArn' --output text
}

# `--source-only` lets the test suite load patch_image() without triggering
# main() or requiring AWS credentials.
if [ "${1:-}" != "--source-only" ]; then
  main "$@"
fi
