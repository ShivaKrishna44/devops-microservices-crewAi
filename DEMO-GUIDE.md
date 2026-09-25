# Demo Guide — Human-Approved EKS Upgrade Agent
# guarded-eks-upgrade-agent — Demo Guide

## What is this project?

An AI multi-agent system that upgrades an Amazon EKS cluster **one minor version
at a time**, driven by a version a human provides, and executed **only after
explicit human approval**. The interesting engineering is not "AI upgrades the
cluster" — it's the **guardrails and approval gates** that make a high-risk,
irreversible operation safe to automate.

Built with **CrewAI**. Four agents, three of them strictly read-only.

---

## The Big Picture (Simple)

```
YOU: "upgrade the cluster to 1.34"
  │
  ▼
PRE-CHECK AGENT   → is this a valid single-minor jump? deprecated APIs?
                    addons compatible? nodes Ready?      (read-only)
  │
  ▼
UPGRADE PLANNER   → terraform plan → "here's exactly what changes" + GO/NO-GO
  │                                                        (read-only)
  ▼
GUARDRAILS        → deterministic, non-LLM. Block downgrade/skip/wrong-cluster.
  │
  ▼
APPROVAL GATE     → YOU type the cluster name + APPROVE.
  │                 (production needs TWO people)
  ▼ (only if APPROVED)
EXECUTOR AGENT    → terraform apply (control plane → nodes).
  │                 Refuses to run without a valid approval.
  ▼
VALIDATOR AGENT   → new version live? nodes Ready? → PASS/FAIL   (read-only)
```

---

## One-Page Architecture — the full layered safety model

```
                          HUMAN provides target version (e.g. 1.34)
                                        │
╔═══════════════════════════════ PRE-FLIGHT (read-only) ═══════════════════════════════╗
║  Pre-Check Agent + Planner Agent — gather evidence, change nothing                     ║
║                                                                                        ║
║   validity          availability (zero-downtime readiness)         plan               ║
║   ─────────         ──────────────────────────────────────         ────               ║
║   • version format  • node readiness (all Ready)                    • terraform plan   ║
║   • single-minor    • PDB coverage (every multi-replica has a PDB)  • GO / NO-GO       ║
║   • deprecated APIs  • PDB STRENGTH (prevention: drain can't breach                     ║
║   • addon compat        the availability threshold)                                    ║
║                     • capacity headroom (>=2 Ready nodes)                               ║
║                     • EC2 surge quota (won't freeze mid-rollover)                       ║
║                     • BASELINE snapshot (nodes, ready pods, replicas)                   ║
╚════════════════════════════════════════┬═══════════════════════════════════════════════╝
                                          ▼
                     ┌────────── DETERMINISTIC GUARDRAILS (non-LLM) ──────────┐
                     │  block on: bad jump · wrong cluster · bad region ·      │
                     │  UNSAFE verdict · surge freeze · PDB too loose          │
                     └────────────────────────┬───────────────────────────────┘
                                          ▼
                     ╔══════════════ HUMAN APPROVAL GATE ══════════════╗
                     ║  type cluster name + APPROVE (agents can't       ║
                     ║  self-approve) · two-person for prod ·           ║
                     ║  evidence-hash bound · TTL expiry                ║
                     ╚════════════════════════┬═════════════════════════╝
                                          ▼ (only if APPROVED)
╔══════════════════════════════ EXECUTION (gated, phased) ═══════════════════════════════╗
║  Executor re-verifies the approval record, then:                                        ║
║                                                                                         ║
║   PHASE 1  terraform apply -target=<control plane>   (AWS handles, zero downtime)       ║
║      │                                                                                  ║
║      ▼  SEQUENCE GATE (block + loop until BOTH, stable x2):                             ║
║           • AWS API: control plane ACTIVE on target version                             ║
║           • K8s API: `kubectl get nodes` responds within latency threshold              ║
║      │                                                                                  ║
║      ▼                                                                                  ║
║   PHASE 2  terraform apply <node groups>  ──────────────┐                               ║
║            (rolling surge + drain)                       │ runs concurrently             ║
║                                                          ▼                               ║
║            LIVE AVAILABILITY MONITOR (detection) — polls every 15s;                     ║
║            if a critical deployment drops >threshold of baseline healthy pods:          ║
║            → SOUND THE ALARM immediately (log + webhook)                                 ║
║            → HALT further draining: cordon remaining old nodes so K8s refuses            ║
║              further eviction; human debugs, uncordon + re-apply to resume              ║
╚════════════════════════════════════════┬═══════════════════════════════════════════════╝
                                          ▼
╔══════════════════════════ POST-UPGRADE VALIDATION (read-only) ═════════════════════════╗
║  Validator Agent — validation LOOP: poll every 30s for up to 20 min until ALL hold:     ║
║    • every node Ready                                                                   ║
║    • every OLD (pre-flight) node fully terminated                                       ║
║    • no bad pods                                                                        ║
║    • deployment replica counts MATCH the pre-flight baseline                            ║
║  then REGRESSION check: nothing healthy-before is broken-now                            ║
╚════════════════════════════════════════┬═══════════════════════════════════════════════╝
                                          ▼
                          AUDIT TRAIL — every decision persisted
                       (request, approvals, verdict, actor, time, reason)

  Legend:  prevention = stops the bad thing happening   ·   detection = catches it live
           ══ gate/blocking layer      ── check/step
```

