# Deployment & Operations Guide — EKS Upgrade by Agent
# guarded-eks-upgrade-agent — Deployment & Operations Guide

How to deploy and operate the human-approved EKS upgrade agent. The agent takes
a **target Kubernetes version from a human**, runs read-only pre-checks, enforces
**deterministic guardrails**, requires **explicit human approval** (two-person for
prod), and only then performs the upgrade via Terraform — with post-upgrade
validation.

> Config values below use placeholders. Replace `<AWS_ACCOUNT_ID>`,
> `<AWS_REGION>`, and `<CLUSTER_NAME>` with your own. Never commit real account
> IDs, keys, or `.env`.

---

## Architecture

```
Human provides target version (e.g. 1.34)
          │
          ▼
 ┌─────────────────────┐   read-only
 │ Pre-Check Agent      │   aws eks / kubectl / addon + node checks
 └─────────┬───────────┘
           ▼
 ┌─────────────────────┐   read-only
 │ Upgrade Planner      │   terraform plan  → GO/NO-GO
 └─────────┬───────────┘
           ▼
 ╔═════════════════════╗   deterministic guardrails (non-LLM)
 ║ GUARDRAILS           ║   single-minor rule · cluster-name · region · verdict
 ╚═════════┬═══════════╝
           ▼
 ╔═════════════════════╗   HUMAN GATE (typed cluster name + APPROVE;
 ║ APPROVAL GATE        ║   two-person for prod; evidence-hash bound; TTL)
 ╚═════════┬═══════════╝
           ▼ (only if APPROVED)
 ┌─────────────────────┐   terraform apply (control plane → node groups)
 │ Executor Agent       │   refuses without a valid approval record
 └─────────┬───────────┘
           ▼
 ┌─────────────────────┐   read-only
 │ Post-Upgrade Validator│  version + node/pod health → PASS/FAIL
 └─────────────────────┘
```

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.12+ | for the agent (`app/`) |
| `terraform` on PATH | drives the actual upgrade |
| `aws` CLI + credentials | prefer a profile / IRSA / instance role over static keys |
| `kubectl` on PATH | pre-check + validation |
| An existing EKS cluster managed by Terraform | with an `eks_version` variable |
| An LLM key | e.g. `OPENROUTER_API_KEY` for `openrouter/openrouter/free` |

---

## 1. Install

```bash
cd app
python -m venv venv
# Windows: venv\Scripts\activate   |   Linux/Mac: source venv/bin/activate
pip install -r requirements.txt
```

## 2. Configure

```bash
cp .env.example .env
# edit .env
```

Key settings (see `app/.env.example` for the full list):

```bash
CREWAI_LLM=openrouter/openrouter/free
OPENROUTER_API_KEY=<your-key>

EKS_CLUSTER_NAME=<CLUSTER_NAME>
AWS_REGION=<AWS_REGION>

# Path to the Terraform dir that manages the cluster (has eks_version var).
# Defaults to ../Terraform relative to app/.
# TERRAFORM_DIR=/abs/path/to/Terraform

# Guardrail / approval policy
APPROVAL_TTL_MINUTES=60
ALLOWED_REGIONS=<AWS_REGION>
PROD_CLUSTER_MARKERS=prod,production,live
REQUIRE_TWO_PERSON_FOR_PROD=true
```

## 3. Verify the safety logic (no AWS needed)

```bash
# from repo root
pip install pytest
python -m pytest tests/ -v
```

These tests cover the guardrails and approval gate (single-minor rule, wrong
cluster, region allow-list, evidence-hash drift, two-person, TTL expiry) without
touching AWS. Run them before trusting the tool.

---

## 4. Run an upgrade (local, interactive)

```bash
cd app
python main.py --target-version 1.34
```

Flow:
1. Read-only pre-checks + `terraform plan` print evidence.
2. You type the exact cluster name to confirm; guardrails run.
3. You type `APPROVE` (agents cannot self-approve).
4. Apply:
   ```bash
   python main.py --apply --target-version 1.34
   ```
5. Post-upgrade validation prints PASS/FAIL.

Utility commands:
```bash
python main.py --status     # show gate state + who approved
python main.py --reset      # clear the current decision (history kept)
```

## 5. Two-person approval (production)

```bash
# 1) first approver opens the request (runs pre-checks + guardrails)
python main.py --target-version 1.34 --actor alice

# 2) a DISTINCT second approver (re-runs pre-check, hash-verified)
python main.py --target-version 1.34 --actor bob --approve

# 3) apply once status is APPROVED (2/2)
python main.py --apply --target-version 1.34
```

Production clusters (name contains a `PROD_CLUSTER_MARKERS` substring) require
two distinct approvers. The same person cannot approve twice.

---

## 6. CI/CD with human approval (GitHub Actions)

Workflow: `.github/workflows/eks-upgrade.yml`

