# Tests — safety logic (no AWS required)

These tests exercise the pure safety logic — the deterministic **guardrails**
and the hardened **approval gate** — with **no calls to AWS, kubectl, or
terraform**. They prove the gates block what they should and allow what they
should.

## Run

```bash
# from the repo root
pip install pytest
python -m pytest tests/ -v
```

`tests/conftest.py` puts `app/` on the path and gives each test its own
temporary approval store, so nothing touches real state.

## What's covered

**`test_guardrails.py`**
- version format validation
- single-minor-step rule (blocks downgrade, no-op, skip, major change, unparseable)
- typed cluster-name confirmation (blocks mismatch/empty)
- region allow-list (blocks disallowed region; empty list allows any)
- production two-person warning trigger
- pre-check verdict scan (blocks UNSAFE / NO-GO / no-compatible-addon)
- full `run_all_guardrails` report: clean pass, bad jump, wrong cluster

**`test_approval_gate.py`**
- single-approver approve → unlock
- version binding (approving 1.34 doesn't approve 1.35)
- evidence-hash binding: plan drift refuses approval, and a post-approval
  evidence change fails the apply check (drift detection)
- two-person: needs two **distinct** approvers; same person can't approve twice
- rejection blocks apply and blocks later approval
- TTL expiry (expires after window; 0 = never expires)
- account/region binding: refuses when account or region differs; backward
  compatible with older records that lack them
- reset clears current decision but keeps audit history

**`test_operations.py`** (additional gated cluster operations)
- generalized `(operation, target)` approval identity: scale/launch approvals
  are bound to their operation AND target, and don't leak across operations
  (a scale approval can't authorize a teardown; a launch approval can't
  authorize an upgrade; scale-to-5 can't authorize scale-to-9)
- upgrade behavior is unchanged when called with the old (no-operation) API
- teardown always needs two distinct approvers (even on a dev cluster), enforced
  by both `gates.required_approvers` and `preapply_gate`
- per-operation guardrails: launch blocks when the cluster already exists /
  passes when ABSENT; scale blocks scale-to-zero and absurd counts / passes a
  sane count / blocks when not ACTIVE; addon blocks a blank name; teardown
  blocks single confirmation, blocks a production cluster, passes a
  double-confirmed non-prod cluster
- gated executor tools (`operation_tools.py`) return `BLOCKED` without a valid
  matching approval, when only one approver signed a teardown, and when the
  approval on file is for a different operation

**`test_preapply_gate.py`** (apply-time two-person, C3)
- non-prod single approver allowed; prod with one approver blocked; prod with
  two distinct approvers allowed
- (skips if CrewAI isn't installed, since it imports `main`)

**`test_health_tools.py`** (zero-downtime health logic)
- baseline snapshot persists to disk
- regression compare: detects newly-unhealthy pods/workloads, ignores
  pre-existing breakage, warns when no baseline exists
- `wait_for_healthy` loop (30s/20min defaults): passes when healthy; times out
  (ALERT) on a NotReady node or bad pod; **fails if old pre-flight nodes are
  still present**; **fails on replica-count mismatch vs baseline**; passes when
  old nodes are gone and replicas match
- (monkeypatches `_collect_health` — no kubectl)

**`test_availability_tools.py`** (availability pre-checks)
- capacity headroom: PASS with ≥2 Ready nodes, ALERT on single/zero node
- vCPU lookup helper (known + unknown fallback)
- EC2 surge quota: PASS when headroom covers surge, ALERT (freeze risk) on
  shortfall, WARN when quota/node-groups unreadable
- PDB strength (preventive, both failure modes): PASS in the safe band, ALERT
  when too loose (over-eviction), ALERT when too strict (disruptionsAllowed=0 →
  drain stall/force-evict), ALERT on no PDBs / misconfigured (0 healthy),
  ignores kube-system, WARN on kubectl error
- (monkeypatches `_run_kubectl` / `_run_aws` — no aws/kubectl)

**`test_guardrails.py`** (added)
- surge-freeze evidence signals block via `gr_precheck_verdict`

**`test_availability_monitor.py`** (live rollover monitor)
- no breach when fully healthy or exactly at the floor
- breach when a critical deployment drops below the floor (>20%)
- ignores non-critical namespaces, single-replica workloads, and kube-system
- degrades gracefully with no baseline; `sound_alarm` never raises
- **halt_node_draining**: cordons only old baseline nodes still present, no-ops
  when old nodes already gone, never raises on kubectl error
- **monitor halts on breach** when `HALT_ON_AVAILABILITY_BREACH` enabled (once),
  and stays alarm-only when disabled
- (passes a baseline dict + monkeypatches `_collect_health` / `_kubectl` — no cluster)

**`test_sequence_gate.py`** (control-plane → nodes gate)
- passes only when AWS says ACTIVE **and** the API server responds within the
  latency threshold
- blocks when AWS not ACTIVE, when kubectl is slow, and when kubectl times out
- requires two **consecutive** good checks (a one-off good blip doesn't open it)
- (monkeypatches the AWS + API-server probe helpers — no aws/kubectl)

## Not covered here (needs a real cluster)

The actual `terraform` apply/plan and live `aws`/`kubectl` calls are not unit
tested — the tests fake their output. Exercise the real thing against a
**throwaway** cluster, never one you care about.
