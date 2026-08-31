# Fundaciones de infra en qa: red y datos — Plan de implementación

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Crear en `qa` la VPC dedicada de longevo-wearables y sus dos almacenes de estado — Aurora PostgreSQL Serverless v2 y ElastiCache Valkey — con Terraform, listos para que el stack de servicios ECS se apoye encima.

**Architecture:** Un proyecto nuevo `src/longevo/open-wearables` en el repositorio `longevoIac`, siguiendo la convención de proyectos con `environments/`. Todo se escribe como recursos propios, sin módulos del registry, porque es el estilo del repo. La VPC tiene tres capas de subnets y **ninguna ruta a internet desde las capas privadas**: la salida a AWS se resuelve con VPC endpoints.

**Tech Stack:** Terraform >= 1.14.0, provider `hashicorp/aws ~> 5.0`, `hashicorp/random ~> 3.0`. Aurora PostgreSQL 17.10, ElastiCache Valkey 9.1.

**Spec:** `docs/superpowers/specs/2026-08-22-aws-infra-design.md` (secciones 4 y 6). Ojo: el spec vive en el repo `longevo-wearables`; el código de este plan se escribe en `longevoIac`.

## Global Constraints

- **El trabajo ocurre en `/Users/manupandolfi/Longevo/longevoIac`**, no en el repo del fork. Rama base: `master`. Crear una rama de feature antes del primer commit; nunca commitear en `master`.
- **Sin módulos del registry.** Ningún proyecto de longevoIac usa uno; sólo providers de hashicorp. Todo se escribe como recursos propios.
- **Ningún proyecto del repo creó una VPC hasta ahora** — todos usan `data "aws_vpc" "default"`. Esta es la primera, así que no hay patrón interno que copiar para la red; sí lo hay para el resto (ver `src/modules/documentdb` como ejemplo de almacén con subnet group, SG y variables).
- Región de `qa`: `us-west-2`, vía `module.global_constants.qa_aws_region`. Nunca hardcodear.
- Nombre del proyecto: **`longevo-open-wearables`**. Es el valor del tag `Project` y el prefijo de todo recurso nombrado, con el formato `${environment}-${project}-<nombre>`.
- Los tags globales se aplican por `default_tags` en el provider. No plumbear tags a mano salvo un `Name`.
- CIDR de la VPC de qa: **`10.60.0.0/16`**. Confirmado libre contra el inventario del spec (prod y qa de Longevo usan `172.31.0.0/16`, staging `10.10.0.0/16`, pools de VPN `10.100/10.150/10.200.0.0/22`).
- Estado en S3: bucket `longevo-terraform-state`, key `longevo/open-wearables/qa/terraform.tfstate`, región `us-east-1`, tabla de lock `terraform-state-lock`, `encrypt = true`.
- **El CI aplica automáticamente al mergear a `master`, sin approval.** Un PR mergeado crea infraestructura de verdad. Revisar el plan del job antes de mergear.
- El rol de apply del CI tiene `ec2:*`, `rds:*`, `elasticache:*`, `kms:*`, `secretsmanager:*`, `logs:*`, `s3:*` regionales, más `iam:*` global. Nada de lo que este plan crea queda fuera.
- Verificación local en cada task: `terraform fmt -recursive`, `terraform init -backend=false`, `terraform validate`. El `terraform plan` autoritativo lo corre el CI en el PR — localmente el backend de S3 puede no ser accesible.
- Commits con conventional commits, igual que el historial del repo.

## Estructura de archivos

Todo bajo `/Users/manupandolfi/Longevo/longevoIac`:

| Archivo | Responsabilidad | Task |
|---|---|---|
| `src/longevo/open-wearables/modules/constants/main.tf` | Nombre canónico del proyecto (nuevo) | 1 |
| `src/longevo/open-wearables/environments/qa/main.tf` | Backend, provider, constants (nuevo) | 1 |
| `src/longevo/open-wearables/README.md` | Qué es el proyecto y cómo se relaciona con `src/longevo/wearables` (nuevo) | 1 |
| `src/longevo/open-wearables/environments/qa/kms.tf` | CMK del ambiente y su alias (nuevo) | 2 |
| `src/longevo/open-wearables/environments/qa/network.tf` | VPC, subnets, IGW, route tables (nuevo) | 3 |
| `src/longevo/open-wearables/environments/qa/endpoints.tf` | VPC endpoints y su security group (nuevo) | 4 |
| `src/longevo/open-wearables/environments/qa/flow-logs.tf` | Bucket y flow logs de la VPC (nuevo) | 5 |
| `src/longevo/open-wearables/environments/qa/aurora.tf` | Cluster Aurora, parameter group, subnet group, SG (nuevo) | 6 |
| `src/longevo/open-wearables/environments/qa/valkey.tf` | ElastiCache Valkey, auth token, SG (nuevo) | 7 |
| `src/longevo/open-wearables/environments/qa/outputs.tf` | Outputs que consumirá el stack de servicios | 7 |

---

### Task 1: Andamiaje del proyecto

**Files:**
- Create: `src/longevo/open-wearables/modules/constants/main.tf`
- Create: `src/longevo/open-wearables/environments/qa/main.tf`
- Create: `src/longevo/open-wearables/README.md`

**Interfaces:**
- Consumes: `src/modules/constants` (outputs `qa_aws_region`, `qa_environment`, `tags`).
- Produces: `module.constants.project` = `"longevo-open-wearables"`, y `module.global_constants` — los consumen todas las tasks siguientes.

- [ ] **Step 1: Crear la rama**

```bash
cd /Users/manupandolfi/Longevo/longevoIac && git switch master && git pull && git switch -c feat/open-wearables-qa-foundations
```

- [ ] **Step 2: Crear el módulo de constantes del proyecto**

`src/longevo/open-wearables/modules/constants/main.tf`:

```hcl
locals {
  project = "longevo-open-wearables"
}

output "project" {
  value       = local.project
  description = "Project identifier. Used as the Project tag value and as the prefix for named AWS resources in this stack."
}
```

- [ ] **Step 3: Crear el root del ambiente qa**

`src/longevo/open-wearables/environments/qa/main.tf`:

```hcl
terraform {
  required_version = ">= 1.14.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }

  backend "s3" {
    bucket         = "longevo-terraform-state"
    key            = "longevo/open-wearables/qa/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "terraform-state-lock"
    encrypt        = true
  }
}

provider "aws" {
  region = module.global_constants.qa_aws_region

  default_tags {
    tags = merge(module.global_constants.tags, {
      Environment = module.global_constants.qa_environment
      Project     = module.constants.project
    })
  }
}

module "global_constants" {
  source = "../../../../modules/constants"
}

module "constants" {
  source = "../../modules/constants"
}

data "aws_caller_identity" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  name = "${module.global_constants.qa_environment}-${module.constants.project}"

  # Two AZs in qa; prod will use three. Sorted so a new AZ appearing in the
  # account cannot silently renumber the subnets and force replacements.
  azs = slice(sort(data.aws_availability_zones.available.names), 0, 2)
}
```

- [ ] **Step 4: Escribir el README del proyecto**

`src/longevo/open-wearables/README.md`:

```markdown
# longevo-open-wearables

Infrastructure for the Open Wearables fork that replaces Spike as Longevo's source
of wearable health data. The application lives in the `longevo-wearables`
repository; this project owns the AWS resources it runs on.

Not to be confused with `src/longevo/wearables`, which owns the DocumentDB cluster
and the raw-ingest bucket for the proprietary wearable. The two are unrelated
stacks with separate state.

## Why this project has its own VPC

Every other project in this repository attaches to the account's default VPC. This
one does not: it holds health data and is meant to be reachable only through its
own load balancer, so it gets a dedicated VPC with no route to the rest of the
account. The design and the reasoning behind each choice live in the application
repository, in `docs/superpowers/specs/2026-08-22-aws-infra-design.md`.

## Environments

| Env | Region | VPC CIDR |
| --- | --- | --- |
| qa | us-west-2 | 10.60.0.0/16 |
| prod | us-east-1 | 10.61.0.0/16 (not built yet) |
```

- [ ] **Step 5: Verificar**

