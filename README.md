# EKS Upgrade by Agent (Human-Approved)
# guarded-eks-upgrade-agent

An AI multi-agent workflow that upgrades an Amazon EKS cluster **one minor version at a time**, driven by a target version a human provides, and executed **only after explicit human approval**.

The point of this project is not "let AI upgrade the cluster." It's the opposite: EKS upgrades are high-risk and irreversible (you can't downgrade the control plane), so the agents do the *tedious, error-prone* work — checking compatibility, drafting the plan, validating afterward — while a human stays firmly in control of the one dangerous action: applying the upgrade.

**Docs:** [Setup & Usage](#setup) · [DEMO-GUIDE.md](DEMO-GUIDE.md) (walkthrough) · [docs/TESTING-GUIDE.md](docs/TESTING-GUIDE.md) (how to test) · [docs/DEPLOYMENT-GUIDE.md](docs/DEPLOYMENT-GUIDE.md) (deploy & operate) · [bin/README.md](bin/README.md) (helper scripts)

---

## The workflow

```
   Human provides target version (e.g. 1.34)
                  │
                  ▼
   ┌──────────────────────────────┐
   │ 1. PRE-CHECK AGENT            │  Reads current version, validates the jump,
   │                              │  scans for deprecated APIs, checks addon
   │                              │  compatibility and node readiness.
   └──────────────┬───────────────┘
                  ▼
   ┌──────────────────────────────┐
   │ 2. UPGRADE PLANNER AGENT      │  Runs `terraform plan`, summarizes exactly
   │                              │  what will change, and produces a
   │                              │  GO / NO-GO recommendation with evidence.
   └──────────────┬───────────────┘
                  ▼
        ╔══════════════════════════╗
        ║  APPROVAL GATE (HUMAN)    ║  Nothing is applied until a human types
        ║  APPROVE / REJECT         ║  APPROVE. Decision is logged with actor,
        ╚══════════════┬═══════════╝  timestamp, reason, and the evidence shown.
                  │ (only if APPROVED)
                  ▼
   ┌──────────────────────────────┐
   │ 3. EXECUTOR AGENT             │  `terraform apply` — control plane first,
   │                              │  then managed node groups. Gated: refuses
   │                              │  to run without a valid approval token.
   └──────────────┬───────────────┘
                  ▼
   ┌──────────────────────────────┐
   │ 4. POST-UPGRADE VALIDATOR     │  Confirms new version, nodes Ready,
   │                              │  system pods healthy. Reports PASS/FAIL.
   └──────────────────────────────┘
```

## The agents

| Agent | Responsibility | Can it change anything? |
|-------|----------------|-------------------------|
| **Pre-Check Agent** | Validate the target version, scan deprecated APIs, addon/node readiness | No — read-only |
| **Upgrade Planner** | `terraform plan`, summarize the diff, GO/NO-GO recommendation | No — plan only |
| **Executor Agent** | `terraform apply` (control plane → node groups) | **Yes — but only with an APPROVED token** |
| **Post-Upgrade Validator** | Verify version, node readiness, pod health | No — read-only |

## Guardrails & approval gates (defense in depth)

Safety is layered — an upgrade must clear **every** layer. If any one blocks, nothing is applied.

### Layer 1 — Read-only by default
Three of the four agents (Pre-Check, Planner, Validator) have **no tools that can change anything**. Only the Executor can modify infrastructure, and only when unlocked.

### Layer 1.5 — Deterministic pre-flight (before anything else)
Run directly (not via the LLM) at the start of `request_and_check`:
- **Cluster exists and is ACTIVE** — if the cluster is `UPDATING` (an upgrade/change already in flight), or the name/account/region is wrong, the run **aborts before any plan**. (Also enforced as `check_cluster_upgradeable` in the pre-check agent.)

### Layer 2 — Deterministic guardrails (non-LLM) — `guardrails.py`
Pattern-based checks that run before any apply. They cannot be "talked out of" a no, because no LLM is in the path:

| Guardrail | Blocks when… | Severity |
|-----------|--------------|----------|
| `version_format` | target isn't a valid version (e.g. `1.34`) | CRITICAL |
| `single_minor_step` | not exactly current+1 minor (downgrade, skip, or major change) | CRITICAL |
| `cluster_name_confirmation` | the human didn't type the exact target cluster name | CRITICAL |
| `region_allowlist` | region isn't in `ALLOWED_REGIONS` | CRITICAL |
| `prod_two_person` | prod cluster + two-person required (raises the approver count) | WARN |
| `precheck_verdict` | the read-only pre-check evidence contained UNSAFE / NO-GO | CRITICAL |

**Availability pre-checks (zero-downtime readiness)** — run in the pre-check phase:

| Check | Flags when… |
|-------|-------------|
| `check_pdb_coverage` | multi-replica app workloads have **no PodDisruptionBudget** — a node drain could evict all replicas at once |
| `check_pdb_strength` | a critical PDB is **outside the safe band** — either **too loose** (`disruptionsAllowed/currentHealthy` > threshold → over-eviction downtime) or **too strict** (`disruptionsAllowed == 0`, e.g. `maxUnavailable: 0` / `minAvailable == replicas` → the drain stalls or force-evicts → downtime) (preventive) |
| `check_capacity_headroom` | fewer than **2 Ready nodes** — a rolling node replacement would drain the only node, causing downtime |
| `check_ec2_surge_quota` | the account lacks **EC2 vCPU quota** to launch the surge nodes — the rollover would **freeze mid-upgrade** (see below) |

#### Prevention + detection, together

Availability during the node rollover is protected two ways:
- **Prevention (`check_pdb_strength`, pre-flight):** verifies critical deployments have a PDB strict enough that Kubernetes itself will **refuse** a drain that would drop them below the threshold. A PDB that merely exists isn't enough — a `maxUnavailable: 50%` PDB still permits a 50% drop. This check reads each PDB's live `disruptionsAllowed / currentHealthy` and blocks the upgrade if it exceeds the threshold.
- **Detection (live monitor, during rollover):** the concurrent monitor sounds the alarm the moment healthy pods actually drop below the floor.

The preventive PDB check is the real control (it stops the disruption from happening); the live monitor is the safety net that catches anything the PDBs didn't.

#### The surge-capacity freeze (why `check_ec2_surge_quota` matters)

True zero-downtime node upgrades bring up **new (surge) nodes before draining old ones**. Those surge instances count against the EC2 **"Running On-Demand Standard instances" vCPU quota** (`L-1216C47A`). If the account is near that limit:

1. The managed node group tries to launch the surge node.
2. AWS refuses (quota exceeded).
3. The rollout **freezes** — old nodes aren't drained, new nodes can't launch, the node group sits in `UPDATING` until it times out or you intervene.

`check_ec2_surge_quota` estimates the surge vCPUs the node groups will request (instance type × surge nodes per group) and compares against remaining quota headroom (`quota − in-use`). A shortfall reports `ALERT` / `UNSAFE`, which the `precheck_verdict` guardrail treats as **blocking** — so you fix the quota *before* approving, not mid-rollout.

**Recovery if it does freeze:** request an increase to the `L-1216C47A` quota (Service Quotas console), wait for it to apply, then re-run `terraform apply` — the node group resumes the rollout from where it stalled. The control plane (already upgraded in phase 1) is unaffected.

### Layer 3 — Human approval gate — `approval_gate.py`
- **Typed cluster-name confirmation** — the operator must type the exact cluster name, preventing "right command, wrong cluster" mistakes.
- **Explicit APPROVE** — a human types `APPROVE`; agents cannot self-approve.
- **Two-person approval for production** — clusters matching `PROD_CLUSTER_MARKERS` require **two distinct approvers** (configurable). One person cannot approve twice.
- **Evidence binding (hash-only)** — the approval is tied to a SHA-256 hash of the pre-check + plan evidence. The raw plan is **not stored** — only its hash. A separate approver re-runs the read-only pre-check; the gate verifies the freshly-generated evidence hashes to the same value. If the cluster/plan drifted since the request, the hash won't match and the approval is refused.
- **Expiry (TTL)** — approvals go stale after `APPROVAL_TTL_MINUTES`, so a forgotten approval can't be used later against a now-different cluster.
- **Version binding** — approving `1.34` never approves `1.35`.
- **Account + region binding** — the approval records the AWS account ID and region; a same-named cluster in a different account/region can't reuse the approval (re-verified at apply).
- **Two-person enforced at apply** — for a production cluster, the apply gate independently requires ≥ 2 **distinct** approvers on record, even if the stored `required_approvers` was somehow lower (belt-and-suspenders, not just advisory).

### Layer 4 — Gated executor + deterministic apply path
The apply is driven **deterministically**, not from an LLM summary:
- `do_apply` first **re-verifies the approval against freshly-regenerated evidence** (`approval_check`) — so plan **drift between approval and apply is caught** (evidence-hash mismatch) and an **expired approval (TTL)** is rejected.
- It then calls the executor and validation tools directly and decides pass/fail from their **own status strings** (`SUCCESS` / `COMPLETED WITH ALARM` / `BLOCKED` / `FAILED`). A degraded or failed upgrade **cannot be narrated into a success** by the model.
- **`COMPLETED WITH ALARM` (an availability breach during rollover) is treated as FAILURE** (non-zero exit), not success.
- The executor tool itself also re-verifies the stored approval record (version, `APPROVED`, enough distinct approvers, not expired) — it validates the **record the human created**, never agent-supplied text. Any failure returns `BLOCKED` and touches nothing.

### Layer 5 — Zero-downtime execution (sequencing + validation loop)
The apply is **phased**, not a single blind `terraform apply`:

1. **Baseline first** — before touching anything, a health snapshot is captured (which nodes/pods/deployments are healthy now) so regressions can be detected later.
2. **Phase 1 — control plane only** (`-target` the cluster). Node groups are not touched yet.
3. **Sequence gate (dual, before touching nodes)** — the run **blocks and loops** until BOTH are true on the same iteration, stable for two consecutive checks:
   - **AWS API:** `describe-cluster` reports the control plane is `ACTIVE` on the target version, and
   - **Kubernetes API:** `kubectl get nodes` actually responds within `APISERVER_LATENCY_THRESHOLD_S` (default 10s).
   A control plane can report `ACTIVE` in the AWS API while the API server is still slow right after an upgrade — so we require the real `kubectl` round-trip to be fast before proceeding. If the gate isn't satisfied in time, the run **halts before node groups**.
4. **Phase 2 — node groups, with a LIVE availability monitor** — the managed rolling replacement (surge node up, old node drained) runs in a background thread while a monitor polls **every 15s**. The moment a **critical deployment drops below its availability floor** — `ceil(baseline_healthy × (1 − AVAILABILITY_DROP_THRESHOLD))`, e.g. losing more than **20%** of its baseline healthy pods — it:
   - **sounds the alarm immediately** (error log + optional webhook), and
   - if `HALT_ON_AVAILABILITY_BREACH=true` (default), **halts further node draining** so a human can debug — see below.
   The result is flagged `COMPLETED WITH ALARM` (and, if halted, includes the cordoned nodes + resume steps) so the breach is never silently swallowed.

#### How "halt further draining" works (and its honest constraint)

A managed-node-group `terraform apply` **cannot be safely killed mid-instance** — interrupting it can leave the node group in a worse, inconsistent state. So the halt does **not** kill terraform. Instead it stops the drain *wave* where Kubernetes controls it: it **cordons every old (pre-flight) node still present**, marking them unschedulable so no further pods move onto them, and — combined with the strict PDBs verified pre-flight — Kubernetes **refuses further evictions**. The in-flight instance finishes, then progress stops.

**Resume after debugging:** fix the workload (scale up, fix the failing pod, loosen nothing you shouldn't), then `kubectl uncordon <node>` the cordoned nodes and re-run `terraform apply` to finish the rollover. Set `HALT_ON_AVAILABILITY_BREACH=false` for alarm-only behavior (no cordon).
5. **Validation loop (after)** — `wait_for_healthy` polls cluster health **every 30s for up to 20 minutes**, and passes only when **all** acceptance criteria hold together:
   - every node is **Ready**,
   - every **old (pre-flight) node is fully terminated** — a stalled rollover that leaves old nodes lingering must not pass,
   - no pods in a bad state, and
   - **deployment replica counts match the pre-flight baseline** (no workload silently lost/gained replicas).
   On timeout it reports exactly which criteria are still unmet.
6. **Regression check** — `compare_to_baseline` flags anything that was healthy **before** the upgrade but is broken **now**. Pre-existing problems don't count; new breakage does. The validator only reports PASS if version is correct, the cluster is healthy, and there are no regressions.

### Layer 6 — Correct, auditable record
- **Full audit trail** — every event (request, each approval, rejection, reset) is persisted with actor, timestamp, reason, and evidence hash, so the whole decision chain is reconstructable.

### Honest limitations
- Zero-downtime depends on your **workloads** being HA (multiple replicas + PDBs + anti-affinity). The agent *checks* for PDBs, node capacity, and EC2 surge quota and *warns/blocks*, but it can't make a single-replica app highly available.
- The surge-quota check estimates vCPUs from instance type × surge nodes and the `L-1216C47A` quota. It's an estimate (unknown instance types use a conservative fallback; it doesn't account for RIs/Savings Plans or non-Standard families) — treat an ALERT as "investigate," and it can't see real-time AWS capacity, only your quota.
- The phased `-target` apply assumes the standard `terraform-aws-modules/eks/aws` resource address (`module.eks.aws_eks_cluster.this`). If your module structure differs, adjust the target in `upgrade_tools.py`.
- The regression check is a coarse health compare (pod status, deployment readiness), not deep app-level SLO monitoring. For production, pair it with real synthetic checks / Prometheus alerts.

### Configurable policy (`.env`)
| Setting | Default | Purpose |
|---------|---------|---------|
| `APPROVAL_TTL_MINUTES` | `60` | how long an approval stays valid (0 = never) |
| `ALLOWED_REGIONS` | `us-east-1` | regions the agent may operate in |
| `PROD_CLUSTER_MARKERS` | `prod,production,live` | substrings that mark a cluster as production |
| `REQUIRE_TWO_PERSON_FOR_PROD` | `true` | require two distinct approvers for prod |

## Beyond upgrade — other gated cluster operations

The same safety model (deterministic guardrails → typed cluster-name confirmation → typed `APPROVE` → evidence-hash + TTL binding → apply-time re-verification → deterministic status prefixes) now covers four additional operations. **None of them bypass the human approval gate** — there is no auto-approve or skip-approval path anywhere.

Select the operation with `--operation`. Default is `upgrade` (unchanged behavior).

| Operation | CLI | Approval identity is bound to | Operation-specific guardrails |
|-----------|-----|-------------------------------|-------------------------------|
| **upgrade** (default) | `--operation upgrade --target-version 1.34` | the target version | single-minor step, version format |
| **launch** | `--operation launch --target-version 1.34` | the target version | cluster must **not already exist** (status `ABSENT`), version format |
| **scale** | `--operation scale --nodes 4` | the desired node count | node count sane (**no scale-to-zero**, no absurd counts), cluster ACTIVE |
| **addon** | `--operation addon --addon vpc-cni` | the addon name | addon must be named (no blanket change), cluster ACTIVE |
| **teardown** | `--operation teardown` | the cluster name | **strictest** — see below |

Every operation still runs the operation-agnostic checks: typed cluster-name match, region allow-list, and the pre-check verdict scan.

### Teardown is gated hardest

Destroying a cluster is total and irreversible, so `teardown` adds controls on top of everything above:

- **Always two-person** — teardown requires **two distinct approvers regardless of whether the cluster looks like production** (`gates.ALWAYS_TWO_PERSON_OPERATIONS`). The apply gate *and* the executor tool each independently enforce this, even if a stored `required_approvers` were somehow lower.
- **Cluster name typed twice** — the operator types the exact cluster name at two separate prompts (`gr_teardown_double_confirm`). A single mistyped/auto-filled prompt can't trigger a destroy.
- **Production is refused outright** — a cluster matching `PROD_CLUSTER_MARKERS` is blocked at the guardrail layer (`gr_teardown_not_prod`), a hard stop, not a warning.
- **Cluster must be ACTIVE** — you can't tear down something mid-update.

### How the generalized gate stays backward-compatible

The approval store identity was generalized from `target_version` to an `(operation, target)` pair. To keep the existing upgrade path and its tests byte-for-byte identical:
- `operation` defaults to `"upgrade"` everywhere.
- For `upgrade`, the `target` **is** the target version, so the historical binding is preserved.
- A stored approval record with **no** `operation` key is treated as `"upgrade"`.

Cross-operation isolation is enforced: a `scale` approval can never authorize a `teardown`, a `launch` approval can never authorize an `upgrade`, and an approval for `scale --nodes 5` can't authorize `scale --nodes 9`. (Covered by `tests/test_operations.py`.)

All operations are **Terraform-driven** through the same `TERRAFORM_DIR` — the agent never hand-rolls AWS API creates/destroys. Launch/scale/addon/teardown live in `app/tools/operation_tools.py` and return the same deterministic `SUCCESS` / `BLOCKED` / `FAILED` prefixes the apply path keys off. (The phased live-monitor path that can emit `COMPLETED WITH ALARM` remains specific to `upgrade`.)

## What it upgrades

The cluster is managed by Terraform (point `TERRAFORM_DIR` at your EKS Terraform — see **Terraform setup** below). A version upgrade is a single variable change:

```hcl
variable "eks_version" {
  default = "1.33"   # ← the agent proposes bumping this to the human's target
}
```

Terraform then upgrades the control plane and node groups through the official `terraform-aws-modules/eks/aws` module.

## Project layout

```
guarded-eks-upgrade-agent/
├── app/
│   ├── main.py            # human-driven entrypoint (input → checks → approval → apply → validate)
│   ├── crew.py            # CrewAI agents + tasks + crew builder
│   ├── guardrails.py      # deterministic (non-LLM) blocking checks
│   ├── approval_gate.py   # evidence-hash + TTL + account-bound approvals, two-person for prod
│   ├── config.py          # settings + guardrail/approval/availability policy
│   ├── tools/
│   │   ├── eks_tools.py       # pre-upgrade checks (version, APIs, addons, PDB, capacity, surge quota)
│   │   ├── upgrade_tools.py   # phased terraform plan/apply (apply is gated) + sequence gate
│   │   └── health_tools.py    # baseline, live availability monitor, halt-on-breach, validation loop
│   ├── requirements.txt
│   └── .env.example
├── bin/                   # helper scripts: setup.sh/.ps1, run.sh/.ps1, test.sh
├── tests/                 # pytest — runnable proof of the safety logic (no cluster needed)
├── .github/workflows/eks-upgrade.yml   # CI with a human approval Environment gate
├── docs/DEPLOYMENT-GUIDE.md · docs/TESTING-GUIDE.md
├── DEMO-GUIDE.md
├── .gitignore
└── README.md
```

## Setup

### Requirements
- **Python 3.10–3.13** for the live agent. ⚠️ **Not 3.14** — CrewAI requires
  `>=3.10,<3.14`, so it will not install on Python 3.14. (The unit tests in
  `tests/` run on any version, including 3.14, via a tool shim.)
- `aws` CLI (configured), `kubectl`, and `terraform` on your PATH.
- An LLM key (e.g. `OPENROUTER_API_KEY`, or Groq etc. per `CREWAI_LLM`).

### Quickest path — the setup script (recommended)
The scripts in `bin/` handle the Python-version / venv / install dance for you.
They auto-pick a compatible Python (3.13 → 3.12 → 3.11), create `app/venv`,
install dependencies, create `app/.env` from the example, and run a safe
`--status` check.

**Git Bash:**
```bash
bash bin/setup.sh
```
**PowerShell:**
```powershell
./bin/setup.ps1
```

Then edit `app/.env` and wire kubectl to your cluster:
```bash
# app/.env
EKS_CLUSTER_NAME=expense-dev
AWS_REGION=us-east-1
TERRAFORM_DIR=/absolute/path/to/your/Terraform   # dir with the eks_version variable
CREWAI_LLM=openrouter/openrouter/free            # + the matching API key

aws eks update-kubeconfig --name expense-dev --region us-east-1
```

### Manual setup (if you prefer)
```bash
cd app
py -3.13 -m venv venv                 # 3.13/3.12/3.11 — NOT 3.14
source venv/Scripts/activate          # Git Bash;  PowerShell: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                  # then edit it
python main.py --status
```

### Running the unit tests (no cluster, no CrewAI needed)
```bash
pip install pytest python-dotenv
python -m pytest tests/ -v            # 97 passing — proves the safety logic
# or:  bash bin/test.sh
```

## Terraform setup

This project drives an existing Terraform config that manages the EKS cluster —
it does **not** duplicate that infrastructure code (duplication causes drift).
Point it at your Terraform in one of two ways:

**Option A — set the path (recommended for local runs):**
```bash
# in app/.env
TERRAFORM_DIR=/absolute/path/to/your/Terraform   # the dir with the eks_version variable
```

**Option B — vendor a copy into this repo** (for a self-contained CI run):
```bash
# copy ONLY the .tf files + tfvars, NOT .terraform/, *.tfstate, or backups
mkdir Terraform
cp /path/to/source/Terraform/*.tf Terraform/
cp -r /path/to/source/Terraform/tfvars Terraform/
cp /path/to/source/Terraform/.terraform.lock.hcl Terraform/
# then remove any committed backend state / init dir before committing
```
The GitHub Actions workflow expects the Terraform at `./Terraform` (Option B).

The only variable the agent changes is `eks_version` — it passes
`-var="eks_version=<target>"` to plan/apply. Everything else in your Terraform
stays as-is.

## Usage

> `bin/run.sh` (Bash) / `bin/run.ps1` (PowerShell) run the agent through the
> venv automatically — **no need to activate it**. Or activate the venv and call
> `python main.py` directly. Both forms are shown below.

### Local (interactive, single approver)
```bash
# via the run script (recommended — uses the venv automatically)
bash bin/run.sh --target-version 1.34
# 1. Read-only pre-checks + terraform plan run and print evidence
# 2. You type the cluster name to confirm; deterministic guardrails run
# 3. You type APPROVE (agents cannot self-approve)
bash bin/run.sh --apply --target-version 1.34
# 4. Executor re-verifies approval, applies (control plane -> nodes), validates

# equivalent, with the venv activated:
#   cd app && python main.py --target-version 1.34
#             python main.py --apply --target-version 1.34
```

### Two-person approval (production)
```bash
bash bin/run.sh --target-version 1.34 --actor alice          # opens request + 1st approval
bash bin/run.sh --target-version 1.34 --actor bob --approve  # DISTINCT 2nd approver
bash bin/run.sh --status                                     # shows 2/2 APPROVED
bash bin/run.sh --apply --target-version 1.34
```

### Utility commands
```bash
bash bin/run.sh --status     # show gate state + who has approved (safe, no cluster calls)
bash bin/run.sh --reset      # clear the current decision
```

PowerShell equivalents: `./bin/run.ps1 --status`, `./bin/run.ps1 --target-version 1.34`, etc.

### CI/CD (GitHub Actions, approval via Environment)
Trigger **Actions → EKS Upgrade (Agent + Human Approval) → Run workflow**, enter
the target version. Then:
1. **Job 1 (precheck-plan)** runs read-only pre-checks + `terraform plan`. Always safe.
2. **Job 2 (apply)** is gated behind the `production-eks-upgrade` **GitHub Environment**.
   It pauses until a **required reviewer approves**. Only then does it apply.

Set it up once: **Settings → Environments → New environment →
`production-eks-upgrade` → add Required reviewers**. That reviewer approval is
the human-in-the-loop gate in CI (equivalent to the `APPROVE` prompt locally).

**Required repo config (Settings → Secrets and variables → Actions):**
- Variables: `AWS_ROLE_ARN`, `AWS_REGION`, `EKS_CLUSTER_NAME`, `CREWAI_LLM`
- Secrets: `OPENROUTER_API_KEY`

## Safety notes (read before running against a real cluster)

- **EKS upgrades are not reversible.** You cannot downgrade a control plane. Treat every run as one-way.
- **Test on a throwaway cluster first**, never a cluster you care about.
- The approval gate is deliberate friction. Do not automate away the `APPROVE` prompt — it is the whole safety model.

---

*This is a portfolio/demonstration project showing safe, human-approved automation of a high-risk operation. It reuses the CrewAI agent patterns and the approval-gate concept from the companion `devops-microservices-crewAi` and `End-End-Project-Automate` projects.*