Two jobs:
1. **`precheck-plan`** — read-only pre-checks + `terraform plan`. Always safe.
2. **`apply`** — gated behind the `production-eks-upgrade` **GitHub Environment**.
   Pauses until a **required reviewer approves**, then applies.

### One-time setup

**a. AWS OIDC role** (no static keys in CI). In IAM:
- Add identity provider `token.actions.githubusercontent.com` (audience `sts.amazonaws.com`).
- Create a role whose trust policy allows this repo:
  ```json
  "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
  "token.actions.githubusercontent.com:sub": "repo:<OWNER>/<REPO>:*"
  ```
- Grant it only what an upgrade needs (EKS + the Terraform backend). Avoid `AdministratorAccess`.

**b. GitHub repo config** (Settings → Secrets and variables → Actions):
- Variables: `AWS_ROLE_ARN`, `AWS_REGION`, `EKS_CLUSTER_NAME`, `CREWAI_LLM`
- Secrets: `OPENROUTER_API_KEY`

**c. Approval environment** (Settings → Environments):
- Create `production-eks-upgrade` → add **Required reviewers**. This reviewer
  approval is the human-in-the-loop gate in CI.

### Trigger

Actions → **EKS Upgrade (Agent + Human Approval)** → Run workflow → enter target
version. Review the plan in Job 1, approve the environment, Job 2 applies.

---

## Guardrails & approval — what blocks what

| Layer | Mechanism | Blocks |
|-------|-----------|--------|
| Read-only agents | Pre-check/Planner/Validator have no write tools | any accidental change |
| `single_minor_step` | version math | downgrade / no-op / skip / major change |
| `cluster_name_confirmation` | typed name must match | wrong-cluster upgrades |
| `region_allowlist` | `ALLOWED_REGIONS` | wrong region/account |
| `precheck_verdict` | scans evidence | UNSAFE / NO-GO / no-compatible-addon |
| Human gate | typed `APPROVE` (+ two-person for prod) | unattended / self-approved applies |
| Evidence-hash bind | SHA-256 of reviewed plan | applying a plan that drifted since approval |
| TTL | `APPROVAL_TTL_MINUTES` | stale approvals |
| Gated executor | re-verifies the approval record | apply without a valid approval |

Full detail: see the repo `README.md`.

---

## Rollback reality (read before you run)

**EKS control-plane upgrades are NOT reversible.** You cannot downgrade a control
plane. There is no "undo" for the version bump itself. What you *can* do:

- **Node groups:** if node upgrades misbehave, you can roll worker nodes back to
  the previous launch template / AMI while the control plane stays put.
- **Workloads:** standard `kubectl rollout undo` for app deployments (unrelated
  to the K8s version).
- **The safe path is prevention:** the pre-checks (deprecated APIs, addon
  compatibility) and the approval gate exist precisely because the version bump
  can't be undone. Take them seriously.

Because of this: **always upgrade a non-prod cluster first**, one minor at a time,
and only promote to prod after validation.

---

## Troubleshooting

| Symptom | Likely cause / fix |
|---------|--------------------|
| `BLOCKED: no approval on record` | Run the pre-check + approve flow first; check `python main.py --status` |
| `BLOCKED: evidence/plan changed since approval` | The cluster/plan drifted; re-run pre-checks and re-approve |
| `BLOCKED: approval expired` | Older than `APPROVAL_TTL_MINUTES`; re-approve |
| Guardrail `single_minor_step` blocks | You tried to skip/downgrade; upgrade one minor at a time |
| Guardrail `region_allowlist` blocks | Region not in `ALLOWED_REGIONS` |
| OIDC `Not authorized to perform sts:AssumeRoleWithWebIdentity` | Trust policy `sub`/`aud` mismatch or missing OIDC provider (see step 6a) |
| `terraform plan` fails to init | Backend/credentials; run `terraform init` in `TERRAFORM_DIR` manually |
| Apply hangs | Control plane + node rollout is slow (can take 20-40+ min); the tool allows a long timeout |
| Pre-check `check_ec2_surge_quota` ALERTs | Not enough EC2 vCPU quota for surge nodes — raise the `L-1216C47A` quota before upgrading |
| Node group stuck in `UPDATING`, surge node never launches | **Surge-capacity freeze** — hit the EC2 On-Demand vCPU quota. Request a `L-1216C47A` increase, wait for it, then re-run `terraform apply`; the node group resumes. Control plane (phase 1) is unaffected. |

---

## Safety checklist before a real run

- [ ] Tested on a throwaway cluster first
- [ ] Target is exactly current + 1 minor
- [ ] Pre-checks returned SAFE (no deprecated APIs, addons compatible, nodes Ready)
- [ ] Reviewed the `terraform plan` output
- [ ] Correct cluster name confirmed
- [ ] Two-person approval for production
- [ ] You accept the upgrade is irreversible