```bash
cd /Users/manupandolfi/Longevo/longevoIac/src/longevo/open-wearables/environments/qa && terraform fmt -recursive ../.. && terraform init -backend=false && terraform validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 6: Commit**

```bash
cd /Users/manupandolfi/Longevo/longevoIac
git add src/longevo/open-wearables
git commit -m "feat(open-wearables): scaffold the qa terraform project"
```

---

### Task 2: CMK del ambiente

Aurora, ElastiCache, los logs y los buckets se cifran con una sola clave por ambiente, según la sección 4 del spec.

**Files:**
- Create: `src/longevo/open-wearables/environments/qa/kms.tf`

**Interfaces:**
- Consumes: `local.name`, `data.aws_caller_identity.current` de Task 1.
- Produces: `aws_kms_key.main` — lo consumen Tasks 5, 6 y 7 vía `aws_kms_key.main.arn`.

- [ ] **Step 1: Escribir el archivo**

`src/longevo/open-wearables/environments/qa/kms.tf`:

```hcl
# One customer-managed key per environment, used by Aurora, ElastiCache, the flow
# log bucket and the log groups. A single key keeps the grant surface small; the
# blast radius of losing it is the whole environment, which is why it has both
# rotation and a deletion window long enough to notice a mistake.
resource "aws_kms_key" "main" {
  description             = "Encryption key for ${local.name}"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  tags = {
    Name = local.name
  }
}

resource "aws_kms_alias" "main" {
  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.main.key_id
}
```

- [ ] **Step 2: Verificar**

```bash
cd /Users/manupandolfi/Longevo/longevoIac/src/longevo/open-wearables/environments/qa && terraform fmt && terraform validate
```

Expected: válido.

- [ ] **Step 3: Commit**

```bash
git add src/longevo/open-wearables/environments/qa/kms.tf
git commit -m "feat(open-wearables): add the qa customer-managed key"
```

---

### Task 3: VPC, subnets y ruteo

Tres capas: pública sólo para el ALB, privada para las tasks de ECS, aislada para los almacenes. Las dos últimas **no tienen ruta por defecto**: sin NAT y sin IGW, no hay salida a internet.

**Files:**
- Create: `src/longevo/open-wearables/environments/qa/network.tf`

**Interfaces:**
- Consumes: `local.name`, `local.azs` de Task 1.
- Produces: `aws_vpc.main`, `aws_subnet.public`, `aws_subnet.private`, `aws_subnet.isolated`, `aws_route_table.private` — los consumen Tasks 4 a 7.

- [ ] **Step 1: Escribir el archivo**

`src/longevo/open-wearables/environments/qa/network.tf`:

```hcl
resource "aws_vpc" "main" {
  cidr_block           = "10.60.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = local.name
  }
}

# Public: the load balancer only. /24 is deliberate — nothing else belongs here,
# and a small block makes that obvious to whoever reads the console next.
resource "aws_subnet" "public" {
  for_each = { for idx, az in local.azs : az => idx }

  vpc_id            = aws_vpc.main.id
  availability_zone = each.key
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, each.value)

  tags = {
    Name = "${local.name}-public-${each.key}"
    Tier = "public"
  }
}

# Private: ECS tasks. /20 because task ENIs are the thing that scales here.
resource "aws_subnet" "private" {
  for_each = { for idx, az in local.azs : az => idx }

  vpc_id            = aws_vpc.main.id
  availability_zone = each.key
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 4, each.value + 1)

  tags = {
    Name = "${local.name}-private-${each.key}"
    Tier = "private"
  }
}

# Isolated: Aurora and ElastiCache. No route table entry beyond local, so these
# subnets cannot reach anything outside the VPC even by misconfiguration.
resource "aws_subnet" "isolated" {
  for_each = { for idx, az in local.azs : az => idx }

  vpc_id            = aws_vpc.main.id
  availability_zone = each.key
  cidr_block        = cidrsubnet(aws_vpc.main.cidr_block, 8, each.value + 48)

  tags = {
    Name = "${local.name}-isolated-${each.key}"
    Tier = "isolated"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = local.name
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "${local.name}-public"
  }
}

