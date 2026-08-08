# AWS environment

This directory deploys one Tracewake environment: a VPC, an application load
balancer for tenants, a private load balancer for workers, ECS/Fargate services
for the Go control plane and the Python worker, RDS PostgreSQL for
authoritative hosted state, a private versioned S3 bucket for bundles and
results, an SQS job queue with a dead-letter queue, ECR repositories, Secrets
Manager secrets, least-privilege IAM roles, and CloudWatch log groups.

The same code runs locally against PostgreSQL and the filesystem store; nothing
here changes Tracewake semantics. Local recording, replay, verification, import,
export, and comparison never need this environment.

## What the operator must decide

Terraform state contains the database password and bootstrap tokens, so the S3
backend is a required partial configuration. Choose an account, region, state
bucket, certificate, and cost ceiling before deploying. `certificate_arn` is
required: the public listener redirects HTTP to HTTPS, and browser sessions use
Secure cookies.

Standing cost is dominated by the NAT gateway, two load balancers, the RDS
instance, and the running Fargate tasks. Destroy the environment when it is not
in use.

## Deploy

```sh
cd deploy/aws
terraform init \
  -backend-config="bucket=<state-bucket>" \
  -backend-config="key=tracewake/prod.tfstate" \
  -backend-config="region=<region>"
terraform apply -var region=<region> -var certificate_arn=<acm-certificate-arn> \
  -var image_tag=$(git rev-parse --short HEAD)
```

An AWS account still on the free-tier plan caps automated backups; pass
`-var database_backup_retention_days=1` there.

The first apply creates empty ECR repositories, so the services cannot start
until images exist. Tasks run on `task_architecture` (ARM64 by default), and
the images must be built for it. Build, push, then roll the services:

```sh
account=$(aws sts get-caller-identity --query Account --output text)
region=<region>
tag=$(git rev-parse --short HEAD)
aws ecr get-login-password --region "$region" \
  | docker login --username AWS --password-stdin "$account.dkr.ecr.$region.amazonaws.com"

control_plane=$(terraform output -raw control_plane_repository)
worker=$(terraform output -raw worker_repository)

docker build --platform linux/arm64 -f controlplane/Dockerfile -t "$control_plane:$tag" ../..
docker build --platform linux/arm64 -f Dockerfile.worker -t "$worker:$tag" ../..
docker push "$control_plane:$tag"
docker push "$worker:$tag"

aws ecs update-service --cluster "$(terraform output -raw cluster_name)" \
  --service tracewake-prod-control-plane --force-new-deployment
aws ecs update-service --cluster "$(terraform output -raw cluster_name)" \
  --service tracewake-prod-worker --force-new-deployment

terraform apply -var region=<region> -var certificate_arn=<acm-certificate-arn> \
  -var image_tag=$(git rev-parse --short HEAD)
```

Read the bootstrap tenant token from Secrets Manager. It is never printed to
logs:

```sh
aws secretsmanager get-secret-value \
  --secret-id "$(terraform output -raw tenant_token_secret)" \
  --query SecretString --output text
```

Then use the ordinary remote commands:

```sh
export TRACEWAKE_REMOTE_URL=$(terraform output -raw public_base_url)
export TRACEWAKE_TOKEN=<token-from-secrets-manager>
tracewake remote upload run.bundle.tar
tracewake remote runs
```

## Schema migrations

The control plane applies embedded migrations at startup inside a migration
ledger and refuses to start against a schema newer than itself. A deploy is
therefore a migration: push the new image and update `image_tag`. Because the
database is only reachable from inside the VPC, run any manual inspection from
a task in the private subnets rather than opening the database to the internet.

Roll a schema change out in this order:

1. Deploy the image whose migrations are additive and backward compatible.
2. Confirm the service is healthy and the ledger contains the new version.
3. Only then deploy code that depends on the new shape.

## Rollback

Application rollback is `terraform apply -var image_tag=<previous-tag>`; ECS
replaces tasks with the previous image. This is safe whenever the older code
still understands the current schema version — the control plane refuses to
start otherwise, which is the intended failure rather than silent
reinterpretation.

Schema rollback is an operator decision, not an automatic repair. The down
migrations under `contracts/postgres/` drop hosted tables and types and are
destructive; use a point-in-time restore of the RDS instance instead when
retained hosted data matters.

## Teardown

```sh
terraform destroy -var region=<region> -var certificate_arn=<acm-certificate-arn>
```

The artifact bucket is created with `force_destroy` and the database with
`skip_final_snapshot` so a disposable environment can be created and removed
repeatedly. Both are variables: set `database_skip_final_snapshot = false` and
`database_deletion_protection = true`, and remove `force_destroy` from the
bucket, before any environment holds real tenant data.

