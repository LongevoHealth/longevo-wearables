# Plataforma y servicios ECS en qa — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Crear en `qa` el cluster ECS, el ALB con WAF, los cinco servicios de open-wearables (api, worker, worker-bulk, beat, frontend), el bootstrap de los roles de Postgres, y el rol OIDC que el pipeline de deploy del fork va a usar para pushear imágenes y actualizar servicios.

**Architecture:** Todo se agrega como archivos nuevos en el mismo proyecto Terraform `src/open-wearables/environments/qa/` que ya existe (VPC, Aurora, Valkey de un plan anterior). Cinco servicios comparten sólo dos imágenes de ECR — api/worker/worker-bulk/beat corren la misma imagen del backend con distinto `command`, frontend tiene la suya — así que **no** se usa el módulo compartido `src/modules/ecs/base` (crea un ECR por invocación, y forzarlo a cinco crearía cuatro repos redundantes). Se escribe un módulo **project-scoped** `src/open-wearables/modules/service/` para el patrón que sí se repite cinco veces (log group, security group, task definition, service, autoscaling), siguiendo la convención del repo de que un módulo scoped a un proyecto es para reutilización *dentro* de ese proyecto, no una abstracción prematura.

**Tech Stack:** Terraform >= 1.14.0, `hashicorp/aws ~> 5.0`, `hashicorp/random ~> 3.0`. Imagen `postgres:17-alpine` (oficial, sólo para el bootstrap de roles — no es la imagen de la app).

**Spec:** `docs/superpowers/specs/2026-08-22-aws-infra-design.md` (secciones 4, 5, 6). Compañero: `docs/superpowers/plans/2026-09-01-deploy-pipeline-qa.md` (workflow de GitHub Actions, en este mismo repo — consume los outputs de este plan).

## Global Constraints