resource "aws_route_table_association" "public" {
  for_each = aws_subnet.public

  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

# One private route table per AZ. There is no default route today because there
# is no NAT: outbound AWS traffic goes through the VPC endpoints. Keeping the
# tables per-AZ means adding a NAT later (when OAuth providers arrive) is a
# per-AZ edit, not a re-architecture.
resource "aws_route_table" "private" {
  for_each = aws_subnet.private

  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${local.name}-private-${each.key}"
  }
}

resource "aws_route_table_association" "private" {
  for_each = aws_subnet.private

  subnet_id      = each.value.id
  route_table_id = aws_route_table.private[each.key].id
}

resource "aws_route_table" "isolated" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${local.name}-isolated"
  }
}

resource "aws_route_table_association" "isolated" {
  for_each = aws_subnet.isolated

  subnet_id      = each.value.id
  route_table_id = aws_route_table.isolated.id
}

# The default security group of a new VPC allows all traffic between its members.
# Nothing should ever use it, so it is emptied rather than left as a trap.
resource "aws_default_security_group" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${local.name}-default-do-not-use"
  }
}
```

- [ ] **Step 2: Verificar los CIDRs calculados**

Antes de dar por buena la aritmética de `cidrsubnet`, comprobarla:

```bash
python3 - <<'EOC'
import ipaddress
vpc = ipaddress.ip_network("10.60.0.0/16")
pub = [list(vpc.subnets(prefixlen_diff=8))[i] for i in (0, 1)]
priv = [list(vpc.subnets(prefixlen_diff=4))[i] for i in (1, 2)]
iso = [list(vpc.subnets(prefixlen_diff=8))[i] for i in (48, 49)]
print("public  ", pub)
print("private ", priv)
print("isolated", iso)
allocated = pub + priv + iso
for a in range(len(allocated)):
    for b in range(a + 1, len(allocated)):
        assert not allocated[a].overlaps(allocated[b]), f"OVERLAP {allocated[a]} {allocated[b]}"
print("no overlaps")
EOC
```

Expected: públicas `10.60.0.0/24` y `10.60.1.0/24`; privadas `10.60.16.0/20` y `10.60.32.0/20`; aisladas `10.60.48.0/24` y `10.60.49.0/24`, y la línea `no overlaps`. Si algún rango no coincide con lo que produce `cidrsubnet` en el `terraform plan` del CI, el que manda es el plan del CI: corregir los índices y volver a verificar.

- [ ] **Step 3: Verificar la configuración**

```bash
terraform fmt && terraform validate
```

Expected: válido.

- [ ] **Step 4: Commit**

```bash
git add src/longevo/open-wearables/environments/qa/network.tf
git commit -m "feat(open-wearables): add the qa vpc, subnets and routing"
```

---

### Task 4: VPC endpoints

Sin NAT, las tasks alcanzan AWS únicamente por endpoints. Los cinco de interface son exactamente los que el stack de servicios va a necesitar: bajar imágenes de ECR, escribir logs, leer secretos y permitir `ecs execute-command`.

**Files:**
- Create: `src/longevo/open-wearables/environments/qa/endpoints.tf`

**Interfaces:**
- Consumes: `aws_vpc.main`, `aws_subnet.private`, `aws_route_table.private`, `aws_route_table.isolated` de Task 3.
- Produces: `aws_security_group.endpoints` — nada más lo consume, pero es el que habilita el 443 hacia los endpoints.

- [ ] **Step 1: Escribir el archivo**

`src/longevo/open-wearables/environments/qa/endpoints.tf`:

```hcl
# Interface endpoints are billed per AZ, so qa runs them in a single AZ and prod
# will run them in all three. Everything the tasks need from AWS goes through
# here; there is no NAT and no route to the internet from the private subnets.
locals {
  interface_endpoints = toset([
    "ecr.api",       # ECR control plane: authentication and image manifests
    "ecr.dkr",       # ECR data plane: image layer pulls
    "logs",          # CloudWatch Logs: the awslogs driver
    "secretsmanager" # Secrets injected into task definitions
    ,
    "ssmmessages" # ecs execute-command, which replaces a bastion host
  ])

  endpoint_subnet_ids = [for az in slice(local.azs, 0, 1) : aws_subnet.private[az].id]
}

