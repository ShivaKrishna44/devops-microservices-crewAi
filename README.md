# DevOps Microservices — CI/CD to EKS

Three small Flask microservices (`order-service`, `payment-service`,
`user-service`) built, containerized, and deployed to an Amazon EKS cluster
via GitHub Actions + Helm, with the underlying infrastructure managed by
Terraform.

> This repo previously also contained an AI multi-agent EKS-upgrade tool.
> That agent (and its guardrails, approval gate, tests, and dedicated CI
> workflow) has been removed from this repo. What remains below is the
> microservices + infra baseline.

---

## What's in this repo

```
devops-microservices-crewAi/
├── app/
│   ├── order-service/      # Flask app — Dockerfile + requirements.txt
│   ├── payment-service/    # Flask app — Dockerfile + requirements.txt
│   └── user-service/       # Flask app — Dockerfile + requirements.txt
├── charts/
│   └── microservice/       # shared Helm chart, one values-<service>.yaml per service
├── Terraform/
│   ├── vpc.tf, eks.tf, iam-*.tf, ecr.tf, ...   # VPC + EKS + IAM + ECR
│   └── variables.tf, output.tf, backend.tf
├── .github/workflows/
│   ├── ci-cd.yml           # build -> push to ECR -> update Helm values (GitOps)
│   └── codeql.yml          # static analysis
└── docs/
    └── LAMBDA-TO-EC2-MIGRATION.md
```

## Microservices

Each service under `app/<service>/` is a minimal Flask app with its own
`Dockerfile` and pinned `requirements.txt`. They're intentionally simple —
the point of this repo is the pipeline and infra around them, not the
business logic inside them. Each Dockerfile builds a non-root, gunicorn-served
image on `python:3.11-slim`.

## CI/CD (`.github/workflows/ci-cd.yml`)

On a push to `main` touching `app/**`, `charts/**`, or the workflow file
itself:
1. `detect-changes` figures out which service(s) actually changed.
2. For each changed service: build the Docker image, push it to ECR
   (auth via GitHub OIDC — `aws-actions/configure-aws-credentials`, no
   static AWS keys), then commit the new image tag into
   `charts/microservice/values-<service>.yaml` (GitOps — a separate
   process/ArgoCD is expected to pick up that commit and actually deploy it).

Can also be triggered manually via `workflow_dispatch` for a single service.

## Infrastructure (`Terraform/`)

Standard VPC + EKS setup using the `terraform-aws-modules` registry modules,
plus hand-written IAM for the node group and IRSA roles (EBS CSI driver, ALB
controller). Remote state in S3. See the inline comments in each `.tf` file
— they're written tutorial-style, explaining the "why" alongside the "what."

## End-to-end: running this project from zero to a live URL

Follow these in order. Each step depends on the one before it.

### 0. Prerequisites

