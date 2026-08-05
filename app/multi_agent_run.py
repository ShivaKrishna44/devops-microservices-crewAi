"""
Multi-Agent DevOps Platform — CrewAI Entry Point
==================================================
Runs the CrewAI hierarchical multi-agent system.

Architecture (CrewAI):
  Manager Agent (auto) → delegates to specialized agents:
    - K8s Agent → pod/node/deployment health
    - AWS Agent → EC2 + GitHub Actions
    - Deploy Agent → rollout status, rollback
    - Cost Agent → idle resources, EBS waste, spending
    - Incident Agent → crash logs, events, diagnosis
    - Migration Agent → Jenkinsfile → GitHub Actions

Usage:
  python multi_agent_run.py                          # Full health check
  python multi_agent_run.py "check kubernetes"       # Specific query
  python multi_agent_run.py "find cost waste"        # Cost scan only
  python multi_agent_run.py "convert Jenkinsfile"    # Migration help
"""

import sys

from config import settings, logger
from crew import run_devops_crew


def main() -> int:
    # Get query from command line or use default
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
    else:
        query = (
            f"Perform a full infrastructure health check for '{settings.TARGET_REPO}'. "
            f"Check: AWS EC2 health, GitHub Actions, Kubernetes pods/nodes, "
            f"and find any cost waste (idle instances, unattached volumes)."
        )

    try:
        result = run_devops_crew(query)
        print("\n" + result)
    except KeyboardInterrupt:
        logger.info("Interrupted by user.")
        return 130
    except Exception as exc:
        logger.error("Multi-agent run failed: %s", exc, exc_info=True)
        return 1

    logger.info("Multi-agent run complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
