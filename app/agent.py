import re
import sys

from langchain_core.messages import AIMessage

from config import settings, logger
from graph import build_agent


def extract_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, list):
        return "\n".join(
            item.get("text", "") if isinstance(item, dict) else str(item)
            for item in content
        )
    return str(content)


def sanitize(text: str) -> str:
    return re.sub(r"[^\x00-\x7F]", "", text).strip()


def run_health_check(agent, repo: str) -> None:
    query = (
        f"Perform a full infrastructure health check. Do ALL of the following:\n"
        f"1. Check GitHub Actions workflow status for '{repo}'\n"
        f"2. Check AWS EC2 instance health\n"
        f"3. Check Kubernetes pod health in all namespaces (try: default, order-service, payment-service, user-service)\n"
        f"4. Check Kubernetes node health\n"
        f"5. Check for idle EC2 instances wasting money\n"
        f"6. Check for unattached EBS volumes\n"
        f"Respond in short plain-text sentences. Report any issues found."
    )
    inputs = {"messages": [("user", query)]}

    for output in agent.stream(inputs, config={"callbacks": []}, stream_mode="values"):
        last_msg = output["messages"][-1]

        if isinstance(last_msg, AIMessage) and last_msg.tool_calls:
            logger.debug("Agent dispatching tool calls...")
            continue

        if hasattr(last_msg, "content") and last_msg.content:
            clean = sanitize(extract_text(last_msg.content))
            if len(clean) > 5:
                logger.info("Agent output:\n%s", clean)


def main() -> int:
    logger.info("DevOps Cloud Agent starting...")
    agent = build_agent()

    try:
        run_health_check(agent, settings.TARGET_REPO)
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
