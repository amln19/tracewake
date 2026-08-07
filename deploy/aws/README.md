# AWS environment

This directory deploys one Locus environment: a VPC, an application load
balancer for tenants, a private load balancer for workers, ECS/Fargate services
for the Go control plane and the Python worker, RDS PostgreSQL for
authoritative hosted state, a private versioned S3 bucket for bundles and
results, an SQS job queue with a dead-letter queue, ECR repositories, Secrets
Manager secrets, least-privilege IAM roles, and CloudWatch log groups.

The same code runs locally against PostgreSQL and the filesystem store; nothing
here changes Locus semantics. Local recording, replay, verification, import,
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
  -backend-config="key=locus/prod.tfstate" \
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
  --service locus-prod-control-plane --force-new-deployment
aws ecs update-service --cluster "$(terraform output -raw cluster_name)" \
  --service locus-prod-worker --force-new-deployment

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
export LOCUS_REMOTE_URL=$(terraform output -raw public_base_url)
export LOCUS_TOKEN=<token-from-secrets-manager>
locus remote upload run.bundle.tar
locus remote runs
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
