# Testing Guide — guarded-eks-upgrade-agent

Three tiers, from zero-risk to a real upgrade. Do them in order.

| Tier | Needs | Risk | What it proves |
|------|-------|------|----------------|
| 1. Unit tests | Python only | none | The safety logic is correct |
| 2. Guardrail behavior | AWS + kubectl (read-only) | none | The gates block the right things on a real cluster |
| 3. Full upgrade | A **throwaway** EKS cluster | high (real upgrade) | End-to-end flow |

> ⚠️ EKS upgrades are irreversible. Never run Tier 3 against a cluster you care about.

---

## Tier 1 — Unit tests (no cluster, no AWS) — start here

Proves the guardrails, approval gate, availability logic, sequence gate, and
health/validation logic — all with faked `kubectl`/`aws` output.

```bash
cd guarded-eks-upgrade-agent
pip install pytest python-dotenv
python -m pytest tests/ -v
```

**Expected:** `97 passed`. That's the full safety surface verified with no
external dependencies (CrewAI not required — the tools fall back to a shim).

Run a single area:
```bash
python -m pytest tests/test_guardrails.py -v          # version/cluster/region rules
python -m pytest tests/test_approval_gate.py -v       # hash/TTL/two-person/account binding
python -m pytest tests/test_availability_tools.py -v  # PDB strength, surge quota, capacity
python -m pytest tests/test_health_tools.py -v         # baseline, regression, validation loop
python -m pytest tests/test_availability_monitor.py -v # live breach + halt
python -m pytest tests/test_sequence_gate.py -v         # control-plane -> nodes gate
python -m pytest tests/test_preapply_gate.py -v         # prod two-person at apply
```

---

## Tier 2 — Guardrail behavior on a real cluster (READ-ONLY, no upgrade)

Proves the agent's pre-flight checks and gates behave correctly against a live
cluster — **without ever applying an upgrade**. Safe on any cluster.

### Setup
```bash
cd app
python -m venv venv
# Windows: venv\Scripts\activate   |   Linux/Mac: source venv/bin/activate
pip install -r requirements.txt      # includes crewai for the live agent
cp .env.example .env
# edit .env: CREWAI_LLM + key, EKS_CLUSTER_NAME, AWS_REGION, TERRAFORM_DIR
aws eks update-kubeconfig --name <YOUR_CLUSTER> --region <REGION>
kubectl get nodes                    # confirm connectivity
```

### 2a. Gate status (100% safe — no cluster calls)
```bash
python main.py --status
```
Expected: "No pending or approved upgrade on record." (or the current record).

