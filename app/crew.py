"""
CrewAI DevOps Multi-Agent Platform
====================================
Replaces LangGraph supervisor pattern with CrewAI's hierarchical process.

Architecture:
  Manager Agent (auto) → delegates to specialized agents:
    - K8s Agent: pod/node/deployment health
    - AWS Agent: EC2 + GitHub Actions
    - Deploy Agent: rollout status, rollback
    - Cost Agent: idle resources, EBS waste, spending
    - Incident Agent: crash logs, events, diagnosis
    - Migration Agent: Jenkinsfile → GitHub Actions

Usage:
    from crew import run_devops_crew
    result = run_devops_crew("check kubernetes pods in all namespaces")
"""

from crewai import Agent, Task, Crew, Process, LLM

# Apply Groq compatibility patch BEFORE any CrewAI LLM calls
from litellm_patch import apply_patch
apply_patch()

from config import settings, logger
from tools.aws_tools import check_aws_ec2_health
from tools.github_tools import check_github_workflow_status
from tools.k8s_tools import check_k8s_pod_health, check_k8s_node_health, check_k8s_deployments
from tools.deploy_tools import check_rollout_status, rollback_deployment, restart_deployment, get_deployment_history
from tools.incident_tools import get_pod_crash_logs, get_cluster_events, diagnose_pod
from tools.cost_tools import find_idle_ec2_instances, find_unattached_ebs_volumes, get_cost_summary
from tools.migration_tools import convert_jenkinsfile_to_github_actions, analyze_jenkinsfile_complexity


# ═══════════════════════════════════════════════════════
# Agent Definitions
# ═══════════════════════════════════════════════════════

LLM_MODEL = LLM(
    model=settings.CREWAI_LLM,
    temperature=0,
)


k8s_agent = Agent(
    role="Kubernetes Specialist",
    goal="Monitor and diagnose Kubernetes cluster health including pods, nodes, and deployments",
    backstory=(
        "You are a senior Kubernetes engineer with deep expertise in cluster operations. "
        "You check pod health across namespaces, verify node readiness, and ensure deployments "
        "have correct replica counts. You quickly identify CrashLoopBackOff, ImagePullBackOff, "
        "and scheduling issues."
    ),
    tools=[check_k8s_pod_health, check_k8s_node_health, check_k8s_deployments,
           get_pod_crash_logs, get_cluster_events, diagnose_pod],
    llm=LLM_MODEL,
    verbose=True,
    allow_delegation=False,
)

aws_agent = Agent(
    role="AWS Infrastructure Specialist",
    goal="Monitor AWS EC2 health and GitHub Actions CI/CD pipeline status",
    backstory=(
        "You are an AWS Solutions Architect focused on infrastructure health monitoring. "
        "You check EC2 instance states, verify GitHub Actions workflows are passing, "
        "and identify any infrastructure issues that could affect service availability."
    ),
    tools=[check_aws_ec2_health, check_github_workflow_status],
    llm=LLM_MODEL,
    verbose=True,
    allow_delegation=False,
)

deploy_agent = Agent(
    role="Deployment Specialist",
    goal="Monitor deployment rollouts, perform rollbacks, and ensure zero-downtime deployments",
    backstory=(
        "You are a deployment engineer who ensures all rollouts complete successfully. "
        "You check rollout status, identify stuck deployments, perform rollbacks when needed, "
        "and can trigger rolling restarts. You prioritize service stability."
    ),
    tools=[check_rollout_status, rollback_deployment, restart_deployment, get_deployment_history],
    llm=LLM_MODEL,
    verbose=True,
    allow_delegation=False,
)

cost_agent = Agent(
    role="Cost Optimization Specialist",
    goal="Find wasted cloud spend: idle EC2 instances, unattached EBS volumes, and track monthly costs",
    backstory=(
        "You are a FinOps engineer focused on reducing AWS costs. "
        "You identify idle instances (< 5% CPU), orphaned EBS volumes wasting money, "
        "and track monthly spending by service. You provide actionable cost-saving recommendations."
    ),
    tools=[find_idle_ec2_instances, find_unattached_ebs_volumes, get_cost_summary],
    llm=LLM_MODEL,
    verbose=True,
    allow_delegation=False,
)

incident_agent = Agent(
    role="Incident Response Specialist",
    goal="Diagnose and triage production incidents by analyzing crash logs, events, and pod state",
    backstory=(
        "You are an SRE who investigates production incidents. You pull crash logs from "
        "failing pods, analyze cluster warning events, and perform root cause analysis. "
        "You understand exit codes (137=OOM, 1=app error, 127=bad entrypoint) and can "
        "recommend fixes."
    ),
    tools=[get_pod_crash_logs, get_cluster_events, diagnose_pod],
    llm=LLM_MODEL,
    verbose=True,
    allow_delegation=False,
)