**Two-control availability model:** `PDB STRENGTH` (pre-flight, prevention) makes Kubernetes
*refuse* a drain that would breach the floor; the `LIVE MONITOR` (during rollover, detection)
sounds the alarm if healthy pods drop anyway. Prevention is the real control; detection is the net.

---

## Why this is interesting (the elevator pitch)

> "Everyone demos an agent that *can* do an operation. The hard part is making
> sure it can't do the wrong one. EKS upgrades are irreversible — you can't
> downgrade a control plane. So I built layered guardrails and a human approval
> gate: the agent does the tedious, error-prone checking; a human owns the one
> dangerous action."

---

## The four agents

| Agent | Job | Can change anything? |
|-------|-----|----------------------|
| **Pre-Check** | validate the jump, scan deprecated APIs, addon + node readiness | No — read-only |
| **Planner** | `terraform plan`, summarize the diff, GO/NO-GO | No — plan only |
| **Executor** | `terraform apply` (control plane → node groups) | **Yes — but only with an APPROVED record** |
| **Validator** | confirm version + node/pod health | No — read-only |

---

## How to Demo (Step by Step explain)   

### Demo 0: Prove the safety logic works (no AWS needed) — 30 seconds

```bash
python -m pytest tests/ -v
```
**What to show:** ~30 tests pass — single-minor rule blocks downgrades/skips,
wrong cluster name blocks, evidence-hash drift blocks, two-person enforced, TTL
expiry works. This is runnable proof the gates aren't just decoration.

### Demo 1: A blocked upgrade (guardrail in action) — the best demo

```bash
cd app
python main.py --target-version 1.36    # cluster is on 1.33 → skips minors
```
**What to show:** the guardrail report prints `[BLOCK] single_minor_step` and the
run stops **before** any approval or apply. The agent refuses an unsafe jump.

Also try typing the **wrong cluster name** at the confirmation prompt — the
`cluster_name_confirmation` guardrail blocks it.

### Demo 2: A valid upgrade with approval — the happy path

```bash
python main.py --target-version 1.34
# 1. Pre-checks + terraform plan print (read-only)
# 2. Type the exact cluster name to confirm
# 3. Guardrails run and CLEAR
# 4. Type APPROVE
python main.py --apply --target-version 1.34
# 5. Executor re-verifies approval, applies, validator prints PASS/FAIL
```
**What to show:** nothing is applied until you type `APPROVE`; the executor
re-checks the approval record before touching anything.

### Demo 3: Two-person approval (production) — the governance story

```bash
python main.py --target-version 1.34 --actor alice           # opens request + 1st approval
python main.py --target-version 1.34 --actor bob --approve   # distinct 2nd approver
python main.py --status                                      # shows 2/2 APPROVED
python main.py --apply --target-version 1.34
```
**What to show:** one person can't approve twice; prod requires two distinct
approvers; every decision is logged with actor, timestamp, and reason.

### Demo 4: Drift detection — the subtle, impressive one

Open the request as alice, then change something on the cluster (or wait for the
plan to differ), then have bob approve. **What to show:** bob's approval is
refused with `evidence/plan changed since approval` — the approval is bound to a
hash of the exact plan reviewed. Approvals can't be reused on a changed plan.

### Demo 5: CI/CD gate (GitHub Actions)

Actions → **EKS Upgrade (Agent + Human Approval)** → Run workflow → enter version.
**What to show:** Job 1 runs pre-checks + plan; Job 2 **pauses** on the
`production-eks-upgrade` environment until a required reviewer approves in the
GitHub UI. Same human-in-the-loop model, enforced by GitHub.

### Demo 6: Other gated operations (launch / scale / addon / teardown)

The same gate now guards more than upgrades. Every one still requires the typed
cluster name + typed `APPROVE`; there is no auto-approve path.

```bash
# Scale the node group to 4 — single approver on a dev cluster
python main.py --operation scale --nodes 4                       # opens request + guardrails + approve
python main.py --operation scale --nodes 4 --apply               # gated apply

# Update a specific addon (must be named — no blanket change)
python main.py --operation addon --addon vpc-cni
python main.py --operation addon --addon vpc-cni --apply

# Launch a new cluster (guardrail refuses if a cluster already exists)
python main.py --operation launch --target-version 1.34
python main.py --operation launch --target-version 1.34 --apply
```

**What to show:**
- `--operation scale --nodes 0` is **blocked** by `node_count_sane` (no scale-to-zero).
- `--operation launch` while the cluster already exists is **blocked** by
  `cluster_absent_for_launch` — you can't clobber a live cluster.