resource "aws_security_group" "endpoints" {
  name        = "${local.name}-endpoints"
  description = "HTTPS from inside the VPC to the interface endpoints"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTPS from the VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }

  tags = {
    Name = "${local.name}-endpoints"
  }
}

resource "aws_vpc_endpoint" "interface" {
  for_each = local.interface_endpoints

  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${module.global_constants.qa_aws_region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = local.endpoint_subnet_ids
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true

  tags = {
    Name = "${local.name}-${replace(each.key, ".", "-")}"
  }
}

# S3 is a gateway endpoint: free, and it is what makes ECR image pulls work,
# since the layers themselves live in S3.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${module.global_constants.qa_aws_region}.s3"
  vpc_endpoint_type = "Gateway"

  route_table_ids = concat(
    [for rt in aws_route_table.private : rt.id],
    [aws_route_table.isolated.id],
  )

  tags = {
    Name = "${local.name}-s3"
  }
}
```

- [ ] **Step 2: Verificar**

```bash
cd /Users/manupandolfi/Longevo/longevoIac/src/longevo/open-wearables/environments/qa && terraform fmt && terraform validate
```

Expected: válido. `terraform fmt` va a reacomodar la lista de `interface_endpoints`; dejarlo como la deje.

- [ ] **Step 3: Commit**

```bash
git add src/longevo/open-wearables/environments/qa/endpoints.tf
git commit -m "feat(open-wearables): add vpc endpoints so private subnets need no nat"
```

---

### Task 5: Flow logs

Evidencia de red para ISO 27001. Van a S3 y no a CloudWatch: sale más barato al volumen que genera una VPC con tráfico de ingesta, y evita un rol de IAM que no aporta nada.

**Files:**
- Create: `src/longevo/open-wearables/environments/qa/flow-logs.tf`

**Interfaces:**
- Consumes: `aws_vpc.main` (Task 3), `aws_kms_key.main` (Task 2), y el módulo compartido `src/modules/s3`.
- Produces: nada que otra task consuma.

- [ ] **Step 1: Leer el módulo de S3 antes de usarlo**

```bash
cat /Users/manupandolfi/Longevo/longevoIac/src/modules/s3/main.tf /Users/manupandolfi/Longevo/longevoIac/src/modules/s3/variables.tf /Users/manupandolfi/Longevo/longevoIac/src/modules/s3/outputs.tf
```

Confirmar los nombres exactos de las variables y de los outputs (`bucket_arn`, `bucket_name`) y si el módulo ya aplica cifrado, versionado y block public access. **Si el módulo no soporta una CMK**, usar sus defaults y anotarlo como concern en el reporte en lugar de modificar un módulo compartido por otros proyectos — eso sería un cambio con fanout a todo el repo.

- [ ] **Step 2: Escribir el archivo**

`src/longevo/open-wearables/environments/qa/flow-logs.tf`:

```hcl
# Flow logs land in S3 rather than CloudWatch Logs: at this volume S3 is
# materially cheaper, and the S3 destination needs no IAM role at all.
module "flow_logs_bucket" {
  source = "../../../../modules/s3"

  name        = "vpc-flow-logs"
  project     = module.constants.project
  environment = module.global_constants.qa_environment
}

resource "aws_flow_log" "main" {
  vpc_id               = aws_vpc.main.id
  traffic_type         = "ALL"
  log_destination_type = "s3"
  log_destination      = module.flow_logs_bucket.bucket_arn

  tags = {
    Name = local.name
  }
}