### 2b. Invalid jump is blocked (guardrail proof)
Assuming the cluster is on e.g. 1.33, ask for an illegal jump:
```bash
python main.py --target-version 1.36
```
When prompted for the cluster name, type it correctly. **Expected:** the
guardrail report shows `[BLOCK] single_minor_step` (can't skip minors) and the
run stops **before** any approval or apply. Nothing is changed.

Also try a downgrade (`--target-version 1.32`) → same block.

### 2c. Wrong cluster name is blocked
```bash
python main.py --target-version 1.34
# at the prompt, type the WRONG cluster name on purpose
```
**Expected:** `[BLOCK] cluster_name_confirmation` → run stops.

### 2d. Valid jump runs pre-checks (still no apply)
```bash
python main.py --target-version 1.34
# type the correct cluster name
```
**Expected:** the pre-check evidence prints — current version, deprecated-API
scan, addon compatibility, node readiness, **PDB coverage + strength**, capacity
headroom, EC2 surge quota, and a baseline snapshot. Then it asks you to type
`APPROVE`. **Type anything else** (e.g. `no`) to abort safely — no upgrade runs.

### 2e. PDB strength — the two failure modes (optional, illustrative)
Create a deliberately bad PDB to see the check fire, then delete it:
```bash
# TOO STRICT (deadlock): allows zero disruption
kubectl create pdb demo-strict --namespace default --selector app=demo --min-available=100%
python main.py --target-version 1.34   # expect ALERT: PDBs too STRICT
kubectl delete pdb demo-strict -n default
```
(Adjust namespace/selector to a real multi-replica deployment in your cluster,
or add it to `CRITICAL_NAMESPACES` in `.env`.)

### 2f. Cluster-already-updating guard
If you happen to run this while the cluster status is `UPDATING`, the run aborts
with "status is 'UPDATING', not ACTIVE". You can't easily force this safely, so
it's mainly covered by the unit test `test_cluster_upgradeable_alert_when_updating`.

---

## Provisioning a test cluster (the agent does NOT create clusters)

**Important:** this agent is an *upgrader*, not a *provisioner*. It has no tool
to create a cluster — it only moves an **existing** cluster one minor version
up. So you must launch a cluster first, then point the agent at it.

For a true end-to-end test, the cluster **must be Terraform-managed** (the agent
upgrades by bumping the `eks_version` variable and running `terraform apply`).

### Recommended: use the companion Terraform (Terraform-managed)
The `devops-microservices-crewAi` repo has a full EKS Terraform setup with an
`eks_version` variable — perfect for this.

```powershell
cd C:\Devops\Repository\devops-microservices-crewAi\Terraform
terraform init -backend-config=tfvars/dev/backend.tfvars
terraform apply -var-file=tfvars/dev/dev.tfvars
#  -> creates cluster 'expense-dev' on the eks_version in variables.tf (e.g. 1.33)
```

Then point the agent at it in `app/.env`:
```bash
EKS_CLUSTER_NAME=expense-dev
AWS_REGION=us-east-1
TERRAFORM_DIR=C:\Devops\Repository\devops-microservices-crewAi\Terraform
```

Give it something to protect (so the availability path has real work):
```bash
kubectl create deployment demo --image=nginx --replicas=3
kubectl create pdb demo --selector=app=demo --min-available=2   # a SANE pdb (not 100%)
```

### Alternative: eksctl (quick, but NOT for the real upgrade test)
```powershell
eksctl create cluster --name test-upgrade --version 1.33 --nodes 2 --region us-east-1
```
⚠️ An eksctl cluster has **no Terraform state**, so the agent's `terraform apply`
won't drive it. Use this only to exercise Tier 2 (read-only guardrails); use the
Terraform option above for Tier 3.

### 💸 Cost + teardown (read this)
A real EKS cluster is **not free**:
- Control plane ≈ **$0.10/hour (~$73/month)** — billed even when idle
- Plus EC2 nodes (2× t3.medium ≈ another ~$60/month if left running)

**Launch → test → destroy the same day.** Don't leave it running.
```powershell
# when done (Terraform option)
cd C:\Devops\Repository\devops-microservices-crewAi\Terraform
terraform destroy -var-file=tfvars/dev/dev.tfvars

# or (eksctl option)
eksctl delete cluster --name test-upgrade --region us-east-1
```

> Free alternative: Tier 1 (unit tests) fakes all cluster responses and proves
> the same logic at **zero cost**. Only spin up a real cluster when you want to
> watch the actual upgrade happen.

---

## Tier 3 — Full end-to-end upgrade (THROWAWAY cluster only)

Only after Tiers 1 & 2 pass, and after provisioning a cluster (above). This
performs a **real, irreversible** upgrade.

### Pre-req recap
A Terraform-managed cluster one minor behind, with ≥2 nodes and a multi-replica
demo deployment + a *sane* PDB (see "Provisioning a test cluster" above).

### Single-approver flow (dev cluster)
```bash
cd app
python main.py --target-version <current+1>
#  1. pre-checks + terraform plan print
#  2. type the cluster name to confirm
#  3. guardrails run and CLEAR
#  4. type APPROVE  (+ optional reason)

python main.py --apply --target-version <current+1>
#  5. re-verifies approval (hash+TTL), then:
#     Phase 1 control plane -> sequence gate (AWS ACTIVE + kubectl responsive)
#     Phase 2 node groups + LIVE availability monitor
#     post-upgrade validation loop (nodes Ready, old nodes gone, replicas match)
#     regression check
```
**Expected success:** `SUCCESS: EKS upgrade to <ver> applied ...` then
`PASS` on version, health loop, and regression. Exit code 0.

### Two-person flow (name the cluster with `prod`/`production`/`live`)
```bash
python main.py --target-version <current+1> --actor alice   # opens request + 1st approval
python main.py --target-version <current+1> --actor bob --approve   # distinct 2nd approver
python main.py --status                                       # shows 2/2 APPROVED
python main.py --apply --target-version <current+1>
```

### Reset the gate between experiments
```bash
python main.py --reset
```

---

## What "correct" looks like (acceptance summary)

| Scenario | Expected result |
|----------|-----------------|
| Illegal version jump | Blocked at guardrails, no apply |
| Wrong cluster name typed | Blocked at guardrails |
| Pre-checks return UNSAFE (deprecated API / addon / too-strict or too-loose PDB / surge shortfall) | Blocked; run aborts before approval |
| Not approved / rejected | `--apply` refuses (`BLOCKED`) |
| Approval expired (older than `APPROVAL_TTL_MINUTES`) | `--apply` refuses |
| Plan drifted since approval | `--apply` refuses (evidence-hash mismatch) |
| Prod cluster, only 1 approver | `--apply` refuses (needs 2 distinct) |
| Availability breach during rollover | Alarm fired + draining halted; result `COMPLETED WITH ALARM` (exit 1) |
| Old nodes not terminated / replicas don't match baseline | Validation loop ALERTs (exit 1) |
| Clean upgrade | `SUCCESS` + all validation PASS (exit 0) |

---

## CLI reference

```bash
python main.py --status                              # show gate state
python main.py --target-version X.Y                  # pre-checks + guardrails + approve (interactive)
python main.py --target-version X.Y --actor NAME --approve   # record an approval (e.g. 2nd approver)
python main.py --apply --target-version X.Y          # execute (after approval)
python main.py --reset                               # clear the current decision
```

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `ModuleNotFoundError` running pytest | Run from repo root; `tests/conftest.py` adds `app/` + `app/tools/` to the path |
| `--apply` says "no approved request" | Run the pre-check + approve flow first; check `--status` |
| `--apply` says "evidence/plan changed" | Cluster/plan drifted since approval — re-run pre-check + approve |
| `--apply` says "approval expired" | Older than `APPROVAL_TTL_MINUTES`; re-approve |
| OIDC / AWS auth errors | `aws sts get-caller-identity` to confirm creds; check region |
| Sequence gate never opens | API server slow post-upgrade; raise `APISERVER_LATENCY_THRESHOLD_S` |
```
