# DevOps Microservices Platform (GitHub Actions + ArgoCD)

Production-ready microservices platform on AWS EKS using **GitHub Actions** for CI and **ArgoCD** for CD (GitOps).

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                         CI/CD Flow (No Jenkins)                          │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  Developer → git push → GitHub (main branch)                             │
│                              ↓                                           │
│  GitHub Actions (CI):                                                    │
│    ├── Detect changed services (paths-filter)                            │
│    ├── Build Docker image                                                │
│    ├── Push to AWS ECR                                                   │
│    └── Update image tag in Helm values → git push                        │
│                              ↓                                           │
│  ArgoCD (CD — GitOps):                                                   │
│    ├── Polls Git every 3 min                                             │
│    ├── Detects new image tag in values file                              │
│    └── Auto-syncs to EKS cluster                                         │
│                              ↓                                           │
│  EKS Cluster:                                                            │
│    ├── order-service   (namespace: order-service)                        │
│    ├── payment-service (namespace: payment-service)                      │
│    └── user-service    (namespace: user-service)                         │
│                                                                          │
│  Monitoring: Prometheus + Grafana                                        │
│  Code Quality: SonarQube                                                 │
│  Progressive Delivery: Argo Rollouts (canary/blue-green)                 │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Why GitHub Actions Over Jenkins?

|    Aspect      | Jenkins                                        | GitHub Actions |
|----------------|------------------------------------------------|----------------|
| Infrastructure | Need EC2 agent + EKS pod                       | Zero infra — runs on GitHub cloud |
| Maintenance    | Plugin updates, disk space, agent connectivity | Zero maintenance |
| Cost           | EC2 running 24/7 ($50+/month)                  | Free for public repos, 2000 min/month for private |
| Setup time     | 2+ hours (Helm, secrets, agent, plugins)       | 5 minutes (add workflow file) |
| Integration    | Manual webhook + credential setup              | Native GitHub integration |
| Scaling        | Need more agents for parallel builds           | Auto-scales (unlimited runners) |

---
   

## Project Structure

```
.
├── .github/workflows/
│   └── ci-cd.yml              ← GitHub Actions CI/CD pipeline
├── app/
│   ├── order-service/         ← Python Flask microservice
│   ├── payment-service/       ← Python Flask microservice
│   └── user-service/          ← Python Flask microservice
├── charts/
│   └── microservice/          ← Helm chart (shared across services)
├── kubernetes/
│   ├── argocd/apps/           ← ArgoCD Application CRDs
│   ├── ingress/               ← ALB ingress rules
│   ├── monitoring/            ← Grafana values
│   └── sonarqube/             ← SonarQube values
├── scripts/                   ← Cluster setup scripts
├── Terraform/                 ← Infrastructure as Code (EKS + VPC + ECR)
├── mcp-server/                ← AI monitoring agent (MCP)
└── README.md
```

---

## Quick Start

### 1. Provision Infrastructure
```bash
cd Terraform
terraform init -backend-config=tfvars/dev/backend.tfvars
terraform apply -var-file=tfvars/dev/dev.tfvars
```

### 2. Configure kubectl
```bash
aws eks update-kubeconfig --name expense-dev --region us-east-1
kubectl get nodes
```

### 3. Install Cluster Components
```bash
bash scripts/01-install-tools.sh
bash scripts/02-install-alb-controller.sh
bash scripts/04-install-argocd.sh
bash scripts/05-install-monitoring.sh
bash scripts/06-install-sonarqube.sh
bash scripts/07-install-argo-rollouts.sh
```

### 4. Add GitHub Secrets
Go to: GitHub repo → Settings → Secrets and variables → Actions

| Secret Name             |         Value       |
|-------------------------|---------------------|
| `AWS_ACCESS_KEY_ID`     | Your AWS access key |
| `AWS_SECRET_ACCESS_KEY` | Your AWS secret key |

### 5. Push Code → Auto Deploy
```bash
git add .
git commit -m "initial commit"
git push origin main
```

GitHub Actions will:
1. Detect which service changed
2. Build Docker image
3. Push to ECR
4. Update Helm values with new tag
5. ArgoCD auto-syncs to cluster

---

## How the Pipeline Works

### Auto-trigger (on push to main)
- Only builds services that actually changed (uses `dorny/paths-filter`)
- Commits new image tag to Helm values
- ArgoCD picks up the change and deploys

### Manual trigger (workflow_dispatch)
- Go to: Actions → CI/CD Pipeline → Run workflow
- Select service + optional tag
- Builds and deploys the selected service

---

## Deployment Flow

```
Push code to main
    ↓
GitHub Actions detects: app/order-service/** changed
    ↓
Builds: docker build → ECR push (tag: commit SHA)
    ↓
Updates: charts/microservice/values-order.yaml → tag: "abc123def"
    ↓
Commits & pushes the values change
    ↓
ArgoCD detects Git change (polls every 3 min)
    ↓
ArgoCD syncs: deploys new image to EKS
    ↓
✅ order-service running with new version
```

---

## URLs

| Service     | URL                              |
|-------------|----------------------------------|
| ArgoCD      | https://argocd.vosukula.online   |
| Grafana     | https://grafana.vosukula.online  |
| SonarQube   | https://sonar.vosukula.online    |
| Application | https://app.vosukula.online      |

---

## Tech Stack

| Component           | Tool                              |
|---------------------|-----------------------------------|
| Cloud               | AWS (EKS, ECR, VPC, ALB, Route53) |
| IaC                 | Terraform                         |
| CI                  | GitHub Actions                    |
| CD (GitOps)         | ArgoCD                            |
| Containers          | Docker, Helm                      |
| Orchestration       | Kubernetes (EKS)                  |
| Monitoring          | Prometheus + Grafana              |
| Code Quality        | SonarQube                         |
| Progressive Delivery| Argo Rollouts                     |
| AI Monitoring       | MCP Server (custom)               |
