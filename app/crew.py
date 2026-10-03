"""
CrewAI agents for the human-approved EKS upgrade workflow.

Four agents, three of them strictly read-only:
  1. Pre-Check Agent      — validates the jump, scans deprecated APIs, addons, nodes
  2. Upgrade Planner      — terraform plan + GO/NO-GO recommendation
  3. Executor Agent       — terraform apply, GATED on human approval
  4. Post-Upgrade Validator — verifies version + node/pod health

The human approval step happens OUTSIDE the crew (see main.py). The Executor
agent's apply tool refuses to run without a recorded approval, so even if the
LLM "decided" to apply, the gate blocks it.
"""
from crewai import Agent, Task, Crew, Process, LLM

from config import settings, logger
from tools.eks_tools import (
    get_current_eks_version,
    check_cluster_upgradeable,
    validate_upgrade_target,
    scan_deprecated_apis,
    check_addon_compatibility,
    check_node_readiness,
    check_pdb_coverage,
    check_pdb_strength,
    check_capacity_headroom,
    check_ec2_surge_quota,
)
from tools.upgrade_tools import (
    terraform_init,
    terraform_plan_upgrade,
    terraform_apply_upgrade,
    verify_eks_version,
)
from tools.health_tools import (
    snapshot_cluster_health,
    wait_for_healthy,
    compare_to_baseline,
)


LLM_MODEL = LLM(model=settings.CREWAI_LLM, temperature=0)


# ─── Agents ─────────────────────────────────────────────────────────────

precheck_agent = Agent(
    role="EKS Pre-Upgrade Analyst",
    goal="Determine whether a proposed EKS version upgrade is safe to attempt, using only read-only checks",
    backstory=(
        "You are a senior Kubernetes platform engineer. Before any EKS upgrade you rigorously "
        "verify the jump is a single valid minor step, scan for deprecated/removed APIs, confirm "
        "every managed addon has a compatible version, and ensure all nodes are Ready. You never "
        "change anything — you gather evidence and state clearly whether each check PASSED or raised an ALERT."
    ),
    tools=[
        get_current_eks_version,
        check_cluster_upgradeable,
        validate_upgrade_target,
        scan_deprecated_apis,
        check_addon_compatibility,
        check_node_readiness,
        check_pdb_coverage,
        check_pdb_strength,
        check_capacity_headroom,
        check_ec2_surge_quota,
        snapshot_cluster_health,
    ],
    llm=LLM_MODEL,
    verbose=True,
    allow_delegation=False,
)

planner_agent = Agent(
    role="EKS Upgrade Planner",
    goal="Produce the terraform plan for the upgrade and a clear GO / NO-GO recommendation for the human approver",
    backstory=(
        "You translate a validated upgrade into an exact change plan. You run terraform init and "
        "terraform plan for the target version, summarize precisely what will change (control plane, "
        "node groups), and give a GO or NO-GO recommendation grounded in the pre-check evidence. "
        "You never apply — you only plan and recommend. The human decides."
    ),
    tools=[terraform_init, terraform_plan_upgrade],
    llm=LLM_MODEL,
    verbose=True,
    allow_delegation=False,
)

executor_agent = Agent(
    role="EKS Upgrade Executor",
    goal="Apply the approved EKS upgrade in the correct order — but only when a human approval is on record",
    backstory=(
        "You perform the actual upgrade via terraform apply. You understand that this is irreversible "
        "and that your apply tool is approval-gated: it will refuse to run unless a human has approved "
        "the exact target version. You never attempt to bypass the gate. Control plane upgrades first, "
        "then managed node groups (the EKS module handles this ordering)."
    ),
    tools=[terraform_apply_upgrade],
    llm=LLM_MODEL,
    verbose=True,
    allow_delegation=False,
)

validator_agent = Agent(
    role="EKS Post-Upgrade Validator",
    goal="Confirm zero-downtime: correct version, poll until healthy, and no regressions vs baseline",
    backstory=(
        "After an upgrade you verify the cluster reports the target version, then run a "
        "validation LOOP that polls until nodes are Ready and pods are healthy, and finally "
        "compare against the pre-upgrade baseline to catch anything that was healthy before but "
        "broke during the upgrade (a regression). You report PASS or FAIL with specifics. Read-only."
    ),
    tools=[verify_eks_version, wait_for_healthy, compare_to_baseline, check_node_readiness],
    llm=LLM_MODEL,
    verbose=True,
    allow_delegation=False,
)


# ─── Tasks ──────────────────────────────────────────────────────────────