- AWS account + credentials configured locally (`aws configure` or SSO)
- `terraform` >= 1.10, `kubectl`, `helm`, `aws` CLI
- A GitHub repo (fork/clone of this one) with Actions enabled
- A registered domain with a Route 53 hosted zone (only needed if you want
  the Ingress's real hostname + HTTPS working — you can skip this and hit
  the ALB's own DNS name over plain HTTP otherwise)

### 1. Provision the AWS infrastructure (Terraform)

The S3 backend bucket and DynamoDB lock table referenced in
`Terraform/tfvars/dev/backend.tfvars` must already exist — Terraform's S3
backend does not create them for you.

```bash
# One-time: create the state bucket + lock table if they don't exist yet
aws s3api create-bucket --bucket <your-tf-state-bucket> --region us-east-1
aws dynamodb create-table --table-name <your-lock-table> \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST

# Update Terraform/tfvars/dev/backend.tfvars with your bucket/table names,
# then:
cd Terraform
terraform init -backend-config=tfvars/dev/backend.tfvars
terraform plan -var-file=tfvars/dev/dev.tfvars
terraform apply -var-file=tfvars/dev/dev.tfvars
```

This creates: VPC + subnets, the EKS cluster (`expense-dev` by default) with
a managed node group, ECR repositories for all three services, and the IAM
roles (node group + IRSA for EBS CSI + ALB controller).

```bash
terraform output   # note cluster_name, cluster_endpoint, etc.
```

### 2. Point kubectl at the new cluster

```bash
aws eks update-kubeconfig --name expense-dev --region us-east-1
kubectl get nodes    # confirms auth + connectivity before going further
```

### 3. Install cluster add-ons Terraform doesn't install for you

Terraform creates the IAM *roles* for these controllers (IRSA), but the
controllers themselves are installed via Helm, not Terraform, in this repo:

```bash
# AWS Load Balancer Controller — required for the chart's ALB Ingress
helm repo add eks https://aws.github.io/eks-charts
helm repo update

# Use a values file rather than --set for the annotation — the dotted
# "eks\.amazonaws\.com/role-arn" key's backslash-escaping is unreliable
# across shells (Git Bash / PowerShell on Windows in particular can mangle
# it, producing "Error: INSTALLATION FAILED: failed parsing --set data:
# error parsing index: EOF"). A values file has no such escaping problem.
cat > alb-controller-values.yaml <<EOF
clusterName: expense-dev
serviceAccount:
  create: true
  annotations:
    eks.amazonaws.com/role-arn: "$(terraform output -raw alb_controller_role_arn)"
EOF

helm install aws-load-balancer-controller eks/aws-load-balancer-controller \
  -n kube-system \
  -f alb-controller-values.yaml

# Argo Rollouts — only needed if you'll set rollout.enabled: true in any
# values-<service>.yaml for canary deploys (off by default, see chart values)
kubectl create namespace argo-rollouts
kubectl apply -n argo-rollouts -f https://github.com/argoproj/argo-rollouts/releases/latest/download/install.yaml
```

### 4. Set up GitHub Actions OIDC (no static AWS keys in CI)

```bash
# If not already created in this AWS account:
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1

# Create a role trusted by your repo (see .github/workflows/ci-cd.yml for
# the exact AWS_ROLE_ARN this pipeline expects), with permissions to push
# to ECR and commit back to this repo's default branch.
```
Update `AWS_ROLE_ARN` in `.github/workflows/ci-cd.yml` to match the role you
created, and confirm the ECR registry URL (`ECR_REGISTRY` env var in the
same file) matches your AWS account ID.

### 5. Trigger the pipeline

Push a change under `app/order-service/**` (or `payment-service`/`user-service`),
or trigger it manually.

**Via the GitHub web UI (no extra tooling needed):** repo → **Actions** tab →
**CI/CD Pipeline — Build & Deploy Microservices** → **Run workflow** →
pick a `service_name` → **Run workflow**.

**Via the GitHub CLI**, if you have `gh` installed
(`winget install --id GitHub.cli` on Windows, then `gh auth login` once):
```bash
gh workflow run ci-cd.yml -f service_name=order-service
```

This builds the image, pushes it to ECR, and commits the new tag into
`charts/microservice/values-order.yaml`. Confirm in the Actions tab that the
run succeeds and the commit landed.

### 6. Deploy via Helm (or point ArgoCD at this repo)

The CI/CD pipeline only updates the values file (GitOps) — it does not
`helm upgrade` for you. Either deploy manually the first time:

```bash
helm install order-service charts/microservice \
  -f charts/microservice/values.yaml \
  -f charts/microservice/values-order.yaml \
  -n default
```

or point ArgoCD's Application at `charts/microservice` with the matching
`values-<service>.yaml` so it syncs automatically on every commit the
pipeline makes — this is the intended long-term setup and avoids manual
`helm upgrade` after every deploy.

Repeat for `payment-service` and `user-service` using their own
`values-<service>.yaml`.

### 7. Confirm it's live

```bash
kubectl get ingress                     # find the ALB's DNS name
kubectl get pods -w                     # watch pods come up healthy
curl -H "Host: app.vosukula.online" http://<alb-dns-name>/order
```

If you own the domain in `ingress.host` (`values.yaml`) and it's in a Route
53 hosted zone, point a record at the ALB's DNS name (or an alias record)
and hit it directly over HTTPS using the ACM cert already wired into
`templates/ingress.yaml`.

### 8. Teardown (avoid ongoing cost)

```bash
helm uninstall order-service payment-service user-service
cd Terraform
terraform destroy -var-file=tfvars/dev/dev.tfvars
```
Destroy the Helm releases before `terraform destroy` — otherwise the ALB
Ingress resources can leave orphaned load balancers in AWS after the
cluster is gone.

## Docs

- [`docs/LAMBDA-TO-EC2-MIGRATION.md`](docs/LAMBDA-TO-EC2-MIGRATION.md) — a
  step-by-step guide for migrating a Java Lambda function to a single EC2
  instance, covering all common trigger types.
