# Pipeline de deploy a ECS qa — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Un workflow de GitHub Actions que, en cada push a `main`, construye las dos imágenes, corre la migración, y actualiza los cinco servicios de ECS en qa — en el orden que evita que un rollout deje el esquema y el código desincronizados.

**Architecture:** Un script chico y puro (`register-task-def-with-image.sh`) hace la única parte no trivial — tomar la task definition que Terraform registró con imagen placeholder y re-registrarla con la imagen real, sin tocar nada más de lo que Terraform ya configuró (env vars, secrets, roles). El workflow lo invoca seis veces (backend×5 + frontend×1) y orquesta el orden: build → push → migración (aborta todo si falla) → actualizar api/frontend/worker/worker-bulk → actualizar beat al final.

**Tech Stack:** GitHub Actions, AWS CLI v2 (ya instalado en los runners `ubuntu-latest`), `jq`.

**Spec:** `docs/superpowers/specs/2026-08-22-aws-infra-design.md` (sección 5, "Orden del rollout"). Compañero: `docs/superpowers/plans/2026-09-01-platform-services-qa.md`, en `longevoIac` — este plan consume sus outputs.

## Global Constraints

- **El trabajo ocurre en `/Users/manupandolfi/Longevo/longevo-wearables`**, rama base `main`. Crear una rama de feature antes del primer commit.
- **Este plan asume que `docs/superpowers/plans/2026-09-01-platform-services-qa.md` ya está aplicado.** Sin cluster, servicios y rol de deploy reales, este workflow no tiene contra qué correr — no se puede probar de punta a punta hasta entonces, sólo validar sintaxis y la lógica del script de forma aislada.
- El rol OIDC de deploy (`qa-open-wearables-deploy`) confía en `repo:LongevoHealth/longevo-wearables:ref:refs/heads/main` — el workflow sólo puede dispararse por push a `main`, no por pull request ni por rama.
- El entorno de GitHub `qa-open-wearables` necesita una variable `AWS_DEPLOY_ROLE_ARN` con el ARN del rol (output `deploy_role_arn` del plan compañero). No es un secreto (es un ARN, no una credencial), pero vive en el entorno de todos modos para que el `permissions: id-token: write` del job sólo pueda asumir ese rol específico si el entorno lo autoriza.
- Las familias de task definition son fijas, ya registradas por Terraform con imagen placeholder: `qa-open-wearables-api`, `qa-open-wearables-celery-worker`, `qa-open-wearables-celery-worker-bulk`, `qa-open-wearables-celery-beat`, `qa-open-wearables-migration` (imagen backend); `qa-open-wearables-frontend` (imagen frontend propia).
- Los nombres de servicio de ECS coinciden con las familias de task definition salvo `migration`, que no tiene servicio — se corre con `RunTask`, no `UpdateService`.
- **Si la migración falla, el deploy aborta sin tocar ningún servicio.** Es la regla explícita del spec — un fallo de Alembic no debe dejar tasks nuevas corriendo contra un esquema a medio migrar.
- `celery-beat` se actualiza **al final**, después de que los demás servicios ya estén estables — nunca en paralelo con otro deploy de beat (la propia configuración de `min 0 / max 100` de Terraform ya evita que convivan dos, pero actualizarlo último minimiza la ventana sin scheduler).
- Verificación local: `bash -n` para sintaxis de shell, `shellcheck` si está disponible, y un test de la lógica de transformación de JSON con `jq` usando fixtures — sin llamar a AWS. La verificación end-to-end contra AWS real queda para después de que el plan compañero esté aplicado.
- Commits con conventional commits.

## Estructura de archivos

| Archivo | Responsabilidad | Task |
|---|---|---|
| `.github/scripts/register-task-def-with-image.sh` | Re-registra una task definition con una imagen nueva, preservando todo lo demás (nuevo) | 1 |
| `.github/scripts/tests/register-task-def-with-image.test.sh` | Test de la transformación JSON contra fixtures, sin AWS (nuevo) | 1 |
| `.github/workflows/deploy-qa.yml` | El workflow completo (nuevo) | 2 |