- **El trabajo ocurre en `/Users/manupandolfi/Longevo/longevoIac`**, rama base `master`. Crear una rama de feature antes del primer commit.
- **Ningún módulo del registry.** Estilo de la casa: recursos propios o módulos locales, nunca terraform-aws-modules.
- **El módulo `service` es project-scoped**, vive en `src/open-wearables/modules/service/`, y sólo lo consume este proyecto. No es un módulo compartido en `src/modules/`.
- Región de `qa`: `us-west-2`, vía `module.global_constants.qa_aws_region`. Nunca hardcodear.
- Nombre del proyecto: `open-wearables`. Prefijo de recursos: `${environment}-${project}-<nombre>` = `qa-open-wearables-<nombre>`.
- **Sizing de qa — mínimo, no el de prod:** api 0.25 vCPU/512 MB, celery-worker 0.5 vCPU/1024 MB, celery-worker-bulk 1 vCPU/2048 MB, celery-beat 0.25 vCPU/512 MB, frontend 0.25 vCPU/512 MB. `desired_count = 1` en los cinco (no 2 como en prod). Estos son pares válidos de la tabla de Fargate (256↔512-2048MB, 512↔1024-4096MB, 1024↔2048-8192MB).
- **La imagen del backend es compartida**: api, celery-worker, celery-worker-bulk y celery-beat usan el mismo ECR repo y el mismo tag; sólo cambia el `command` de la task definition. Frontend usa un ECR repo propio.
- **Todas las task definitions se registran con imagen placeholder** (`public.ecr.aws/docker/library/busybox:latest`) y `lifecycle { ignore_changes = [container_definitions] }`, igual que hace `src/modules/ecs/base`. El pipeline de deploy (plan compañero) registra las revisiones reales.
- **`DB_NAME` debe ser `open_wearables`** (con guion bajo) en cada task que toque la base — el default de la app es `open-wearables` (guion), que RDS rechaza como nombre de base; la base real ya se creó como `open_wearables` en el plan de datos.
- La cola de Celery ahora es configurable: `worker.sh` lee `CELERY_QUEUES`, default `default,sdk_sync,garmin_sync,webhook_sync` si no se setea. Confirmado mergeado en `longevo-wearables#2`. `celery-worker-bulk` la pisa con `CELERY_QUEUES=sdk_sync`; `celery-worker` no la setea (usa el default menos `sdk_sync`... — **no**, el default incluye las cuatro colas. `celery-worker` debe fijar explícitamente `CELERY_QUEUES=default,garmin_sync,webhook_sync` para no competir con el worker de backfill por `sdk_sync`.
- **Hostnames confirmados:** `wearables-api-qa.longevo.com` (API) y `wearables-app-qa.longevo.com` (frontend), bajo el certificado wildcard `*.longevo.com` ya emitido en `us-west-2` (vence 2027-03-08, no gestionado por ningún state de Terraform — se referencia con `data`, nunca se crea).
- La zona hospedada `longevo.com.` (`Z04001102YDPRZ2Z97Y2X`) ya existe; el rol de apply del repo tiene `route53:*`, así que Terraform puede agregar registros ahí sin tocar los existentes.
- Health checks: API responde `GET /` con `{"message": "Server is running!"}` sin tocar la base — es liveness, no readiness de la base. Frontend responde `GET /` en :3000.
- El endpoint de la API es `https://wearables-api-qa.longevo.com`; el SDK móvil lo va a hardcodear, así que el hostname no se cambia después de este punto sin coordinar una nueva release de la app.
- Verificación local en cada task: `terraform fmt -recursive`, `terraform init -reconfigure` (con backend real — ya existe state), `terraform validate`, y `terraform plan` de sólo lectura contra la cuenta real cuando el task lo pide explícitamente. Usar `AWS_PROFILE=manu-longevo-app-mfa` para todo comando que toque AWS.
- Commits con conventional commits.

## Estructura de archivos

Todo bajo `/Users/manupandolfi/Longevo/longevoIac/src/open-wearables/`:

| Archivo | Responsabilidad | Task |
|---|---|---|
| `modules/service/variables.tf` | Interfaz del módulo project-scoped (nuevo) | 1 |
| `modules/service/main.tf` | Log group, SG, task definition, service, autoscaling (nuevo) | 1 |
| `modules/service/outputs.tf` | security_group_id, service_name (nuevo) | 1 |
| `environments/qa/ecr.tf` | Dos repos ECR (backend, frontend) (nuevo) | 2 |
| `environments/qa/execution-role.tf` | Rol de ejecución dedicado (nuevo) | 2 |
| `environments/qa/cluster.tf` | `aws_ecs_cluster` (nuevo) | 3 |
| `environments/qa/secrets.tf` | SECRET_KEY, MASTER_KEY, ADMIN_PASSWORD, credenciales de `app`/`migrator` (nuevo) | 4 |
| `environments/qa/db-bootstrap.tf` | Task de bootstrap de roles Postgres + `null_resource` que la dispara (nuevo) | 5 |
| `environments/qa/dns-alb-waf.tf` | Cert (data), ALB, listener, WAF, registros DNS (nuevo) | 6 |
| `environments/qa/migration-task.tf` | Task definition de migración, sin servicio (nuevo) | 7 |
| `environments/qa/service-api.tf` | Servicio api + target group + regla de listener (nuevo) | 8 |
| `environments/qa/service-frontend.tf` | Servicio frontend + target group + regla de listener (nuevo) | 9 |
| `environments/qa/service-worker.tf` | Servicio celery-worker (nuevo) | 10 |
| `environments/qa/service-worker-bulk.tf` | Servicio celery-worker-bulk (nuevo) | 10 |
| `environments/qa/service-beat.tf` | Servicio celery-beat, sin autoscaling (nuevo) | 11 |
| `environments/qa/deploy-role.tf` | Rol OIDC para `longevo-wearables` (nuevo) | 12 |
| `environments/qa/outputs.tf` | Se **extiende** (ya existe de un plan anterior) | 13 |

---

### Task 1: Módulo project-scoped `service`

El molde que usan los cinco servicios: log group cifrado con la CMK del ambiente, security group vacío de ingress (el caller decide quién entra), task definition con imagen placeholder, `aws_ecs_service`, y autoscaling opcional por CPU.

**Files:**
- Create: `src/open-wearables/modules/service/variables.tf`
- Create: `src/open-wearables/modules/service/main.tf`
- Create: `src/open-wearables/modules/service/outputs.tf`

**Interfaces:**
- Consumes: nada de otras tasks — es la base de todas las siguientes.
- Produces: `module.<caller>.security_group_id`, `module.<caller>.service_name`, `module.<caller>.task_definition_family` — los consumen las Tasks 8 a 12, y las reglas de ingress de Aurora/Valkey en la Task 4.

- [ ] **Step 1: Crear la rama**

```bash
cd /Users/manupandolfi/Longevo/longevoIac && git switch master && git pull && git switch -c feat/open-wearables-qa-platform
```

- [ ] **Step 2: Escribir las variables del módulo**

`src/open-wearables/modules/service/variables.tf`:

```hcl
variable "name" {
  type        = string
  description = "Workload name, e.g. \"api\" or \"celery-worker-bulk\". Combined with environment/project to prefix every resource."
}

variable "environment" {
  type = string
}

variable "project" {
  type = string
}

variable "cluster_id" {
  type = string
}

variable "vpc_id" {
  type = string
}

variable "subnet_ids" {
  type        = list(string)
  description = "Private subnets the task's ENI attaches to."
}

variable "kms_key_arn" {
  type        = string
  description = "Environment CMK, used to encrypt the log group."
}

variable "execution_role_arn" {
  type = string
}

variable "task_role_arn" {
  type = string
}

variable "cpu" {
  type = number
}

variable "memory" {
  type = number
}

variable "command" {
  type        = list(string)
  description = "Container command override. Null keeps the image's own entrypoint/CMD."
  default     = null
}

variable "container_port" {
  type        = number
  description = "Port the container listens on. Null for workers with no inbound traffic."
  default     = null
}

variable "environment_variables" {
  type    = map(string)
  default = {}
}

variable "secrets" {
  type        = map(string)
  description = "Map of container env var name to Secrets Manager secret ARN, injected via ECS `secrets`."
  default     = {}
}

variable "ingress_security_group_ids" {
  type        = list(string)
  description = "Security groups allowed to reach container_port. Empty for workers with no inbound traffic."
  default     = []
}

variable "target_group_arn" {
  type        = string
  description = "ALB target group to register the service against. Null for services with no load balancer."
  default     = null
}

variable "desired_count" {
  type    = number
  default = 1
}

variable "deployment_minimum_healthy_percent" {
  type    = number
  default = 100
}

variable "deployment_maximum_percent" {
  type    = number
  default = 200
}

variable "autoscaling" {
  type = object({
    min_capacity       = number
    max_capacity       = number
    cpu_target_percent = number
  })
  description = "Null disables autoscaling — the service stays pinned at desired_count."
  default     = null
}
```

- [ ] **Step 3: Escribir el módulo**

`src/open-wearables/modules/service/main.tf`:

```hcl
locals {
  name = "${var.environment}-${var.project}-${var.name}"
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/ecs/${local.name}"
  retention_in_days = 14
  kms_key_id        = var.kms_key_arn
}

# Ingress is added by the caller via ingress_security_group_ids; a service with
# no inbound traffic (a worker) gets an empty list and stays closed by default.
resource "aws_security_group" "this" {
  name        = "${local.name}-sg"
  description = "ECS service ${local.name}"
  vpc_id      = var.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name}-sg"
  }
}

resource "aws_security_group_rule" "ingress" {
  for_each = toset(var.ingress_security_group_ids)

  type                     = "ingress"
  from_port                = var.container_port
  to_port                  = var.container_port
  protocol                 = "tcp"
  security_group_id        = aws_security_group.this.id
  source_security_group_id = each.value
}

resource "aws_ecs_task_definition" "this" {
  family                   = local.name
  cpu                      = var.cpu
  memory                   = var.memory
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.task_role_arn

  container_definitions = jsonencode([{
    name      = var.name
    image     = "public.ecr.aws/docker/library/busybox:latest"
    essential = true
    command   = var.command
    portMappings = var.container_port == null ? [] : [{
      containerPort = var.container_port
      protocol      = "tcp"
    }]
    environment = [for k, v in var.environment_variables : { name = k, value = v }]
    secrets     = [for k, arn in var.secrets : { name = k, valueFrom = arn }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.this.name
        "awslogs-region"        = data.aws_region.current.name
        "awslogs-stream-prefix" = var.name
      }
    }
  }])

  # A placeholder image and empty command/secrets here; the deploy pipeline
  # registers the real revision with the built image and full container spec.
  lifecycle {
    ignore_changes = [container_definitions]
  }
}

data "aws_region" "current" {}

resource "aws_ecs_service" "this" {
  name            = local.name
  cluster         = var.cluster_id
  task_definition = aws_ecs_task_definition.this.arn
  desired_count   = var.desired_count
  launch_type     = "FARGATE"

  enable_execute_command = true

  network_configuration {
    subnets         = var.subnet_ids
    security_groups = [aws_security_group.this.id]
    # Private subnets, no public IP — there is no route to the internet from
    # here at all, assigning one would just fail to attach.
    assign_public_ip = false
  }

  dynamic "load_balancer" {
    for_each = var.target_group_arn == null ? [] : [var.target_group_arn]
    content {
      target_group_arn = load_balancer.value
      container_name   = var.name
      container_port   = var.container_port
    }
  }

  deployment_minimum_healthy_percent = var.deployment_minimum_healthy_percent
  deployment_maximum_percent         = var.deployment_maximum_percent

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # The deploy pipeline registers new task definition revisions directly;
  # Terraform should not fight it back to the placeholder on every apply.
  lifecycle {
    ignore_changes = [task_definition, desired_count]
  }
}

resource "aws_appautoscaling_target" "this" {
  count = var.autoscaling == null ? 0 : 1

  service_namespace  = "ecs"
  resource_id        = "service/${var.cluster_id}/${aws_ecs_service.this.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.autoscaling.min_capacity
  max_capacity       = var.autoscaling.max_capacity
}

resource "aws_appautoscaling_policy" "cpu" {
  count = var.autoscaling == null ? 0 : 1

  name               = "${local.name}-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.this[0].service_namespace
  resource_id        = aws_appautoscaling_target.this[0].resource_id
  scalable_dimension = aws_appautoscaling_target.this[0].scalable_dimension

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
    target_value = var.autoscaling.cpu_target_percent
  }
}
```

Nota: `cluster_id` recibe el **nombre** del cluster, no el ARN — todos los callers de este módulo (Tasks 8 a 11) pasan `cluster_id = aws_ecs_cluster.main.name`. `aws_ecs_service.this`'s `resource_id` para autoscaling (`"service/${var.cluster_id}/..."`) depende de eso; no pasar `.id` ni `.arn` de `aws_ecs_cluster.main` aquí.

- [ ] **Step 4: Escribir los outputs**

`src/open-wearables/modules/service/outputs.tf`:

```hcl
output "security_group_id" {
  value = aws_security_group.this.id
}

output "service_name" {
  value = aws_ecs_service.this.name
}

output "task_definition_family" {
  value = aws_ecs_task_definition.this.family
}

output "task_definition_arn" {
  value = aws_ecs_task_definition.this.arn
}
```

- [ ] **Step 5: Verificar sintaxis del módulo de forma aislada**

Un módulo no se puede `validate` solo sin un caller — crear un caller temporal mínimo para probarlo:

```bash
mkdir -p /tmp/service-module-check && cd /tmp/service-module-check
cat > main.tf <<'EOF'
terraform {
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}
provider "aws" { region = "us-west-2" }

module "check" {
  source                 = "/Users/manupandolfi/Longevo/longevoIac/src/open-wearables/modules/service"
  name                    = "check"
  environment             = "qa"
  project                 = "open-wearables"
  cluster_id              = "arn:aws:ecs:us-west-2:000000000000:cluster/fake"
  vpc_id                  = "vpc-00000000"
  subnet_ids              = ["subnet-00000000"]
  kms_key_arn             = "arn:aws:kms:us-west-2:000000000000:key/fake"
  execution_role_arn      = "arn:aws:iam::000000000000:role/fake"
  task_role_arn           = "arn:aws:iam::000000000000:role/fake"
  cpu                     = 256
  memory                  = 512
}
EOF
export PATH="/opt/homebrew/bin:$PATH"
terraform init -backend=false && terraform validate
```

Expected: `Success! The configuration is valid.` Borrar `/tmp/service-module-check` después.

- [ ] **Step 6: Commit**

```bash
cd /Users/manupandolfi/Longevo/longevoIac
git add src/open-wearables/modules/service
git commit -m "feat(open-wearables): add the project-scoped ECS service module"
```

---

### Task 2: ECR repos y rol de ejecución dedicado

**Files:**
- Create: `src/open-wearables/environments/qa/ecr.tf`
- Create: `src/open-wearables/environments/qa/execution-role.tf`

**Interfaces:**
- Consumes: `local.name`, `aws_kms_key.main` (de un plan anterior, ya en `kms.tf`).
- Produces: `aws_ecr_repository.backend`, `aws_ecr_repository.frontend`, `aws_iam_role.execution` — los consumen todas las tasks de servicios (7 en adelante) y el rol de deploy (Task 12).

- [ ] **Step 1: Escribir los repos ECR**

`src/open-wearables/environments/qa/ecr.tf`:

```hcl
# One shared image for api/worker/worker-bulk/beat — same command pattern as the
# docker-compose file this fork ships. Frontend is its own image.
resource "aws_ecr_repository" "backend" {
  name                 = "${local.name}-backend"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "frontend" {
  name                 = "${local.name}-frontend"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "backend" {
  repository = aws_ecr_repository.backend.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 20 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 20
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_ecr_lifecycle_policy" "frontend" {
  repository = aws_ecr_repository.frontend.name
  policy      = aws_ecr_lifecycle_policy.backend.policy
}
```

- [ ] **Step 2: Escribir el rol de ejecución**

`src/open-wearables/environments/qa/execution-role.tf`:

```hcl
module "tasks_assume_policy" {
  source = "../../../modules/ecs/tasks-assume-policy"
}

# Dedicated, not the account-wide global-ecs-task-execution role: that one only
# carries the managed AmazonECSTaskExecutionRolePolicy (ECR pull, basic log
# write), with no Secrets Manager or KMS access — and every task here injects
# secrets encrypted with the environment CMK.
resource "aws_iam_role" "execution" {
  name               = "${local.name}-execution"
  assume_role_policy = module.tasks_assume_policy.json

  tags = {
    Name = "${local.name}-execution"
  }
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# The key's own policy already delegates to IAM (the EnableIAMUserPermissions
# statement from kms.tf), so granting Decrypt here is enough — no key policy
# change needed.
data "aws_iam_policy_document" "execution_extra" {
  statement {
    sid       = "DecryptSecrets"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.main.arn]
  }

  statement {
    sid    = "ReadSecrets"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
    ]
    # Scoped in Task 4 by appending the app/migrator/valkey/aurora secret ARNs
    # once they exist; wildcard-by-prefix here is intentionally tight, not "*".
    resources = ["arn:aws:secretsmanager:${module.global_constants.qa_aws_region}:${data.aws_caller_identity.current.account_id}:secret:${local.name}-*"]
  }
}

resource "aws_iam_role_policy" "execution_extra" {
  name   = "secrets-and-kms"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_extra.json
}
```

Nota: la política de secrets usa un prefijo `${local.name}-*`, así que **todo secreto que las tasks necesiten leer tiene que nombrarse con ese prefijo** (`qa-open-wearables-<algo>`). La Task 4 lo respeta. El secreto del master de Aurora (gestionado por RDS, con un nombre `rds!cluster-...` que no sigue este prefijo) no lo usa ninguna task de aplicación — sólo el bootstrap, que tiene su propio rol de task, no éste.

- [ ] **Step 3: Verificar**

```bash
cd /Users/manupandolfi/Longevo/longevoIac/src/open-wearables/environments/qa
export PATH="/opt/homebrew/bin:$PATH"
AWS_PROFILE=manu-longevo-app-mfa terraform init -reconfigure -input=false
terraform fmt && terraform validate
```

Expected: válido. Este es el primer task de este plan que corre contra el backend real (ya existe state de un plan anterior) — usar `terraform plan`, no `apply`, para ver qué se agregaría:

```bash
AWS_PROFILE=manu-longevo-app-mfa terraform plan -input=false -no-color 2>&1 | tail -40
```

Expected: sólo altas (`+`) para los recursos de este task, ningún cambio inesperado en lo que ya existe (Aurora, Valkey, red). Si aparece algo más, no seguir — reportar qué es.

- [ ] **Step 4: Commit**

```bash
git add src/open-wearables/environments/qa/ecr.tf src/open-wearables/environments/qa/execution-role.tf
git commit -m "feat(open-wearables): add ecr repositories and a dedicated execution role"
```

---

### Task 3: Cluster ECS

**Files:**
- Create: `src/open-wearables/environments/qa/cluster.tf`

**Interfaces:**
- Consumes: `local.name`.
- Produces: `aws_ecs_cluster.main` — lo consumen todas las tasks de servicios.

- [ ] **Step 1: Escribir el archivo**

`src/open-wearables/environments/qa/cluster.tf`:

```hcl
resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = {
    Name = local.name
  }
}
```

- [ ] **Step 2: Verificar**

```bash
cd /Users/manupandolfi/Longevo/longevoIac/src/open-wearables/environments/qa
terraform fmt && terraform validate
```

- [ ] **Step 3: Commit**

```bash
git add src/open-wearables/environments/qa/cluster.tf
git commit -m "feat(open-wearables): add the ecs cluster"
```

---

### Task 4: Secretos de la aplicación

`SECRET_KEY` (firma JWT), `MASTER_KEY` (clave Fernet que cifra los provider settings guardados), `ADMIN_PASSWORD` (seed del admin inicial), y las credenciales de los dos roles de Postgres que el bootstrap (Task 5) va a crear.

**Files:**
- Create: `src/open-wearables/environments/qa/secrets.tf`

**Interfaces:**
- Consumes: `local.name`, `aws_kms_key.main`.
- Produces: `aws_secretsmanager_secret.secret_key`, `.master_key`, `.admin_password`, `.db_app_password`, `.db_migrator_password` — los consumen la Task 5 (bootstrap) y las Tasks 7 a 11 (servicios).

- [ ] **Step 1: Escribir el archivo**

`src/open-wearables/environments/qa/secrets.tf`:

```hcl
# JWT signing secret.
resource "random_password" "secret_key" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "secret_key" {
  name       = "${local.name}-secret-key"
  kms_key_id = aws_kms_key.main.arn
}

resource "aws_secretsmanager_secret_version" "secret_key" {
  secret_id     = aws_secretsmanager_secret.secret_key.id
  secret_string = random_password.secret_key.result
}

# Fernet key: 32 raw random bytes, base64-urlsafe encoded. random_id's b64_url
# uses that same charset but is unpadded (see the padding fix on the secret
# version below) — with the appended "=", this matches what
# cryptography.fernet.Fernet.generate_key() produces, without a Python step.
resource "random_id" "master_key" {
  byte_length = 32
}

resource "aws_secretsmanager_secret" "master_key" {
  name       = "${local.name}-master-key"
  kms_key_id = aws_kms_key.main.arn
}

resource "aws_secretsmanager_secret_version" "master_key" {
  secret_id     = aws_secretsmanager_secret.master_key.id
  # b64_url is unpadded RawURLEncoding (43 chars for 32 bytes) — Fernet requires
  # the padded 44-char form. Confirmed against the random provider's source
  # (base64.RawURLEncoding), not assumed.
  secret_string = "${random_id.master_key.b64_url}="
}

resource "random_password" "admin_password" {
  length  = 32
  special = true
  # Avoid characters that need shell-escaping if this ever gets pasted into a
  # terminal by whoever seeds the first admin account.
  override_special = "!@#%^&*()-_=+"
}

resource "aws_secretsmanager_secret" "admin_password" {
  name       = "${local.name}-admin-password"
  kms_key_id = aws_kms_key.main.arn
}

resource "aws_secretsmanager_secret_version" "admin_password" {
  secret_id     = aws_secretsmanager_secret.admin_password.id
  secret_string = random_password.admin_password.result
}

# Runtime DB role — no CREATEDB, no SUPERUSER. Created by the bootstrap task
# (db-bootstrap.tf), not by Terraform directly: GitHub Actions runners have no
# route to Aurora in the isolated subnets, only an ECS task inside the VPC does.
resource "random_password" "db_app_password" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "db_app_password" {
  name       = "${local.name}-db-app-password"
  kms_key_id = aws_kms_key.main.arn
}

resource "aws_secretsmanager_secret_version" "db_app_password" {
  secret_id     = aws_secretsmanager_secret.db_app_password.id
  secret_string = random_password.db_app_password.result
}

# Migration DB role — DDL only, used solely by the migration task (Task 7).
resource "random_password" "db_migrator_password" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "db_migrator_password" {
  name       = "${local.name}-db-migrator-password"
  kms_key_id = aws_kms_key.main.arn
}

resource "aws_secretsmanager_secret_version" "db_migrator_password" {
  secret_id     = aws_secretsmanager_secret.db_migrator_password.id
  secret_string = random_password.db_migrator_password.result
}
```

- [ ] **Step 2: Verificar**

```bash
cd /Users/manupandolfi/Longevo/longevoIac/src/open-wearables/environments/qa
terraform fmt && terraform validate
```

- [ ] **Step 3: Confirmar que `b64_url` de `random_id` produce un Fernet key válido**

Esto no se puede verificar con `terraform validate` — es una propiedad del valor generado, no de la sintaxis. Verificar con Python después del primer `apply` de este task (no antes, el valor no existe todavía):

```bash
export PATH="/opt/homebrew/bin:$PATH"
AWS_PROFILE=manu-longevo-app-mfa aws secretsmanager get-secret-value --secret-id qa-open-wearables-master-key --query SecretString --output text | python3 -c "
import sys
from cryptography.fernet import Fernet
key = sys.stdin.read().strip()
Fernet(key.encode())  # raises ValueError if the key is not a valid Fernet key
print('valid Fernet key')
"
```

El código ya agrega el padding (`"${random_id.master_key.b64_url}="`) — `b64_url` por sí solo es unpadded RawURLEncoding (43 caracteres para 32 bytes) y Fernet exige exactamente 44 terminados en `=`, confirmado contra el código fuente del provider `hashicorp/random`, no supuesto. Este paso confirma que el valor real en Secrets Manager es un Fernet key válido, no diagnostica un problema esperado.

Este paso requiere que Task 4 ya esté aplicada (`apply`, no sólo `plan`) — no lo puede correr el implementador de esta task en modo aislado; queda para el controller verificar después de que el PR aplique, o para quien ejecute el `apply` real. Anotar en el reporte que este paso quedó pendiente de verificación post-apply.

- [ ] **Step 4: Commit**

```bash
git add src/open-wearables/environments/qa/secrets.tf
git commit -m "feat(open-wearables): add application secrets"
```

---

### Task 5: Bootstrap de los roles de Postgres

Un `null_resource` con `local-exec` que dispara un `aws ecs run-task` cada vez que las contraseñas cambian. La task en sí corre `postgres:17-alpine` (no la imagen de la app) dentro de la VPC, con el secreto master de RDS y crea los roles `app`/`migrator` de forma idempotente.

**Files:**
- Create: `src/open-wearables/environments/qa/db-bootstrap.tf`

**Interfaces:**
- Consumes: `aws_rds_cluster.main`, `aws_security_group.aurora`, `aws_rds_cluster.main.master_user_secret[0].secret_arn` (de un plan anterior), `aws_secretsmanager_secret.db_app_password`, `.db_migrator_password` (Task 4), `aws_ecs_cluster.main` (Task 3).
- Produces: nada que otra task consuma — es un efecto (los roles existen en la base), no un recurso referenciable.

- [ ] **Step 1: Escribir el archivo**

`src/open-wearables/environments/qa/db-bootstrap.tf`:

```hcl
# Own role and SG — this task talks to Aurora with the RDS master secret, which
# no application task role should ever have access to.
data "aws_iam_policy_document" "db_bootstrap_task_extra" {
  statement {
    sid       = "ReadBootstrapSecrets"
    effect    = "Allow"
    actions    = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_rds_cluster.main.master_user_secret[0].secret_arn,
      aws_secretsmanager_secret.db_app_password.arn,
      aws_secretsmanager_secret.db_migrator_password.arn,
    ]
  }
}

resource "aws_iam_role" "db_bootstrap_task" {
  name               = "${local.name}-db-bootstrap-task"
  assume_role_policy = module.tasks_assume_policy.json
}

resource "aws_iam_role_policy" "db_bootstrap_task" {
  name   = "read-secrets"
  role   = aws_iam_role.db_bootstrap_task.id
  policy = data.aws_iam_policy_document.db_bootstrap_task_extra.json
}

resource "aws_security_group" "db_bootstrap" {
  name        = "${local.name}-db-bootstrap"
  description = "Bootstrap task connecting to Aurora as master"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group_rule" "aurora_from_bootstrap" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.aurora.id
  source_security_group_id = aws_security_group.db_bootstrap.id
}

resource "aws_cloudwatch_log_group" "db_bootstrap" {
  name              = "/ecs/${local.name}-db-bootstrap"
  retention_in_days = 14
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_ecs_task_definition" "db_bootstrap" {
  family                   = "${local.name}-db-bootstrap"
  cpu                      = 256
  memory                   = 512
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.db_bootstrap_task.arn

  container_definitions = jsonencode([{
    name      = "db-bootstrap"
    image     = "public.ecr.aws/docker/library/postgres:17-alpine"
    essential = true
    # Idempotent: safe to run on every apply that changes a password. DO $$ ...
    # END $$ blocks let CREATE ROLE be conditional, which plain SQL can't do.
    # MASTER_DSN_JSON is the raw RDS-managed secret — a JSON blob
    # ({"username":...,"password":...,"host":...}), not a ready DSN — so the
    # command parses it with grep/cut rather than jq, which alpine's base image
    # does not carry (sh/grep/cut do, no extra install needed).
    command = ["sh", "-c", <<-EOT
      set -e
      HOST=$(echo "$MASTER_DSN_JSON" | grep -o '"host":"[^"]*"' | cut -d'"' -f4)
      PASSWORD=$(echo "$MASTER_DSN_JSON" | grep -o '"password":"[^"]*"' | cut -d'"' -f4)
      export PGPASSWORD="$PASSWORD"
      psql "host=$HOST port=5432 dbname=postgres user=postgres sslmode=require" <<'SQL'
      DO $$
      BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'app') THEN
          CREATE ROLE app LOGIN PASSWORD '${random_password.db_app_password.result}';
        ELSE
          ALTER ROLE app WITH PASSWORD '${random_password.db_app_password.result}';
        END IF;
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'migrator') THEN
          CREATE ROLE migrator LOGIN PASSWORD '${random_password.db_migrator_password.result}' CREATEDB;
        ELSE
          ALTER ROLE migrator WITH PASSWORD '${random_password.db_migrator_password.result}';
        END IF;
      END
      $$;
      GRANT ALL PRIVILEGES ON DATABASE open_wearables TO migrator;
      GRANT CONNECT ON DATABASE open_wearables TO app;
      SQL
    EOT
    ]
    environment = [
      { name = "PGSSLMODE", value = "require" }
    ]
    secrets = [
      { name = "MASTER_DSN_JSON", valueFrom = aws_rds_cluster.main.master_user_secret[0].secret_arn }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.db_bootstrap.name
        "awslogs-region"        = module.global_constants.qa_aws_region
        "awslogs-stream-prefix" = "db-bootstrap"
      }
    }
  }])
}
```

- [ ] **Step 2: Disparar el bootstrap desde `terraform apply`**

Agregar al final del mismo archivo:

```hcl
resource "null_resource" "db_bootstrap_run" {
  triggers = {
    task_definition_arn = aws_ecs_task_definition.db_bootstrap.arn
    app_password         = random_password.db_app_password.id
    migrator_password    = random_password.db_migrator_password.id
  }

  provisioner "local-exec" {
    command = <<-EOT
      set -e
      TASK_ARN=$(aws ecs run-task \
        --cluster ${aws_ecs_cluster.main.name} \
        --task-definition ${aws_ecs_task_definition.db_bootstrap.arn} \
        --launch-type FARGATE \
        --network-configuration "awsvpcConfiguration={subnets=[${join(",", [for s in aws_subnet.private : s.id])}],securityGroups=[${aws_security_group.db_bootstrap.id}],assignPublicIp=DISABLED}" \
        --region ${module.global_constants.qa_aws_region} \
        --query 'tasks[0].taskArn' --output text)
      echo "Bootstrap task: $TASK_ARN"
      aws ecs wait tasks-stopped --cluster ${aws_ecs_cluster.main.name} --tasks "$TASK_ARN" --region ${module.global_constants.qa_aws_region}
      EXIT_CODE=$(aws ecs describe-tasks --cluster ${aws_ecs_cluster.main.name} --tasks "$TASK_ARN" --region ${module.global_constants.qa_aws_region} --query 'tasks[0].containers[0].exitCode' --output text)
      if [ "$EXIT_CODE" != "0" ]; then
        echo "Bootstrap task exited with code $EXIT_CODE — check CloudWatch Logs group ${aws_cloudwatch_log_group.db_bootstrap.name}"
        exit 1
      fi
      echo "Bootstrap task succeeded"
    EOT
  }

  depends_on = [
    aws_ecs_task_definition.db_bootstrap,
    aws_security_group_rule.aurora_from_bootstrap,
  ]
}
```

**Esto corre en la máquina que ejecuta `terraform apply` — en CI, el runner de GitHub Actions.** El runner necesita el `aws` CLI disponible (ya lo usa `terraform-prepare`) y el rol de apply del CI necesita `ecs:RunTask`, `ecs:DescribeTasks`, `ecs:Wait*` — ya cubierto por el `ecs:*` regional que tiene `writes-policy`.

- [ ] **Step 3: Verificar sintaxis**

```bash
cd /Users/manupandolfi/Longevo/longevoIac/src/open-wearables/environments/qa
terraform fmt && terraform validate
```

**No correr `terraform apply` desde este task** — el `null_resource` con `local-exec` es real y crearía los roles contra la Aurora de qa de verdad. Eso lo decide el controller, no el implementador. Reportar `DONE_WITH_CONCERNS` si en algún punto se sintió la tentación de aplicar para probar — no hacerlo.

- [ ] **Step 4: Commit**

```bash
git add src/open-wearables/environments/qa/db-bootstrap.tf
git commit -m "feat(open-wearables): bootstrap the app and migrator postgres roles"
```

---

### Task 6: Certificado, ALB, WAF, DNS

**Files:**
- Create: `src/open-wearables/environments/qa/dns-alb-waf.tf`

**Interfaces:**
- Consumes: `aws_vpc.main`, `aws_subnet.public` (de un plan anterior).
- Produces: `aws_lb.main`, `aws_lb_listener.https`, `aws_security_group.alb` — los consumen las Tasks 8 y 9 (target groups y reglas de listener).

- [ ] **Step 1: Escribir el archivo**

`src/open-wearables/environments/qa/dns-alb-waf.tf`:

```hcl
# Not created here — an existing wildcard, issued for other ALBs in this
# account outside any Terraform state. Referenced, never adopted into state.
data "aws_acm_certificate" "wildcard" {
  domain      = "*.longevo.com"
  statuses    = ["ISSUED"]
  most_recent = true
}

data "aws_route53_zone" "longevo" {
  name = "longevo.com."
}

resource "aws_security_group" "alb" {
  name        = "${local.name}-alb"
  description = "Public ALB for ${local.name}"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTPS from the internet"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTP, redirected to HTTPS"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name}-alb"
  }
}

resource "aws_lb" "main" {
  name               = "${local.name}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb.id]
  subnets            = [for s in aws_subnet.public : s.id]

  tags = {
    Name = "${local.name}-alb"
  }
}

resource "aws_lb_listener" "http_redirect" {
  load_balancer_arn = aws_lb.main.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.main.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = data.aws_acm_certificate.wildcard.arn

  # No host matches either service's rule (Task 8/9) by default: reject rather
  # than silently routing an unexpected Host header to either backend.
  default_action {
    type = "fixed-response"
    fixed_response {
      content_type = "text/plain"
      message_body = "Not found"
      status_code  = "404"
    }
  }
}

resource "aws_route53_record" "api" {
  zone_id = data.aws_route53_zone.longevo.zone_id
  name    = "wearables-api-qa.longevo.com"
  type    = "A"

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}

resource "aws_route53_record" "app" {
  zone_id = data.aws_route53_zone.longevo.zone_id
  name    = "wearables-app-qa.longevo.com"
  type    = "A"

  alias {
    name                   = aws_lb.main.dns_name
    zone_id                = aws_lb.main.zone_id
    evaluate_target_health = true
  }
}

resource "aws_wafv2_web_acl" "main" {
  name  = "${local.name}-alb"
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  rule {
    name     = "core-rule-set"
    priority = 0

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"

        # SizeRestrictions_BODY blocks bodies over 8 KB by default — the SDK's
        # batches are bigger. Without this override, ingestion silently 403s.
        rule_action_override {
          name = "SizeRestrictions_BODY"
          action_to_use {
            allow {}
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-core-rule-set"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "known-bad-inputs"
    priority = 1

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-known-bad-inputs"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "ip-reputation"
    priority = 2

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesAmazonIpReputationList"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-ip-reputation"
      sampled_requests_enabled   = true
    }
  }

  # Rate-based, scoped to the ingestion path only, excluding the SNS callback
  # (SNS delivers in bursts, already signature-verified by the app). No
  # Anonymous-IP list: carrier NAT and mobile VPNs share IPs across many real
  # users, which the Anonymous IP list would flag as false positives.
  rule {
    name     = "sdk-ingest-rate-limit"
    priority = 3

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = 6000
        aggregate_key_type = "IP"

        scope_down_statement {
          and_statement {
            statement {
              byte_match_statement {
                search_string         = "/api/v1/sdk/"
                positional_constraint = "STARTS_WITH"
                field_to_match {
                  uri_path {}
                }
                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
            statement {
              not_statement {
                statement {
                  byte_match_statement {
                    search_string         = "/api/v1/sns/notification"
                    positional_constraint = "EXACTLY"
                    field_to_match {
                      uri_path {}
                    }
                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
              }
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name}-sdk-ingest-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.name}-alb"
    sampled_requests_enabled   = true
  }

  tags = {
    Name = "${local.name}-alb"
  }
}

resource "aws_wafv2_web_acl_association" "main" {
  resource_arn = aws_lb.main.arn
  web_acl_arn  = aws_wafv2_web_acl.main.arn
}
```

- [ ] **Step 2: Verificar que el certificado wildcard resuelve**

Antes de correr `terraform validate` (que no hace llamadas a AWS), confirmar que el `data` source va a encontrar algo:

```bash
export PATH="/opt/homebrew/bin:$PATH"
AWS_PROFILE=manu-longevo-app-mfa aws acm list-certificates --region us-west-2 --query "CertificateSummaryList[?DomainName=='*.longevo.com' && Status=='ISSUED']" --output json
```

Expected: un array con un elemento. Si está vacío, el certificado venció o cambió de ARN — no seguir, reportar `BLOCKED`.

- [ ] **Step 3: Verificar sintaxis**

```bash
cd /Users/manupandolfi/Longevo/longevoIac/src/open-wearables/environments/qa
terraform fmt && terraform validate
```

- [ ] **Step 4: Commit**

```bash
git add src/open-wearables/environments/qa/dns-alb-waf.tf
git commit -m "feat(open-wearables): add the alb, waf, and dns records"
```

---

### Task 7: Task definition de migración

Sin servicio — la ejecuta el pipeline de deploy con `RunTask` antes de actualizar los servicios. Comando: `scripts/start/init.sh`, la imagen del backend compartida.

**Files:**
- Create: `src/open-wearables/environments/qa/migration-task.tf`

**Interfaces:**
- Consumes: `aws_ecr_repository.backend` (Task 2), `aws_iam_role.execution` (Task 2), los secretos de la Task 4, `aws_rds_cluster.main`/`aws_elasticache_replication_group.main` (de un plan anterior).
- Produces: `aws_ecs_task_definition.migration.family` — lo consume el plan compañero de pipeline.

- [ ] **Step 1: Escribir el rol de task y el security group**

`src/open-wearables/environments/qa/migration-task.tf`:

```hcl
data "aws_iam_policy_document" "migration_task_extra" {
  statement {
    sid       = "ReadOwnSecrets"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.secret_key.arn,
      aws_secretsmanager_secret.master_key.arn,
      aws_secretsmanager_secret.admin_password.arn,
      aws_secretsmanager_secret.db_migrator_password.arn,
      aws_secretsmanager_secret.valkey_auth.arn,
    ]
  }
}

resource "aws_iam_role" "migration_task" {
  name               = "${local.name}-migration-task"
  assume_role_policy = module.tasks_assume_policy.json
}

resource "aws_iam_role_policy" "migration_task" {
  name   = "read-secrets"
  role   = aws_iam_role.migration_task.id
  policy = data.aws_iam_policy_document.migration_task_extra.json
}

resource "aws_security_group" "migration" {
  name        = "${local.name}-migration"
  description = "One-off migration task"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_security_group_rule" "aurora_from_migration" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.aurora.id
  source_security_group_id = aws_security_group.migration.id
}

resource "aws_security_group_rule" "valkey_from_migration" {
  type                     = "ingress"
  from_port                = 6379
  to_port                  = 6379
  protocol                 = "tcp"
  security_group_id        = aws_security_group.valkey.id
  source_security_group_id = aws_security_group.migration.id
}

resource "aws_cloudwatch_log_group" "migration" {
  name              = "/ecs/${local.name}-migration"
  retention_in_days = 14
  kms_key_id        = aws_kms_key.main.arn
}

resource "aws_ecs_task_definition" "migration" {
  family                   = "${local.name}-migration"
  cpu                      = 512
  memory                   = 1024
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.migration_task.arn

  container_definitions = jsonencode([{
    name      = "migration"
    image     = "public.ecr.aws/docker/library/busybox:latest"
    essential = true
    command   = ["bash", "scripts/start/init.sh"]
    environment = [
      { name = "ENVIRONMENT", value = "qa" },
      { name = "DB_HOST", value = aws_rds_cluster.main.endpoint },
      { name = "DB_PORT", value = "5432" },
      { name = "DB_NAME", value = "open_wearables" },
      { name = "DB_USER", value = "migrator" },
      { name = "REDIS_HOST", value = aws_elasticache_replication_group.main.primary_endpoint_address },
      { name = "REDIS_PORT", value = "6379" },
      { name = "REDIS_SSL", value = "true" },
    ]
    secrets = [
      { name = "SECRET_KEY", valueFrom = aws_secretsmanager_secret.secret_key.arn },
      { name = "MASTER_KEY", valueFrom = aws_secretsmanager_secret.master_key.arn },
      { name = "ADMIN_PASSWORD", valueFrom = aws_secretsmanager_secret.admin_password.arn },
      { name = "DB_PASSWORD", valueFrom = aws_secretsmanager_secret.db_migrator_password.arn },
      { name = "REDIS_PASSWORD", valueFrom = aws_secretsmanager_secret.valkey_auth.arn },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.migration.name
        "awslogs-region"        = module.global_constants.qa_aws_region
        "awslogs-stream-prefix" = "migration"
      }
    }
  }])

  lifecycle {
    ignore_changes = [container_definitions]
  }
}
```

- [ ] **Step 2: Verificar**

```bash
cd /Users/manupandolfi/Longevo/longevoIac/src/open-wearables/environments/qa
terraform fmt && terraform validate
```

- [ ] **Step 3: Commit**

```bash
git add src/open-wearables/environments/qa/migration-task.tf
git commit -m "feat(open-wearables): add the migration task definition"
```

---

### Task 8: Servicio api

Target group, regla de listener, y el servicio en sí vía el módulo de la Task 1.

**Files:**
- Create: `src/open-wearables/environments/qa/service-api.tf`

**Interfaces:**
- Consumes: `module.service` (Task 1), `aws_lb.main`/`aws_lb_listener.https`/`aws_security_group.alb` (Task 6), `aws_ecr_repository.backend` (Task 2), los secretos (Task 4).
- Produces: `module.api.security_group_id` — lo consume la regla de ingress de Aurora/Valkey en este mismo task.

- [ ] **Step 1: Escribir el archivo**

`src/open-wearables/environments/qa/service-api.tf`:

```hcl
resource "aws_iam_role" "api_task" {
  name               = "${local.name}-api-task"
  assume_role_policy = module.tasks_assume_policy.json
}

resource "aws_lb_target_group" "api" {
  name        = "${local.name}-api"
  port        = 8000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    path                = "/"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener_rule" "api" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 100

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }

  condition {
    host_header {
      values = ["wearables-api-qa.longevo.com"]
    }
  }
}

module "api" {
  source = "../../modules/service"

  name        = "api"
  environment = module.global_constants.qa_environment
  project     = module.constants.project
  cluster_id  = aws_ecs_cluster.main.name
  vpc_id      = aws_vpc.main.id
  subnet_ids  = [for s in aws_subnet.private : s.id]
  kms_key_arn = aws_kms_key.main.arn

  execution_role_arn = aws_iam_role.execution.arn
  task_role_arn       = aws_iam_role.api_task.arn

  cpu    = 256
  memory = 512

  container_port             = 8000
  ingress_security_group_ids = [aws_security_group.alb.id]
  target_group_arn           = aws_lb_target_group.api.arn

  environment_variables = {
    ENVIRONMENT = "qa"
    DB_HOST     = aws_rds_cluster.main.endpoint
    DB_PORT     = "5432"
    DB_NAME     = "open_wearables"
    DB_USER     = "app"
    REDIS_HOST  = aws_elasticache_replication_group.main.primary_endpoint_address
    REDIS_PORT  = "6379"
    REDIS_SSL   = "true"
  }

  secrets = {
    SECRET_KEY     = aws_secretsmanager_secret.secret_key.arn
    MASTER_KEY     = aws_secretsmanager_secret.master_key.arn
    DB_PASSWORD    = aws_secretsmanager_secret.db_app_password.arn
    REDIS_PASSWORD = aws_secretsmanager_secret.valkey_auth.arn
  }

  desired_count = 1

  autoscaling = {
    min_capacity       = 1
    max_capacity       = 2
    cpu_target_percent = 70
  }
}

resource "aws_security_group_rule" "aurora_from_api" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.aurora.id
  source_security_group_id = module.api.security_group_id
}

resource "aws_security_group_rule" "valkey_from_api" {
  type                     = "ingress"
  from_port                = 6379
  to_port                  = 6379
  protocol                 = "tcp"
  security_group_id        = aws_security_group.valkey.id
  source_security_group_id = module.api.security_group_id
}
```

- [ ] **Step 2: Verificar**

```bash
cd /Users/manupandolfi/Longevo/longevoIac/src/open-wearables/environments/qa
terraform fmt && terraform validate
```

- [ ] **Step 3: Commit**

```bash
git add src/open-wearables/environments/qa/service-api.tf
git commit -m "feat(open-wearables): add the api service"
```

---

### Task 9: Servicio frontend

Igual forma que la Task 8, con su propia imagen (sin acceso a Aurora ni Valkey — el frontend habla con la API, no con la base).

**Files:**
- Create: `src/open-wearables/environments/qa/service-frontend.tf`

**Interfaces:**
- Consumes: lo mismo que la Task 8, más `aws_ecr_repository.frontend` (Task 2).
- Produces: nada que otra task consuma.

- [ ] **Step 1: Escribir el archivo**

`src/open-wearables/environments/qa/service-frontend.tf`:

```hcl
resource "aws_iam_role" "frontend_task" {
  name               = "${local.name}-frontend-task"
  assume_role_policy = module.tasks_assume_policy.json
}

resource "aws_lb_target_group" "frontend" {
  name        = "${local.name}-frontend"
  port        = 3000
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"

  health_check {
    path                = "/"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener_rule" "frontend" {
  listener_arn = aws_lb_listener.https.arn
  priority     = 200

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.frontend.arn
  }

  condition {
    host_header {
      values = ["wearables-app-qa.longevo.com"]
    }
  }
}

module "frontend" {
  source = "../../modules/service"

  name        = "frontend"
  environment = module.global_constants.qa_environment
  project     = module.constants.project
  cluster_id  = aws_ecs_cluster.main.name
  vpc_id      = aws_vpc.main.id
  subnet_ids  = [for s in aws_subnet.private : s.id]
  kms_key_arn = aws_kms_key.main.arn

  execution_role_arn = aws_iam_role.execution.arn
  task_role_arn       = aws_iam_role.frontend_task.arn

  cpu    = 256
  memory = 512

  container_port             = 3000
  ingress_security_group_ids = [aws_security_group.alb.id]
  target_group_arn           = aws_lb_target_group.frontend.arn

  environment_variables = {
    VITE_API_URL = "https://wearables-api-qa.longevo.com"
  }

  desired_count = 1

  autoscaling = {
    min_capacity       = 1
    max_capacity       = 2
    cpu_target_percent = 70
  }
}
```

- [ ] **Step 2: Verificar**

```bash
cd /Users/manupandolfi/Longevo/longevoIac/src/open-wearables/environments/qa
terraform fmt && terraform validate
```

- [ ] **Step 3: Commit**

```bash
git add src/open-wearables/environments/qa/service-frontend.tf
git commit -m "feat(open-wearables): add the frontend service"
```

---

### Task 10: Servicios celery-worker y celery-worker-bulk

Sin ALB — no reciben tráfico entrante. Se diferencian por `CELERY_QUEUES`.

**Files:**
- Create: `src/open-wearables/environments/qa/service-worker.tf`
- Create: `src/open-wearables/environments/qa/service-worker-bulk.tf`

**Interfaces:**
- Consumes: lo mismo que la Task 8 salvo el ALB.
- Produces: nada que otra task consuma.

- [ ] **Step 1: Escribir `service-worker.tf`**

`src/open-wearables/environments/qa/service-worker.tf`:

```hcl
resource "aws_iam_role" "worker_task" {
  name               = "${local.name}-worker-task"
  assume_role_policy = module.tasks_assume_policy.json
}

module "worker" {
  source = "../../modules/service"

  name        = "celery-worker"
  environment = module.global_constants.qa_environment
  project     = module.constants.project
  cluster_id  = aws_ecs_cluster.main.name
  vpc_id      = aws_vpc.main.id
  subnet_ids  = [for s in aws_subnet.private : s.id]
  kms_key_arn = aws_kms_key.main.arn

  execution_role_arn = aws_iam_role.execution.arn
  task_role_arn       = aws_iam_role.worker_task.arn

  cpu    = 512
  memory = 1024

  command = ["bash", "scripts/start/worker.sh"]

  environment_variables = {
    ENVIRONMENT   = "qa"
    DB_HOST       = aws_rds_cluster.main.endpoint
    DB_PORT       = "5432"
    DB_NAME       = "open_wearables"
    DB_USER       = "app"
    REDIS_HOST    = aws_elasticache_replication_group.main.primary_endpoint_address
    REDIS_PORT    = "6379"
    REDIS_SSL     = "true"
    # sdk_sync goes to celery-worker-bulk, not here — a single large batch
    # would otherwise starve default/webhook_sync/garmin_sync on this worker.
    CELERY_QUEUES = "default,garmin_sync,webhook_sync"
  }

  secrets = {
    SECRET_KEY     = aws_secretsmanager_secret.secret_key.arn
    MASTER_KEY     = aws_secretsmanager_secret.master_key.arn
    DB_PASSWORD    = aws_secretsmanager_secret.db_app_password.arn
    REDIS_PASSWORD = aws_secretsmanager_secret.valkey_auth.arn
  }

  desired_count = 1

  autoscaling = {
    min_capacity       = 1
    max_capacity       = 2
    cpu_target_percent = 70
  }
}

resource "aws_security_group_rule" "aurora_from_worker" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.aurora.id
  source_security_group_id = module.worker.security_group_id
}

resource "aws_security_group_rule" "valkey_from_worker" {
  type                     = "ingress"
  from_port                = 6379
  to_port                  = 6379
  protocol                 = "tcp"
  security_group_id        = aws_security_group.valkey.id
  source_security_group_id = module.worker.security_group_id
}
```

- [ ] **Step 2: Escribir `service-worker-bulk.tf`**

`src/open-wearables/environments/qa/service-worker-bulk.tf`:

```hcl
resource "aws_iam_role" "worker_bulk_task" {
  name               = "${local.name}-worker-bulk-task"
  assume_role_policy = module.tasks_assume_policy.json
}

module "worker_bulk" {
  source = "../../modules/service"

  name        = "celery-worker-bulk"
  environment = module.global_constants.qa_environment
  project     = module.constants.project
  cluster_id  = aws_ecs_cluster.main.name
  vpc_id      = aws_vpc.main.id
  subnet_ids  = [for s in aws_subnet.private : s.id]
  kms_key_arn = aws_kms_key.main.arn

  execution_role_arn = aws_iam_role.execution.arn
  task_role_arn       = aws_iam_role.worker_bulk_task.arn

  # Sized for the backfill: process_s3_sdk_upload reads the whole object into
  # memory. The presign ceiling is 50 MB and the measured multiplier is ~10x,
  # so this must comfortably clear ~500 MB per concurrent task.
  cpu    = 1024
  memory = 2048

  command = ["bash", "scripts/start/worker.sh"]

  environment_variables = {
    ENVIRONMENT   = "qa"
    DB_HOST       = aws_rds_cluster.main.endpoint
    DB_PORT       = "5432"
    DB_NAME       = "open_wearables"
    DB_USER       = "app"
    REDIS_HOST    = aws_elasticache_replication_group.main.primary_endpoint_address
    REDIS_PORT    = "6379"
    REDIS_SSL     = "true"
    CELERY_QUEUES = "sdk_sync"
  }

  secrets = {
    SECRET_KEY     = aws_secretsmanager_secret.secret_key.arn
    MASTER_KEY     = aws_secretsmanager_secret.master_key.arn
    DB_PASSWORD    = aws_secretsmanager_secret.db_app_password.arn
    REDIS_PASSWORD = aws_secretsmanager_secret.valkey_auth.arn
  }

  desired_count = 1

  autoscaling = {
    min_capacity       = 1
    max_capacity       = 3
    cpu_target_percent = 70
  }
}

resource "aws_security_group_rule" "aurora_from_worker_bulk" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.aurora.id
  source_security_group_id = module.worker_bulk.security_group_id
}

resource "aws_security_group_rule" "valkey_from_worker_bulk" {
  type                     = "ingress"
  from_port                = 6379
  to_port                  = 6379
  protocol                 = "tcp"
  security_group_id        = aws_security_group.valkey.id
  source_security_group_id = module.worker_bulk.security_group_id
}
```

Nota deliberada: `desired_count = 1` para `celery-worker-bulk` en fase 1. El spec (sección 6, sección 8) documenta que `--concurrency=1` en este worker es un requisito antes de la primera cohorte de backfill, para no reventar el techo de memoria — eso se fija en `scripts/start/worker.sh` o en el `command` de esta task definition, y queda **fuera de este plan**: es parte del ítem "gate obligatorio antes del primer lote" del spec, no de la plataforma base.

- [ ] **Step 3: Verificar**

```bash
cd /Users/manupandolfi/Longevo/longevoIac/src/open-wearables/environments/qa
terraform fmt && terraform validate
```

- [ ] **Step 4: Commit**

```bash
git add src/open-wearables/environments/qa/service-worker.tf src/open-wearables/environments/qa/service-worker-bulk.tf
git commit -m "feat(open-wearables): add the celery-worker and celery-worker-bulk services"
```

---

### Task 11: Servicio celery-beat

Sin autoscaling — un scheduler duplicado dispara tareas programadas dos veces. `min 0 / max 100` en el deploy para que nunca convivan dos beats.

**Files:**
- Create: `src/open-wearables/environments/qa/service-beat.tf`

**Interfaces:**
- Consumes: lo mismo que la Task 10.
- Produces: nada que otra task consuma.

- [ ] **Step 1: Escribir el archivo**

`src/open-wearables/environments/qa/service-beat.tf`:

```hcl
resource "aws_iam_role" "beat_task" {
  name               = "${local.name}-beat-task"
  assume_role_policy = module.tasks_assume_policy.json
}

module "beat" {
  source = "../../modules/service"

  name        = "celery-beat"
  environment = module.global_constants.qa_environment
  project     = module.constants.project
  cluster_id  = aws_ecs_cluster.main.name
  vpc_id      = aws_vpc.main.id
  subnet_ids  = [for s in aws_subnet.private : s.id]
  kms_key_arn = aws_kms_key.main.arn

  execution_role_arn = aws_iam_role.execution.arn
  task_role_arn       = aws_iam_role.beat_task.arn

  cpu    = 256
  memory = 512

  command = ["bash", "scripts/start/beat.sh"]

  environment_variables = {
    ENVIRONMENT = "qa"
    DB_HOST     = aws_rds_cluster.main.endpoint
    DB_PORT     = "5432"
    DB_NAME     = "open_wearables"
    DB_USER     = "app"
    REDIS_HOST  = aws_elasticache_replication_group.main.primary_endpoint_address
    REDIS_PORT  = "6379"
    REDIS_SSL   = "true"
  }

  secrets = {
    SECRET_KEY     = aws_secretsmanager_secret.secret_key.arn
    MASTER_KEY     = aws_secretsmanager_secret.master_key.arn
    DB_PASSWORD    = aws_secretsmanager_secret.db_app_password.arn
    REDIS_PASSWORD = aws_secretsmanager_secret.valkey_auth.arn
  }

  desired_count = 1

  # No autoscaling — exactly one scheduler, always. Deploys use min 0 / max 100
  # so the old task is fully stopped before the new one starts; two live beats
  # would double-fire every scheduled task.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  autoscaling                        = null
}

resource "aws_security_group_rule" "aurora_from_beat" {
  type                     = "ingress"
  from_port                = 5432
  to_port                  = 5432
  protocol                 = "tcp"
  security_group_id        = aws_security_group.aurora.id
  source_security_group_id = module.beat.security_group_id
}

resource "aws_security_group_rule" "valkey_from_beat" {
  type                     = "ingress"
  from_port                = 6379
  to_port                  = 6379
  protocol                 = "tcp"
  security_group_id        = aws_security_group.valkey.id
  source_security_group_id = module.beat.security_group_id
}
```

- [ ] **Step 2: Verificar**

```bash
cd /Users/manupandolfi/Longevo/longevoIac/src/open-wearables/environments/qa
terraform fmt && terraform validate
```

- [ ] **Step 3: Commit**

```bash
git add src/open-wearables/environments/qa/service-beat.tf
git commit -m "feat(open-wearables): add the celery-beat service, without autoscaling"
```

---

### Task 12: Rol OIDC de deploy para `longevo-wearables`

El patrón exacto ya existe en `src/agent-lab/environments/qa/iam_deploy.tf` (rol `telar_deploy`) — se adapta, no se reinventa.

**Files:**
- Create: `src/open-wearables/environments/qa/deploy-role.tf`

**Interfaces:**
- Consumes: `aws_ecr_repository.backend`/`.frontend` (Task 2), `aws_iam_role.execution` (Task 2), los seis roles de task de las Tasks 7 a 11, `module.api.service_name` etc. (Tasks 8-11), `aws_ecs_cluster.main` (Task 3).
- Produces: `aws_iam_role.deploy.arn` — lo consume el plan compañero (`2026-09-01-deploy-pipeline-qa.md`), como secreto/variable en el entorno de GitHub `qa-open-wearables`.

- [ ] **Step 1: Escribir el archivo**

`src/open-wearables/environments/qa/deploy-role.tf`:

```hcl
# We do not reuse src/terraform/modules/github-oidc-trust/base: that module
# trusts only LongevoHealth/longevoIac. This role must trust the app repo.
#
# The GitHub OIDC provider already exists in the account. The ARN is built
# deterministically instead of via aws_iam_openid_connect_provider (that data
# source needs iam:ListOpenIDConnectProviders, which this account's CI roles
# may lack). A wrong ARN fails AssumeRoleWithWebIdentity at runtime, fail-safe.
locals {
  github_oidc_provider_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:oidc-provider/token.actions.githubusercontent.com"

  # From `gh api /repos/LongevoHealth/longevo-wearables/actions/oidc/customization/sub`.
  # The repo is on the name-based subject today (use_immutable_subject: false);
  # the immutable form is listed too so trust survives an opt-in later.
  longevo_wearables_org_id  = "105651350"
  longevo_wearables_repo_id = "1342063149"

  deploy_service_names = [
    module.api.service_name,
    module.frontend.service_name,
    module.worker.service_name,
    module.worker_bulk.service_name,
    module.beat.service_name,
  ]

  deploy_task_role_arns = [
    aws_iam_role.api_task.arn,
    aws_iam_role.frontend_task.arn,
    aws_iam_role.worker_task.arn,
    aws_iam_role.worker_bulk_task.arn,
    aws_iam_role.beat_task.arn,
    aws_iam_role.migration_task.arn,
  ]
}

data "aws_iam_policy_document" "deploy_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:LongevoHealth/longevo-wearables:ref:refs/heads/main",
        "repo:LongevoHealth@${local.longevo_wearables_org_id}/longevo-wearables@${local.longevo_wearables_repo_id}:ref:refs/heads/main",
      ]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name               = "${local.name}-deploy"
  description        = "GitHub Actions OIDC role to build/push open-wearables and deploy to ECS qa"
  assume_role_policy = data.aws_iam_policy_document.deploy_trust.json

  tags = {
    Name = "${local.name}-deploy"
  }
}

data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "EcrAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPush"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchGetImage",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      "ecr:DescribeRepositories",
      "ecr:DescribeImages",
    ]
    resources = [aws_ecr_repository.backend.arn, aws_ecr_repository.frontend.arn]
  }

  statement {
    sid    = "EcsDescribe"
    effect = "Allow"
    actions = [
      "ecs:DescribeClusters",
      "ecs:DescribeServices",
      "ecs:DescribeTaskDefinition",
      "ecs:DescribeTasks",
      "ecs:ListTasks",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "EcsRegisterTaskDefinition"
    effect    = "Allow"
    actions   = ["ecs:RegisterTaskDefinition"]
    resources = ["*"]
  }

  statement {
    sid    = "EcsUpdateService"
    effect = "Allow"
    actions = ["ecs:UpdateService"]
    resources = [
      for name in local.deploy_service_names :
      "arn:aws:ecs:${module.global_constants.qa_aws_region}:${data.aws_caller_identity.current.account_id}:service/${aws_ecs_cluster.main.name}/${name}"
    ]
  }

  # The migration task has no service — RunTask instead of UpdateService.
  statement {
    sid    = "EcsRunMigrationTask"
    effect = "Allow"
    actions = ["ecs:RunTask"]
    resources = [aws_ecs_task_definition.migration.arn]

    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.main.arn]
    }
  }

  statement {
    sid       = "EcsWaitMigrationTask"
    effect    = "Allow"
    actions   = ["ecs:DescribeTasks"]
    resources = ["*"]
  }

  statement {
    sid     = "PassTaskRoles"
    effect  = "Allow"
    actions = ["iam:PassRole"]
    resources = concat(
      [aws_iam_role.execution.arn],
      local.deploy_task_role_arns,
    )

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "deploy-policy"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}
```

Nota: el rol de bootstrap (`db_bootstrap_task`) **no** entra en `deploy_task_role_arns` — el pipeline de deploy nunca lo vuelve a tocar después del `apply` inicial de Terraform, así que no necesita `PassRole` sobre él.

- [ ] **Step 2: Verificar**

```bash
cd /Users/manupandolfi/Longevo/longevoIac/src/open-wearables/environments/qa
terraform fmt && terraform validate
```

- [ ] **Step 3: Commit**

```bash
git add src/open-wearables/environments/qa/deploy-role.tf
git commit -m "feat(open-wearables): add the github actions deploy role"
```

---

### Task 13: Extender los outputs

Los que el plan compañero de pipeline necesita para escribir el workflow.

**Files:**
- Modify: `src/open-wearables/environments/qa/outputs.tf` (ya existe, agregar al final)

**Interfaces:**
- Consumes: todo lo de las Tasks 1 a 12.
- Produces: los outputs que lee `2026-09-01-deploy-pipeline-qa.md`.

- [ ] **Step 1: Agregar los outputs**

Al final de `src/open-wearables/environments/qa/outputs.tf`:

```hcl
output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "deploy_role_arn" {
  value = aws_iam_role.deploy.arn
}

output "backend_ecr_repository_url" {
  value = aws_ecr_repository.backend.repository_url
}

output "frontend_ecr_repository_url" {
  value = aws_ecr_repository.frontend.repository_url
}

output "migration_task_definition_family" {
  value = aws_ecs_task_definition.migration.family
}

output "api_service_name" {
  value = module.api.service_name
}

output "frontend_service_name" {
  value = module.frontend.service_name
}

output "worker_service_name" {
  value = module.worker.service_name
}

output "worker_bulk_service_name" {
  value = module.worker_bulk.service_name
}

output "beat_service_name" {
  value = module.beat.service_name
}

output "alb_dns_name" {
  value = aws_lb.main.dns_name
}

output "api_url" {
  value = "https://wearables-api-qa.longevo.com"
}

output "app_url" {
  value = "https://wearables-app-qa.longevo.com"
}
```

- [ ] **Step 2: Verificar el conjunto completo**

```bash
cd /Users/manupandolfi/Longevo/longevoIac/src/open-wearables/environments/qa
terraform fmt -check -recursive /Users/manupandolfi/Longevo/longevoIac/src/open-wearables
terraform validate
```

Expected: sin diferencias de formato, configuración válida.

- [ ] **Step 3: `terraform plan` completo de sólo lectura**

```bash
export PATH="/opt/homebrew/bin:$PATH"
AWS_PROFILE=manu-longevo-app-mfa terraform plan -input=false -no-color 2>&1 | tail -60
```

Leer el resumen final (`Plan: N to add, M to change, 0 to destroy`). Confirmar **0 destroy** — cualquier destroy en este plan sobre Aurora, Valkey o la VPC es motivo de parar y reportar antes de seguir. Contar que las altas incluyan: 1 cluster, 2 ECR, ~9 roles IAM, 1 ALB + 1 WAF + 2 registros DNS, 6 task definitions, 5 servicios, 5 grupos de autoscaling (4 con política + 1 sin), ~14 reglas de security group. No hace falta que el número exacto coincida — sí que la forma general lo haga.

- [ ] **Step 4: Commit**

```bash
git add src/open-wearables/environments/qa/outputs.tf
git commit -m "feat(open-wearables): expose platform outputs for the deploy pipeline"
```

---

## Antes de abrir el PR

1. **Este PR, al mergear, dispara el bootstrap real de roles de Postgres contra la Aurora de qa** (Task 5) además de crear cluster/ALB/servicios/etc. Es la primera vez que este plan toca datos, no sólo red — leer el plan del job de CI con más cuidado que de costumbre.
2. **`terraform apply` en CI corre el `null_resource.db_bootstrap_run`**, que a su vez corre `aws ecs run-task` y espera a que termine (`ecs wait tasks-stopped`, sin timeout explícito — por default son 100 intentos cada 6s, ~10 minutos). Si el job de apply tiene un timeout más corto que eso, el bootstrap puede cortarse a mitad de camino — confirmar el timeout del job `terraform-apply.yml` antes de mergear.
3. **Las cinco task definitions de servicios se registran con imagen placeholder** (`busybox`) — los servicios van a estar en estado `RUNNING` pero fallando el health check hasta que el plan compañero de pipeline registre la imagen real y actualice los servicios. Es esperado, no es una señal de que algo salió mal.

## Fuera del alcance de este plan

| Qué | Dónde va |
|---|---|
| El workflow de GitHub Actions que construye las imágenes y dispara el rollout | `docs/superpowers/plans/2026-09-01-deploy-pipeline-qa.md`, en `longevo-wearables` |
| Bucket S3, SNS, DLQ para la ingesta de SDK | Plan 4 (`sdk-ingest` + observabilidad) |
| Alarmas de CloudWatch, canary de Synthetics | Plan 4 |
| `--concurrency=1` en `celery-worker-bulk` | Ítem obligatorio antes de la primera cohorte de migración, ver spec sección 6 y 8 — no es parte de la plataforma base |
| Guard de dedupe para reprocesamiento de sleep | Código en `longevo-wearables`, bloquea la primera cohorte, sin plan escrito todavía |
| **Rotación automática de `db_app_password`** con `force-new-deployment` de los servicios | El spec (sección 4) la pide explícitamente y este plan **no** la implementa — genera el secreto una vez, estático. Agregar `aws_secretsmanager_secret_rotation` con una Lambda de rotación, más el trigger de `force-new-deployment`, es una pieza propia (Lambda + EventBridge + permisos), no un afterthought de este plan ya grande. Gap real, no un olvido silencioso — anotado para un plan chico aparte antes de considerar qa "productionizado". `MASTER_KEY` sigue sin rotación a propósito, según el spec. |
| Replicar todo esto en prod | Plan 5, con 3 AZs y los sizings de prod |