# Flow logs are evidence, not history. Ninety days covers an investigation window
# without paying to store traffic records nobody will read.
resource "aws_s3_bucket_lifecycle_configuration" "flow_logs" {
  bucket = module.flow_logs_bucket.bucket_name

  rule {
    id     = "expire-flow-logs"
    status = "Enabled"

    filter {}

    expiration {
      days = 90
    }
  }
}
```

- [ ] **Step 3: Verificar**

```bash
cd /Users/manupandolfi/Longevo/longevoIac/src/longevo/open-wearables/environments/qa && terraform fmt && terraform init -backend=false && terraform validate
```

`init` se vuelve a correr porque hay un módulo nuevo. Expected: válido.

- [ ] **Step 4: Commit**

```bash
git add src/longevo/open-wearables/environments/qa/flow-logs.tf
git commit -m "feat(open-wearables): ship vpc flow logs to s3 with a 90 day lifecycle"
```

---

### Task 6: Aurora PostgreSQL Serverless v2

**Files:**
- Create: `src/longevo/open-wearables/environments/qa/aurora.tf`

**Interfaces:**
- Consumes: `aws_vpc.main`, `aws_subnet.isolated` (Task 3), `aws_kms_key.main` (Task 2).
- Produces: `aws_rds_cluster.main`, `aws_security_group.aurora` — los consume Task 7 (outputs) y el plan del stack de servicios.

- [ ] **Step 1: Escribir el archivo**

`src/longevo/open-wearables/environments/qa/aurora.tf`:

```hcl
resource "aws_db_subnet_group" "aurora" {
  name       = "${local.name}-aurora"
  subnet_ids = [for s in aws_subnet.isolated : s.id]

  tags = {
    Name = "${local.name}-aurora"
  }
}

# Ingress is added by the stacks that need it, referencing this group. Nothing is
# allowed in from here, so an empty rule set is the correct starting state.
resource "aws_security_group" "aurora" {
  name        = "${local.name}-aurora"
  description = "Aurora cluster for ${local.name}"
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "${local.name}-aurora"
  }
}

resource "aws_rds_cluster_parameter_group" "main" {
  name        = "${local.name}-aurora17"
  family      = "aurora-postgresql17"
  description = "Cluster parameters for ${local.name}"

  # Reject any connection that is not over TLS. The application already supports
  # it; this makes a plaintext connection impossible rather than discouraged.
  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot"
  }

  # Schema and role changes are the audit surface worth keeping. Auditing DML on
  # tables of health data would produce an unaffordable volume of logs with no
  # forensic value.
  parameter {
    name  = "log_statement"
    value = "ddl"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }
}

resource "random_id" "aurora_final_snapshot" {
  byte_length = 4
}

resource "aws_rds_cluster" "main" {
  cluster_identifier = "${local.name}-aurora"
  engine             = "aurora-postgresql"
  engine_version     = "17.10"
  database_name      = "open_wearables"

  # RDS owns the master credential and rotates it. The application never uses it:
  # it connects as a role created by the migration job, so a rotation of this
  # secret cannot break a running task.
  master_username             = "postgres"
  manage_master_user_password = true
  master_user_secret_kms_key_id = aws_kms_key.main.arn

  db_subnet_group_name            = aws_db_subnet_group.aurora.name
  vpc_security_group_ids          = [aws_security_group.aurora.id]
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.main.name

  storage_encrypted = true
  kms_key_id        = aws_kms_key.main.arn

  backup_retention_period      = 7
  preferred_backup_window      = "07:00-08:00"
  preferred_maintenance_window = "mon:08:30-mon:09:30"

  deletion_protection       = true
  skip_final_snapshot       = false
  final_snapshot_identifier = "${local.name}-aurora-final-${random_id.aurora_final_snapshot.hex}"

  enabled_cloudwatch_logs_exports = ["postgresql"]

  # qa scales to zero when idle. The load profile is a migration spike followed by
  # a quiet baseline, which is the case Serverless v2 exists for.
  serverlessv2_scaling_configuration {
    min_capacity = 0
    max_capacity = 4
  }
}