---

### Task 1: Script de re-registro de task definition

Toma una familia de task definition y una imagen, la describe, le cambia sólo el campo `image` del primer (y único) container, le quita los campos que ECS no acepta en un `register-task-definition` (`taskDefinitionArn`, `revision`, `status`, `requiresAttributes`, `compatibilities`, `registeredAt`, `registeredBy`), y registra la nueva revisión. Imprime el ARN de la revisión nueva en stdout — nada más, para que el workflow lo capture limpio.

**Files:**
- Create: `.github/scripts/register-task-def-with-image.sh`
- Test: `.github/scripts/tests/register-task-def-with-image.test.sh`

**Interfaces:**
- Consumes: nada de otra task.
- Produces: `register-task-def-with-image.sh <family> <image-uri>` → imprime el ARN de la nueva revisión en stdout y sale con código 0, o sale con código ≠0 y nada en stdout si falla. Lo consume el workflow (Task 2), una vez por cada una de las seis familias.

- [ ] **Step 1: Crear la rama**

```bash
cd /Users/manupandolfi/Longevo/longevo-wearables && git switch main -q && git pull -q && git switch -c feat/deploy-pipeline-qa
```

- [ ] **Step 2: Escribir el test de la transformación JSON (sin AWS)**

El script llama a `aws ecs describe-task-definition`/`register-task-definition` para la parte real, pero la transformación en sí (JSON in → JSON out) es pura y se puede probar aislada extrayendo esa lógica a una función que el test invoca directamente con un fixture, sin tocar la red.

`.github/scripts/tests/register-task-def-with-image.test.sh`:

```bash
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

echo "All tests passed"
```

- [ ] **Step 3: Correr el test y verificar que falla**

```bash
chmod +x .github/scripts/tests/register-task-def-with-image.test.sh
bash .github/scripts/tests/register-task-def-with-image.test.sh
```

Expected: FAIL — `register-task-def-with-image.sh` todavía no existe (`source: no such file or directory` o similar).

- [ ] **Step 4: Escribir el script**

`.github/scripts/register-task-def-with-image.sh`:

```bash
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
```

- [ ] **Step 5: Correr el test y verificar que pasa**

```bash
chmod +x .github/scripts/register-task-def-with-image.sh
bash .github/scripts/tests/register-task-def-with-image.test.sh
```

Expected: `All tests passed`

- [ ] **Step 6: Verificar sintaxis de shell**

```bash
bash -n .github/scripts/register-task-def-with-image.sh
bash -n .github/scripts/tests/register-task-def-with-image.test.sh
which shellcheck > /dev/null && shellcheck .github/scripts/register-task-def-with-image.sh || echo "shellcheck no instalado, se salteó — no bloqueante"
```

- [ ] **Step 7: Commit**

```bash
git add .github/scripts/register-task-def-with-image.sh .github/scripts/tests/register-task-def-with-image.test.sh
git commit -m "feat: add the script that re-registers a task definition with a real image"
```

---

### Task 2: El workflow de deploy

**Files:**
- Create: `.github/workflows/deploy-qa.yml`

**Interfaces:**
- Consumes: `.github/scripts/register-task-def-with-image.sh` (Task 1); del plan compañero, por nombre fijo (no por output automatizado — GitHub Actions no lee outputs de Terraform de otro repo): `qa-open-wearables` como nombre de cluster, `qa-open-wearables-backend`/`qa-open-wearables-frontend` como nombres de ECR, `qa-open-wearables-api`/`-celery-worker`/`-celery-worker-bulk`/`-celery-beat`/`-migration` como familias de task definition y nombres de servicio (salvo migration).
- Produces: nada que otra task de este plan consuma — es la hoja del árbol.

- [ ] **Step 1: Escribir el workflow**

`.github/workflows/deploy-qa.yml`:

```yaml
name: Deploy to ECS (qa)

on:
  push:
    branches: [main]

permissions:
  id-token: write
  contents: read

env:
  AWS_REGION: us-west-2
  CLUSTER: qa-open-wearables
  BACKEND_ECR: qa-open-wearables-backend
  FRONTEND_ECR: qa-open-wearables-frontend

jobs:
  deploy:
    runs-on: ubuntu-latest
    environment: qa-open-wearables
    steps:
      - uses: actions/checkout@v4

      - uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ vars.AWS_DEPLOY_ROLE_ARN }}
          aws-region: ${{ env.AWS_REGION }}

      - uses: aws-actions/amazon-ecr-login@v2
        id: ecr-login

      - name: Resolve image URIs
        id: images
        run: |
          echo "backend=${{ steps.ecr-login.outputs.registry }}/${{ env.BACKEND_ECR }}:${{ github.sha }}" >> "$GITHUB_OUTPUT"
          echo "frontend=${{ steps.ecr-login.outputs.registry }}/${{ env.FRONTEND_ECR }}:${{ github.sha }}" >> "$GITHUB_OUTPUT"

      - name: Build and push backend image
        run: |
          docker build \
            --build-arg GIT_SHA=${{ github.sha }} \
            -t ${{ steps.images.outputs.backend }} \
            backend
          docker push ${{ steps.images.outputs.backend }}

      - name: Build and push frontend image
        run: |
          docker build \
            --build-arg VITE_API_URL=https://wearables-api-qa.longevo.com \
            -t ${{ steps.images.outputs.frontend }} \
            frontend
          docker push ${{ steps.images.outputs.frontend }}

      - name: Register migration task definition with the new image
        id: migration-td
        run: |
          arn=$(.github/scripts/register-task-def-with-image.sh qa-open-wearables-migration "${{ steps.images.outputs.backend }}")
          echo "arn=$arn" >> "$GITHUB_OUTPUT"

      - name: Get private subnets and migration security group
        id: network
        run: |
          # Private subnets are tagged Name = "qa-open-wearables-private-<az>"
          # by the platform plan's network.tf — filtering by that tag avoids
          # having to resolve the VPC ID first (describe-clusters doesn't
          # return one; it would need a second round trip through the ENI or
          # the service's network configuration).
          subnets=$(aws ec2 describe-subnets \
            --filters "Name=tag:Name,Values=${{ env.CLUSTER }}-private-*" \
            --query 'Subnets[].SubnetId' --output text | tr '\t' ',')
          sg=$(aws ec2 describe-security-groups \
            --filters "Name=group-name,Values=${{ env.CLUSTER }}-migration" \
            --query 'SecurityGroups[0].GroupId' --output text)
          echo "subnets=$subnets" >> "$GITHUB_OUTPUT"
          echo "sg=$sg" >> "$GITHUB_OUTPUT"

      - name: Run migration — abort the deploy if this fails
        run: |
          task_arn=$(aws ecs run-task \
            --cluster "$CLUSTER" \
            --task-definition "${{ steps.migration-td.outputs.arn }}" \
            --launch-type FARGATE \
            --network-configuration "awsvpcConfiguration={subnets=[${{ steps.network.outputs.subnets }}],securityGroups=[${{ steps.network.outputs.sg }}],assignPublicIp=DISABLED}" \
            --query 'tasks[0].taskArn' --output text)
          echo "Migration task: $task_arn"
          aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$task_arn"
          exit_code=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task_arn" --query 'tasks[0].containers[0].exitCode' --output text)
          if [ "$exit_code" != "0" ]; then
            echo "::error::Migration failed with exit code $exit_code — no service will be updated. Check the /ecs/qa-open-wearables-migration log group."
            exit 1
          fi

      - name: Register and deploy api, frontend, celery-worker, celery-worker-bulk
        run: |
          declare -A families=(
            [api]="qa-open-wearables-api:${{ steps.images.outputs.backend }}"
            [frontend]="qa-open-wearables-frontend:${{ steps.images.outputs.frontend }}"
            [celery-worker]="qa-open-wearables-celery-worker:${{ steps.images.outputs.backend }}"
            [celery-worker-bulk]="qa-open-wearables-celery-worker-bulk:${{ steps.images.outputs.backend }}"
          )
          for service in "${!families[@]}"; do
            family="${families[$service]%%:*}"
            image="${families[$service]#*:}"
            td_arn=$(.github/scripts/register-task-def-with-image.sh "$family" "$image")
            echo "Deploying $service with $td_arn"
            aws ecs update-service --cluster "$CLUSTER" --service "$family" --task-definition "$td_arn" > /dev/null
          done
          aws ecs wait services-stable --cluster "$CLUSTER" \
            --services qa-open-wearables-api qa-open-wearables-frontend qa-open-wearables-celery-worker qa-open-wearables-celery-worker-bulk

      - name: Register and deploy celery-beat (last, on its own)
        run: |
          td_arn=$(.github/scripts/register-task-def-with-image.sh qa-open-wearables-celery-beat "${{ steps.images.outputs.backend }}")
          aws ecs update-service --cluster "$CLUSTER" --service qa-open-wearables-celery-beat --task-definition "$td_arn" > /dev/null
          aws ecs wait services-stable --cluster "$CLUSTER" --services qa-open-wearables-celery-beat
```

