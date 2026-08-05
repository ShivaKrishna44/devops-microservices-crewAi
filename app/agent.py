"""
DevOps AI Agent — CrewAI Entry Point
======================================
Runs the CrewAI multi-agent system for infrastructure health checks.

Usage:
    python agent.py                              # Full health check
    python agent.py "check kubernetes pods"      # Custom query
    python agent.py "find cost waste"            # Cost scan
    python agent.py "convert Jenkinsfile"        # Migration help
"""

import sys

from config import settings, logger
from crew import run_devops_crew


def main() -> int:
    logger.info("DevOps CrewAI Agent starting...")
    logger.info("Target repo: %s", settings.TARGET_REPO)
    logger.info("LLM: %s", settings.CREWAI_LLM)

    # Get query from command line or use default (full health check)
    if len(sys.argv) > 1:
        query = " ".join(sys.argv[1:])
    else:
        query = ""  # Empty = full health check

    try:
        result = run_devops_crew(query)
        print("\n" + "=" * 60)
        print("FINAL REPORT")
        print("=" * 60)
        print(result)
    except KeyboardInterrupt:
        logger.info("Agent interrupted by user.")
        return 130
    except Exception as exc:
        logger.error("Agent run failed: %s", exc, exc_info=True)
        return 1

    logger.info("Health check complete. Exiting.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