## Observability

Both services write operational telemetry to standard output, which the awslogs
driver ships to the environment's CloudWatch log groups. Spans are one JSON
object per line in the OpenTelemetry span model; metrics are CloudWatch
embedded metric format, so they become metrics in `Tracewake/<environment>/…`
without a metrics agent or collector. This telemetry describes the services. It
is unrelated to the OTLP artifacts Tracewake produces for a tenant, which describe
a recorded run.

A job notification carries its W3C trace context, so one trace covers the
request that created a job, the outbox publication, the claim, the worker's
download, analysis, upload, and the artifact commit — across both languages.

`alarms.json` holds every alarm as data; `observability.tf` provisions them
from that file, and the control-plane tests check each custom-namespace alarm
against the metrics the service actually emits. `alarm_actions` is empty by
default: alarm state is visible in CloudWatch without a notification target,
and the operator supplies one.

## Retention and deletion

The control plane enforces retention itself, because it is the only component
that knows what a successful job still references:

| Data | Retained |
| --- | --- |
| Input bundles and authoritative result artifacts | 90 days |
| Failed uploads and orphan attempt outputs | 24 hours |
| Idempotency records | 24 hours |
| Published notifications | 7 days |
| Audit records | 365 days |

Past its deadline an artifact stops appearing on its job and stops being
downloadable, and a run stops being readable, listable, and analysable. The
artifact row itself survives: it is the record of exactly what a job committed,
including digest, size, and object version, after the bytes are gone. Object
cleanup removes anything the database no longer retains, which is why an
expired or deleted object disappears without a second bookkeeping system.

`DELETE /v1/runs/{run_id}` is a tenant deletion request. It expires the run and
every artifact derived from it immediately, so the data stops being reachable
in the same transaction that records the request; the next cleanup pass removes
the stored bytes. Bucket lifecycle rules expire noncurrent object versions
after one day, so a deleted object's earlier versions go too.

## Backup and disaster recovery

Authoritative hosted state is the RDS database. Automated backups are enabled
with `database_backup_retention_days` (7 by default), which also enables
point-in-time recovery. Artifacts live in a versioned bucket; their identity —
key and object version — is recorded in the database, so a database restore and
the bucket together describe a consistent system.

Recovery objective: the database can be recovered to any point inside the
backup window, so the exposure is the retention setting, not a snapshot
interval. Recovery time is dominated by the RDS restore, which creates a new
instance.

To recover:

1. Restore the instance to the chosen time
   (`aws rds restore-db-instance-to-point-in-time`), or restore a snapshot.
2. Point `TRACEWAKE_DATABASE_URL` at the restored instance by updating the Secrets
   Manager secret, then force a new deployment of both services.
3. Confirm the schema ledger matches the deployed image before admitting
   traffic. The control plane refuses to start against a newer schema.
4. Expect duplicate notifications for work that was in flight. Duplicate
   delivery cannot create duplicate authoritative work, and an attempt whose
   lease expired during the outage is fenced and retried.
5. Objects written after the restore point are orphans: no successful job
   references them, so retention removes them on schedule.

Do not restore the bucket to an earlier state independently of the database. A
successful job records the exact object version it committed; rolling objects
back without the database would leave a successful job referencing a version
that no longer exists.

The `backup_and_restore` scenario in `evidence/` exercises the dump, drop, and
reload path against the same schema and confirms runs, jobs, artifacts, and
audit records survive it.

## Boundaries this environment enforces

* The database and the bucket have no public route; the bucket blocks public
  access and refuses non-TLS requests.
* Only the control-plane task role may read or write artifacts and publish
  notifications. Workers hold queue permissions only and reach objects through
  short-lived URLs the control plane issues.
* The worker API listens on a separate port published only through the internal
  load balancer, which accepts traffic from the worker security group.
* Queue visibility matches the database lease, so a fenced worker cannot keep a
  message hidden.
* Both services receive their credentials from Secrets Manager; no token is
  printed, logged, or committed.

## Artifact integrity in object storage

A presigned upload cannot bind a checksum S3 enforces: the AWS SDK places
`x-amz-checksum-sha256` in the query string, where the service neither verifies
nor stores it. Uploading bytes that contradict the declaration therefore
succeeds at the object store.

The declared digest is proven afterwards instead. Committing an artifact
verifies the exact object version and size, compares the stored checksum when
one exists, and otherwise re-reads and hashes the object up to
`MaxVerifiedReadSize`. A bundle larger than that is proven by mandatory
validation, which refuses to make a run `ready` unless the stored bytes hash to
the digest its upload declared.