resource "aws_rds_cluster_instance" "writer" {
  identifier         = "${local.name}-aurora-1"
  cluster_identifier = aws_rds_cluster.main.id
  instance_class     = "db.serverless"
  engine             = aws_rds_cluster.main.engine
  engine_version     = aws_rds_cluster.main.engine_version

  performance_insights_enabled    = true
  performance_insights_kms_key_id = aws_kms_key.main.arn

  # qa is single-AZ by design: a reader doubles the floor cost and buys
  # availability that a qa environment does not need.
  publicly_accessible = false
}
```

- [ ] **Step 2: Verificar**

```bash
cd /Users/manupandolfi/Longevo/longevoIac/src/longevo/open-wearables/environments/qa && terraform fmt && terraform validate
```

Expected: válido.

**Si `validate` rechaza `min_capacity = 0`** porque la versión del provider resuelta no lo soporta: no bajar la restricción del provider por tu cuenta. Poner `min_capacity = 0.5`, dejar un comentario explicando por qué, y reportar `DONE_WITH_CONCERNS` con la versión del provider que se resolvió (`terraform version`). Subir la restricción del provider afecta a todo el repo y es decisión del controller.

- [ ] **Step 3: Commit**

```bash
git add src/longevo/open-wearables/environments/qa/aurora.tf
git commit -m "feat(open-wearables): add the qa aurora serverless v2 cluster"
```

---

### Task 7: ElastiCache Valkey y outputs

Valkey y no Redis: ElastiCache no ofrece Redis OSS 8 — corta en 7.1 — y el compose del fork usa Redis 8. Valkey 9.1 es wire-compatible con lo que usan Celery y redis-py.

**Files:**
- Create: `src/longevo/open-wearables/environments/qa/valkey.tf`
- Create: `src/longevo/open-wearables/environments/qa/outputs.tf`

**Interfaces:**
- Consumes: `aws_vpc.main`, `aws_subnet.isolated` (Task 3), `aws_kms_key.main` (Task 2), `aws_rds_cluster.main`, `aws_security_group.aurora` (Task 6).
- Produces: los outputs que consumirá el plan del stack de servicios.

- [ ] **Step 1: Escribir el archivo de Valkey**

`src/longevo/open-wearables/environments/qa/valkey.tf`:

```hcl
resource "aws_elasticache_subnet_group" "valkey" {
  name       = "${local.name}-valkey"
  subnet_ids = [for s in aws_subnet.isolated : s.id]

  tags = {
    Name = "${local.name}-valkey"
  }
}

resource "aws_security_group" "valkey" {
  name        = "${local.name}-valkey"
  description = "Valkey cluster for ${local.name}"
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "${local.name}-valkey"
  }
}

# noeviction, not the ElastiCache default of volatile-lru. This instance is a
# Celery broker as well as a cache: under memory pressure volatile-lru would
# silently drop the task result keys that carry a TTL, whereas noeviction makes
# the pressure fail loudly and visibly instead of losing data quietly.
resource "aws_elasticache_parameter_group" "valkey" {
  name        = "${local.name}-valkey9"
  family      = "valkey9"
  description = "Parameters for ${local.name}"

  parameter {
    name  = "maxmemory-policy"
    value = "noeviction"
  }
}

resource "random_password" "valkey_auth" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "valkey_auth" {
  name       = "${local.name}-valkey-auth-token"
  kms_key_id = aws_kms_key.main.arn
}

resource "aws_secretsmanager_secret_version" "valkey_auth" {
  secret_id     = aws_secretsmanager_secret.valkey_auth.id
  secret_string = random_password.valkey_auth.result
}

resource "aws_elasticache_replication_group" "main" {
  replication_group_id = "${local.name}-valkey"
  description          = "Celery broker and cache for ${local.name}"

  engine         = "valkey"
  engine_version = "9.1"
  node_type      = "cache.t4g.micro"

  # qa runs a single node: no replica, no automatic failover. prod gets both.
  num_cache_clusters         = 1
  automatic_failover_enabled = false
  multi_az_enabled           = false

  parameter_group_name = aws_elasticache_parameter_group.valkey.name
  subnet_group_name    = aws_elasticache_subnet_group.valkey.name
  security_group_ids   = [aws_security_group.valkey.id]

  at_rest_encryption_enabled = true
  kms_key_id                 = aws_kms_key.main.arn
  transit_encryption_enabled = true
  auth_token                 = random_password.valkey_auth.result

  maintenance_window       = "mon:09:30-mon:10:30"
  snapshot_retention_limit = 1
  snapshot_window          = "06:00-07:00"

  tags = {
    Name = "${local.name}-valkey"
  }
}
```

- [ ] **Step 2: Escribir los outputs**

`src/longevo/open-wearables/environments/qa/outputs.tf`:

```hcl
output "vpc_id" {
  value       = aws_vpc.main.id
  description = "VPC that hosts the whole environment"
}

