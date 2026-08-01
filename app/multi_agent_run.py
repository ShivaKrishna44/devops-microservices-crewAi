"""
Multi-Agent DevOps Platform — Entry Point
==========================================
Runs the supervisor-based multi-agent system.

Architecture:
  Supervisor → decides which agent to call
  K8s Agent → pod/node/deployment health
  AWS Agent → EC2 + GitHub Actions
  Deploy Agent → rollout status, rollback
  Cost Agent → idle resources, EBS waste, spending
  Migration Agent → Jenkinsfile → GitHub Actions

Usage:
  python multi_agent_run.py                          # Full health check
  python multi_agent_run.py "check kubernetes"       # Specific query
  python multi_agent_run.py "find cost waste"        # Cost scan only
  python multi_agent_run.py "convert Jenkinsfile"    # Migration help
"""

import sys
import re

from langchain_core.messages import AIMessage, HumanMessage

from config import settings, logger
from graph import build_multi_agent


def sanitize(text: str) -> str:
    """Remove non-ASCII characters."""
    return re.sub(r"[^\x00-\x7F]", "", text).strip()


def extract_text(content) -> str:
    """Extract text from LangChain message content."""
    if content is None:
        return ""
    if isinstance(content, list):
        return "\n".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content)


def run_multi_agent(query: str) -> None:
    """Run the multi-agent system with a given query."""
    logger.info("=" * 60)
    logger.info("MULTI-AGENT SYSTEM STARTING")
    logger.info("Query: %s", query)
    logger.info("=" * 60)

    agent = build_multi_agent()

    inputs = {
        "messages": [HumanMessage(content=query)],
        "next_agent": "",
        "results": {},
    }

    # Stream outputs to see each agent's work
    for output in agent.stream(inputs, config={"callbacks": []}, stream_mode="values"):
        # Show sub-agent routing decisions
        if output.get("next_agent"):
            logger.info("→ Supervisor routing to: %s", output["next_agent"])

        # Show results as they come in
        if output.get("results"):
            for agent_name, result in output["results"].items():
                if result and len(result) > 5:
                    logger.info("[%s] %s", agent_name.upper(), result[:200])

        # Show final messages
        last_msg = output.get("messages", [None])[-1] if output.get("messages") else None
        if isinstance(last_msg, AIMessage) and last_msg.content:
            clean = sanitize(extract_text(last_msg.content))
            if len(clean) > 10 and "===" in clean:
                print("\n" + clean)


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
        run_multi_agent(query)
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