Nota sobre el paso "Get private subnets and migration security group": el filtro por `Name=tag:Name,Values=qa-open-wearables-private-*` depende de los tags `Name = "${local.name}-private-${each.key}"` que `aws_subnet.private` ya trae desde el plan de plataforma (`src/open-wearables/environments/qa/network.tf`). Si el naming cambiara ahí, este paso no encuentra subnets y falla ruidosamente en el `describe-subnets` — preferible a fallar en silencio con una lista vacía.

- [ ] **Step 2: Verificar sintaxis del YAML**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/deploy-qa.yml'))" && echo "YAML válido"
```

- [ ] **Step 3: Verificar que `actionlint` (si está disponible) no encuentra errores**

```bash
which actionlint > /dev/null && actionlint .github/workflows/deploy-qa.yml || echo "actionlint no instalado, se salteó — no bloqueante"
```

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/deploy-qa.yml
git commit -m "feat: add the ecs deploy workflow for qa"
```

---

## Antes de abrir el PR

1. **Este workflow no se puede probar de punta a punta hasta que `2026-09-01-platform-services-qa.md` esté aplicado en `longevoIac`.** El PR se puede abrir y revisar por forma (sintaxis, lógica del script, orden de los pasos) antes de eso, pero el primer push a `main` después de mergear va a **fallar** si el cluster/servicios/rol no existen todavía. Coordinar el orden: mergear el plan de plataforma primero, confirmar que aplicó, recién ahí mergear este.
2. **La variable de entorno `AWS_DEPLOY_ROLE_ARN`** tiene que crearse en el entorno de GitHub `qa-open-wearables` (Settings → Environments → `qa-open-wearables` → Variables) con el valor del output `deploy_role_arn` del plan compañero, después de que ese plan aplique. Sin esto, `aws-actions/configure-aws-credentials` falla al no tener qué rol asumir.
3. **El primer deploy real es también el primer chequeo de correctitud de las env vars/secrets** que el plan de plataforma inyectó — si `DB_NAME`, `MASTER_KEY`, o cualquier otro valor está mal, la migración va a fallar ahí, con logs en CloudWatch (`/ecs/qa-open-wearables-migration`). Es esperable tener que iterar una o dos veces antes de que un deploy pase limpio.

## Fuera del alcance de este plan

| Qué | Dónde va |
|---|---|
| Crear el cluster, los servicios, el rol de deploy | `2026-09-01-platform-services-qa.md`, en `longevoIac` — este plan lo consume, no lo crea |
| Rollback automático más allá del circuit breaker de ECS (`deployment_circuit_breaker { rollback = true }`, ya configurado en el plan de plataforma) | No hay lógica de rollback adicional en el workflow — si el circuit breaker de ECS no alcanza, es manual |
| Notificaciones de deploy (Slack, etc.) | No pedido, no se agrega — YAGNI |
| El mismo workflow para prod | Se escribe cuando exista el plan de plataforma de prod, probablemente como un segundo job o un workflow separado con el mismo script |