def create_precheck_task(target_version: str) -> Task:
    return Task(
        description=(
            f"Assess whether upgrading the EKS cluster to version '{target_version}' is safe "
            f"AND can be done with zero downtime.\n"
            f"Steps:\n"
            f"0. Check the cluster exists and is ACTIVE (not already mid-update). If not, STOP and report UNSAFE.\n"
            f"1. Get the current EKS version.\n"
            f"2. Validate that '{target_version}' is a valid single-minor upgrade from current.\n"
            f"3. Scan for deprecated/removed Kubernetes APIs.\n"
            f"4. Check addon compatibility with '{target_version}'.\n"
            f"5. Check that all nodes are Ready.\n"
            f"6. Check PodDisruptionBudget coverage (every multi-replica workload has a PDB).\n"
            f"6b. Check PodDisruptionBudget STRENGTH — critical PDBs must be strict enough that a "
            f"node drain can't drop a deployment below the availability threshold (preventive).\n"
            f"7. Check cluster capacity headroom (need >=2 Ready nodes so pods reschedule).\n"
            f"8. Check EC2 surge quota — is there enough On-Demand vCPU quota headroom to launch "
            f"the surge nodes during the rollover? If not, the node upgrade will FREEZE mid-way.\n"
            f"9. Capture a health baseline (snapshot) so regressions can be detected later.\n"
            f"If step 2 fails (invalid jump), STOP and report NO-GO.\n"
            f"If step 8 ALERTs (insufficient quota), report UNSAFE — do not proceed until the "
            f"EC2 vCPU quota is raised.\n"
            f"Report each check as PASS or ALERT, then an overall SAFE / UNSAFE verdict."
        ),
        expected_output=(
            "A pre-check report: version validity, deprecated APIs, addon compatibility, node "
            "readiness, PDB coverage, capacity headroom, EC2 surge quota, baseline captured — "
            "each PASS/ALERT — ending with SAFE or UNSAFE."
        ),
        agent=precheck_agent,
    )


def create_plan_task(target_version: str) -> Task:
    return Task(
        description=(
            f"Produce the upgrade plan for target '{target_version}'.\n"
            f"1. Run terraform init.\n"
            f"2. Run terraform plan for eks_version={target_version}.\n"
            f"3. Summarize what will change (control plane version, node groups).\n"
            f"4. Give a GO or NO-GO recommendation, referencing the pre-check evidence.\n"
            f"Do NOT apply anything."
        ),
        expected_output=(
            "The terraform plan summary plus a GO/NO-GO recommendation for the human approver."
        ),
        agent=planner_agent,
    )


def create_execute_task(target_version: str) -> Task:
    return Task(
        description=(
            f"Apply the EKS upgrade to '{target_version}' using the approval-gated apply tool.\n"
            f"The tool will refuse if no human approval is on record for this exact version — "
            f"that is expected and correct. Report the result."
        ),
        expected_output="The apply result: SUCCESS with details, or BLOCKED/FAILED with the reason.",
        agent=executor_agent,
    )


def create_validate_task(target_version: str) -> Task:
    return Task(
        description=(
            f"Validate the cluster after upgrading to '{target_version}' — confirm zero downtime.\n"
            f"1. Verify the control-plane version is now '{target_version}'.\n"
            f"2. Run the validation LOOP (polls every 30s for up to 20 minutes). It passes only "
            f"when ALL hold: every node Ready, every OLD pre-flight node fully terminated, no "
            f"bad pods, and deployment replica counts MATCH the pre-flight baseline.\n"
            f"3. Run the regression check: compare current health to the pre-upgrade baseline "
            f"and flag anything that was healthy before but is broken now.\n"
            f"Report PASS only if the version is correct, the validation loop passed, AND there "
            f"are no regressions. Otherwise FAIL with specifics."
        ),
        expected_output=(
            "A post-upgrade validation report: version check, health-loop result, and "
            "regression-vs-baseline result — ending in PASS or FAIL."
        ),
        agent=validator_agent,
    )


# ─── Crew builders (sequential — order matters for an upgrade) ────────────

def run_precheck(target_version: str) -> str:
    """Phase 1+2: pre-checks and plan. Read-only. Returns evidence for the human."""
    crew = Crew(
        agents=[precheck_agent, planner_agent],
        tasks=[create_precheck_task(target_version), create_plan_task(target_version)],
        process=Process.sequential,
        verbose=True,
    )
    logger.info("Running pre-check + plan for target %s", target_version)
    return str(crew.kickoff())


def run_execute_and_validate(target_version: str) -> str:
    """Phase 3+4: apply (gated) then validate. Only call AFTER human approval."""
    crew = Crew(
        agents=[executor_agent, validator_agent],
        tasks=[create_execute_task(target_version), create_validate_task(target_version)],
        process=Process.sequential,
        verbose=True,
    )
    logger.info("Running execute + validate for target %s", target_version)
    return str(crew.kickoff())
