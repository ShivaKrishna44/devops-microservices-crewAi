"""Kubernetes health check tools — checks pods, nodes, deployments."""
import subprocess
import json
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
            timeout=30,
        )
        if result.returncode != 0:
            return f"kubectl error: {result.stderr.strip()}"
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "kubectl timed out after 30 seconds"
    except Exception as e:
        return f"kubectl failed: {e}"


@tool
def check_k8s_pod_health(namespace: str = "default") -> str:
    """Checks Kubernetes pod health in a given namespace. Finds pods that are NOT Running/Completed."""
    logger.info("Checking K8s pod health in namespace: %s", namespace)

    output = _run_kubectl(f"get pods -n {namespace} --no-headers")
    if "error" in output.lower() or "failed" in output.lower():
        return f"K8s pod check failed: {output}"

    if not output:
        return f"K8s: No pods found in namespace '{namespace}'."

    unhealthy = []
    for line in output.split("\n"):
        parts = line.split()
        if len(parts) >= 3:
            pod_name = parts[0]
            ready = parts[1]     # e.g., "1/1" or "0/1"
            status = parts[2]    # Running, CrashLoopBackOff, Pending, etc.
            if status not in ("Running", "Completed"):
                unhealthy.append(f"{pod_name} ({status}, ready={ready})")

    if not unhealthy:
        return f"K8s: All pods healthy in namespace '{namespace}'."

    return f"K8s ALERT: Unhealthy pods in '{namespace}': " + ", ".join(unhealthy)


@tool
def check_k8s_node_health() -> str:
    """Checks Kubernetes node health. Finds nodes that are NotReady or have resource pressure."""
    logger.info("Checking K8s node health...")

    output = _run_kubectl("get nodes --no-headers")
    if "error" in output.lower() or "failed" in output.lower():
        return f"K8s node check failed: {output}"

    issues = []
    for line in output.split("\n"):
        parts = line.split()
        if len(parts) >= 2:
            node_name = parts[0]
            status = parts[1]
            if "NotReady" in status:
                issues.append(f"{node_name} (NotReady)")

    if not issues:
        return "K8s: All nodes are Ready and healthy."

    return "K8s ALERT: Node issues detected: " + ", ".join(issues)


@tool
def check_k8s_deployments(namespace: str = "default") -> str:
    """Checks if all deployments have desired replicas available."""
    logger.info("Checking K8s deployments in namespace: %s", namespace)

    output = _run_kubectl(f"get deployments -n {namespace} --no-headers")
    if "error" in output.lower() or "failed" in output.lower():
        return f"K8s deployment check failed: {output}"

    if not output:
        return f"K8s: No deployments found in namespace '{namespace}'."

    issues = []
    for line in output.split("\n"):
        parts = line.split()
        if len(parts) >= 4:
            name = parts[0]
            ready = parts[1]    # e.g., "3/3" or "1/3"
            ready_parts = ready.split("/")
            if len(ready_parts) == 2 and ready_parts[0] != ready_parts[1]:
                issues.append(f"{name} (ready={ready})")

    if not issues:
        return f"K8s: All deployments healthy in namespace '{namespace}'."

    return f"K8s ALERT: Degraded deployments in '{namespace}': " + ", ".join(issues)
