"""Deployment tools — trigger rollout, check status, rollback."""
import subprocess
from langchain_core.tools import tool
from config import logger


def _run_kubectl(cmd: str) -> str:
    """Run a kubectl command and return output."""
    try:
        result = subprocess.run(
            f"kubectl {cmd}",
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return f"kubectl error: {result.stderr.strip()}"
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "kubectl timed out"
    except Exception as e:
        return f"kubectl failed: {e}"


@tool
def check_rollout_status(deployment: str, namespace: str = "default") -> str:
    """Checks if a Kubernetes deployment rollout is progressing, complete, or stuck."""
    logger.info("Checking rollout status: %s in %s", deployment, namespace)

    output = _run_kubectl(f"rollout status deployment/{deployment} -n {namespace} --timeout=30s")

    if "successfully rolled out" in output:
        return f"Deployment '{deployment}' rollout is COMPLETE and healthy."
    elif "error" in output.lower() or "timed out" in output.lower():
        return f"Deployment '{deployment}' rollout is STUCK or FAILING: {output}"
    else:
        return f"Deployment '{deployment}' rollout status: {output}"


@tool
def rollback_deployment(deployment: str, namespace: str = "default") -> str:
    """Rolls back a Kubernetes deployment to the previous revision."""
    logger.info("Rolling back deployment: %s in %s", deployment, namespace)

    output = _run_kubectl(f"rollout undo deployment/{deployment} -n {namespace}")

    if "rolled back" in output.lower() or "error" not in output.lower():
        return f"SUCCESS: Deployment '{deployment}' rolled back to previous version."
    else:
        return f"ROLLBACK FAILED for '{deployment}': {output}"


@tool
def get_deployment_history(deployment: str, namespace: str = "default") -> str:
    """Shows the revision history of a deployment (for rollback decisions)."""
    logger.info("Getting deployment history: %s in %s", deployment, namespace)

    output = _run_kubectl(f"rollout history deployment/{deployment} -n {namespace}")

    if not output or "error" in output.lower():
        return f"Could not get history for '{deployment}': {output}"

    return f"Deployment history for '{deployment}':\n{output}"


@tool
def restart_deployment(deployment: str, namespace: str = "default") -> str:
    """Restarts all pods of a deployment (rolling restart — zero downtime)."""
    logger.info("Restarting deployment: %s in %s", deployment, namespace)

    output = _run_kubectl(f"rollout restart deployment/{deployment} -n {namespace}")

    if "restarted" in output.lower() or "error" not in output.lower():
        return f"SUCCESS: Deployment '{deployment}' restarting (rolling restart, zero downtime)."
    else:
        return f"RESTART FAILED for '{deployment}': {output}"