- A `scale` approval can't be used to run a `launch` (or vice versa) — the
  approval is bound to the `(operation, target)` pair.

### Demo 7: Teardown — gated hardest (the "would you really let an agent do this?" demo)

```bash
# Teardown ALWAYS needs two distinct approvers, even on a dev cluster:
python main.py --operation teardown --actor alice          # opens request, types cluster name TWICE, 1st approval
python main.py --operation teardown --actor bob --approve  # distinct 2nd approver
python main.py --status                                    # shows 2/2 APPROVED for teardown
python main.py --operation teardown --apply                # gated terraform destroy
```

**What to show:**
- The cluster name must be typed **twice** at the prompt (`teardown_double_confirm`).
- **Two distinct approvers** are required regardless of prod/dev — one person
  can't do it alone, and the executor tool re-checks this independently.
- Pointing teardown at a **production-looking** cluster (name contains `prod`)
  is **refused outright** by `teardown_not_prod` — a hard stop, not a warning.
- This is the clearest example of the thesis: the more dangerous the operation,
  the more the safety layer does — never less.

---

## The guardrails (talking points)

| Layer | What it stops |
|-------|---------------|
| Read-only agents | 3 of 4 agents have no tools that can change anything |
| `single_minor_step` | downgrade / no-op / skip / major version change |
| `cluster_name_confirmation` | upgrading the wrong cluster |
| `region_allowlist` | wrong region / account |
| `precheck_verdict` | proceeding when pre-checks said UNSAFE |
| Human gate | unattended or self-approved applies (agents can't approve) |
| Two-person (prod) | a single person pushing a risky prod upgrade |
| Evidence-hash binding | applying a plan that drifted since it was approved |
| TTL expiry | reusing a stale approval |
| Gated executor | any apply without a valid approval record |
| `(operation, target)` binding | using a scale/launch approval to run a different op or a different target |
| `cluster_absent_for_launch` | launching over a cluster that already exists |
| `node_count_sane` | scale-to-zero or an absurd node count |
| Teardown two-person (always) | one person destroying a cluster alone, even on dev |
| `teardown_double_confirm` | a destroy from a single mistyped/auto-filled prompt |
| `teardown_not_prod` | tearing down a production-looking cluster at all |

---

## Key Points for Interview

1. **Why is the approval gate the whole point?**
   EKS control-plane upgrades are irreversible. There's no undo. So the safety
   layer isn't optional polish — it's the product. The agent removes toil
   (checking APIs, addons, nodes, drafting the plan); the human owns the
   irreversible decision.

2. **Why deterministic guardrails AND an LLM agent?**
   The guardrails are plain Python — no LLM in the path — so they can't be
   "talked out of" a no by a clever prompt or a hallucination. The LLM does
   judgement work; the guardrails enforce hard rules.

3. **How do you stop the agent applying something it shouldn't?**
   The executor's apply tool checks the persisted approval record (right
   version, APPROVED, enough distinct approvers, evidence hash matches, not
   expired) and refuses otherwise. Even if the LLM "decided" to apply, the gate
   blocks it. It validates the record the *human* created, never agent text.

4. **What's the audit story?**
   Every event — request, each approval, rejection, reset — is persisted with
   actor, timestamp, reason, and the evidence hash. The whole decision chain is
   reconstructable.

5. **How would you productionize it?**
   Run in CI with OIDC (no static keys), the `production-eks-upgrade` environment
   as the human gate, least-privilege IAM (EKS + backend only), and always
   upgrade non-prod first.

---

## Files That Matter (Quick Reference)

```
app/
├── main.py            ← human entrypoint: input → precheck → guardrails → approve → apply
├── crew.py            ← the 4 CrewAI agents + tasks
├── guardrails.py      ← deterministic non-LLM safety checks
├── approval_gate.py   ← records evidence + APPROVE/REJECT, gates the executor
├── config.py          ← settings + guardrail/approval policy
└── tools/
    ├── eks_tools.py       ← read-only pre-checks
    └── upgrade_tools.py   ← terraform plan/apply (apply is GATED)
tests/                 ← runnable proof of the safety logic (no AWS)
.github/workflows/eks-upgrade.yml   ← CI with environment approval gate
docs/DEPLOYMENT-GUIDE.md            ← full setup & operations
```

---

## Setup (One-Time)

```bash
cd app
python -m venv venv
pip install -r requirements.txt
cp .env.example .env      # set CREWAI_LLM + key, EKS_CLUSTER_NAME, AWS_REGION
# point TERRAFORM_DIR at the Terraform that manages your cluster (has eks_version)
aws eks update-kubeconfig --name <CLUSTER_NAME> --region <AWS_REGION>
```

Then run `python main.py --status` (safe) to confirm it's wired up.

> Reminder: EKS upgrades are irreversible. Always demo/test against a throwaway
> cluster, never one you care about.