migration_agent = Agent(
    role="CI/CD Migration Specialist",
    goal="Analyze Jenkinsfiles and convert them to GitHub Actions workflows",
    backstory=(
        "You are a CI/CD platform engineer who migrates teams from Jenkins to GitHub Actions. "
        "You assess Jenkinsfile complexity (Tier 1-3), generate equivalent GitHub Actions YAML, "
        "and recommend migration strategies including OIDC, reusable workflows, and security scanning."
    ),
    tools=[convert_jenkinsfile_to_github_actions, analyze_jenkinsfile_complexity],
    llm=LLM_MODEL,
    verbose=True,
    allow_delegation=False,
)


# ═══════════════════════════════════════════════════════
# Task Definitions
# ═══════════════════════════════════════════════════════

def create_health_check_tasks(repo: str) -> list[Task]:
    """Create a full health check task set for all agents."""

    k8s_task = Task(
        description=(
            f"Check Kubernetes cluster health:\n"
            f"1. Check pod health in namespaces: default, order-service, payment-service, user-service\n"
            f"2. Check node health (any NotReady nodes?)\n"
            f"3. Check deployments in all namespaces\n"
            f"Report each check as PASS or FAIL with details."
        ),
        expected_output="A report listing pod status, node status, and deployment status with PASS/FAIL for each namespace.",
        agent=k8s_agent,
    )

    aws_task = Task(
        description=(
            f"Check AWS and CI/CD health:\n"
            f"1. Check EC2 instance health (are all instances running?)\n"
            f"2. Check GitHub Actions workflow status for repo '{repo}'\n"
            f"Report each check as PASS or FAIL."
        ),
        expected_output="A report showing EC2 health status and GitHub Actions pipeline status with PASS/FAIL.",
        agent=aws_agent,
    )

    cost_task = Task(
        description=(
            f"Perform cost optimization scan:\n"
            f"1. Find idle EC2 instances (< 5% CPU over 24h)\n"
            f"2. Find unattached EBS volumes (wasting money)\n"
            f"3. Get current month AWS spend breakdown\n"
            f"Report findings with estimated waste in dollars."
        ),
        expected_output="A cost report listing idle resources, unattached volumes, monthly spend, and savings recommendations.",
        agent=cost_agent,
    )

    summary_task = Task(
        description=(
            f"Compile a final infrastructure health report combining all findings from the team.\n"
            f"Include:\n"
            f"- Overall status: HEALTHY / WARNING / CRITICAL\n"
            f"- K8s cluster health summary\n"
            f"- AWS/EC2 health summary\n"
            f"- GitHub Actions CI/CD status\n"
            f"- Cost optimization findings\n"
            f"- Recommended actions (if any issues found)\n"
            f"Format as a clear, concise report."
        ),
        expected_output=(
            "A final summary report with overall health status, per-component findings, "
            "and recommended actions. Format: plain text with clear sections."
        ),
        agent=aws_agent,  # Manager will override this in hierarchical mode
    )

    return [k8s_task, aws_task, cost_task, summary_task]


def create_custom_task(query: str) -> list[Task]:
    """Create a single task from a custom user query — manager will delegate."""

    task = Task(
        description=query,
        expected_output="A clear, concise answer addressing the user's request with actionable details.",
        agent=k8s_agent,  # Default agent; manager overrides in hierarchical mode
    )
    return [task]


# ═══════════════════════════════════════════════════════
# Crew Builder
# ═══════════════════════════════════════════════════════

def build_devops_crew(tasks: list[Task]) -> Crew:
    """Build the CrewAI crew with hierarchical process (manager auto-delegates)."""
    return Crew(
        agents=[k8s_agent, aws_agent, deploy_agent, cost_agent, incident_agent, migration_agent],
        tasks=tasks,
        process=Process.hierarchical,
        manager_llm=LLM_MODEL,
        verbose=True,
        memory=False,
        tracing=True,
    )


def run_devops_crew(query: str = "") -> str:
    """
    Main entry point — run the DevOps crew.
    If no query, performs a full health check.
    If query provided, creates a targeted task.
    """
    repo = settings.TARGET_REPO

    if not query or "health check" in query.lower() or "full" in query.lower():
        logger.info("Running full infrastructure health check...")
        tasks = create_health_check_tasks(repo)
    else:
        logger.info("Running custom query: %s", query)
        tasks = create_custom_task(query)

    crew = build_devops_crew(tasks)

    logger.info("=" * 60)
    logger.info("CREWAI DEVOPS PLATFORM — STARTING")
    logger.info("Agents: %d | Tasks: %d | Process: hierarchical", len(crew.agents), len(crew.tasks))
    logger.info("=" * 60)

    result = crew.kickoff()

    logger.info("=" * 60)
    logger.info("CREWAI RUN COMPLETE")
    logger.info("=" * 60)

    return str(result)