output "private_subnet_ids" {
  value       = [for s in aws_subnet.private : s.id]
  description = "Subnets for ECS tasks"
}

output "public_subnet_ids" {
  value       = [for s in aws_subnet.public : s.id]
  description = "Subnets for the load balancer"
}

output "aurora_endpoint" {
  value       = aws_rds_cluster.main.endpoint
  description = "Writer endpoint of the Aurora cluster"
}

output "aurora_security_group_id" {
  value       = aws_security_group.aurora.id
  description = "Security group to authorise database clients against"
}

output "aurora_master_secret_arn" {
  value       = aws_rds_cluster.main.master_user_secret[0].secret_arn
  description = "RDS-managed secret holding the master credential. The application does not use it; the migration job creates its own roles."
}

output "valkey_primary_endpoint" {
  value       = aws_elasticache_replication_group.main.primary_endpoint_address
  description = "Primary endpoint of the Valkey replication group"
}

output "valkey_security_group_id" {
  value       = aws_security_group.valkey.id
  description = "Security group to authorise cache clients against"
}

output "valkey_auth_secret_arn" {
  value       = aws_secretsmanager_secret.valkey_auth.arn
  description = "Secret holding the Valkey auth token"
}

output "kms_key_arn" {
  value       = aws_kms_key.main.arn
  description = "Environment CMK, for stacks that create their own encrypted resources"
}
```

- [ ] **Step 3: Verificar**

```bash
cd /Users/manupandolfi/Longevo/longevoIac/src/longevo/open-wearables/environments/qa && terraform fmt && terraform validate
```

Expected: válido.

- [ ] **Step 4: Revisar el conjunto completo con ojos frescos**

```bash
terraform fmt -check -recursive /Users/manupandolfi/Longevo/longevoIac/src/longevo/open-wearables
```

Expected: sin salida. Después leer los siete archivos de una sentada y confirmar tres cosas: que ningún recurso quedó en la subnet equivocada, que ninguna capa privada o aislada tiene ruta a `0.0.0.0/0`, y que todo lo que se cifra usa `aws_kms_key.main`.

- [ ] **Step 5: Commit**

```bash
git add src/longevo/open-wearables/environments/qa/valkey.tf src/longevo/open-wearables/environments/qa/outputs.tf
git commit -m "feat(open-wearables): add the qa valkey replication group and stack outputs"
```

---

## Antes de abrir el PR

Dos cosas que no son código y que el implementador **no** debe hacer solo:

1. **El entorno de GitHub `qa-longevo-open-wearables`** puede necesitar existir para que el job de plan corra. Si el workflow falla por eso, avisar al controller — se crea desde Settings → Environments, es una acción manual del usuario.
2. **Mergear aplica.** El CI de este repo hace apply automático al mergear a `master`, sin approval. Este PR crea una VPC, un cluster Aurora y un ElastiCache reales. El plan del job es lo que hay que leer antes de mergear, no el resumen del comentario.

## Fuera del alcance de este plan

| Plan | Contenido |
|---|---|
| 3 | Plataforma y servicios en qa: cluster ECS, ALB, WAF, los cinco servicios, workflow de deploy y rol OIDC del repo del fork |
| 4 | `sdk-ingest` y observabilidad en qa: bucket, SNS, DLQ, alarmas, canary |
| 5 | Replicar 1 a 4 en prod, un módulo por PR, con 3 AZs y los sizings de prod |

Dos cosas más que este plan deliberadamente no hace:

- **Los roles `app` y `migrator` de PostgreSQL.** El spec pide separar la credencial de runtime de la que aplica DDL, pero crearlas desde Terraform exigiría el provider `cyrilgdn/postgresql`, y los runners de GitHub Actions no tienen ruta hacia una Aurora en subnets aisladas. Las crea el job de migración, que sí corre dentro de la VPC. Va en el plan 3, junto con ese job.
- **El guard de dedupe**, que es código del repo del fork y bloquea la primera cohorte de migración.
