"""Incident response tools — diagnose issues, check events, get crash logs."""
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
            timeout=30,
        )
        if result.returncode != 0:
            return f"kubectl error: {result.stderr.strip()}"
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "kubectl timed out"
    except Exception as e:
        return f"kubectl failed: {e}"


@tool
def get_pod_crash_logs(pod_name: str, namespace: str = "default") -> str:
    """Gets logs from a crashed/restarting pod (previous container). Useful for diagnosing CrashLoopBackOff."""
    logger.info("Getting crash logs for pod: %s in %s", pod_name, namespace)

    # Try previous container logs first
    output = _run_kubectl(f"logs {pod_name} -n {namespace} --previous --tail=50")
    if "error" in output.lower() and "previous" in output.lower():
        # No previous container, get current logs
        output = _run_kubectl(f"logs {pod_name} -n {namespace} --tail=50")

    if not output or "error" in output.lower():
        return f"Could not get logs for pod '{pod_name}': {output}"

    return f"Crash logs for '{pod_name}':\n{output[-2000:]}"  # Limit to 2000 chars


@tool
def get_cluster_events(namespace: str = "default") -> str:
    """Gets recent Kubernetes events — shows scheduling failures, pull errors, OOM kills, etc."""
    logger.info("Getting cluster events in namespace: %s", namespace)

    output = _run_kubectl(
        f"get events -n {namespace} --sort-by=.metadata.creationTimestamp --field-selector type=Warning"
    )

    if not output or "No resources found" in output:
        return f"No warning events in namespace '{namespace}'. Cluster looks healthy."

    # Return last 2000 chars of events
    lines = output.split("\n")
    recent = "\n".join(lines[-15:])  # Last 15 events
    return f"Recent warning events in '{namespace}':\n{recent}"


@tool
def diagnose_pod(pod_name: str, namespace: str = "default") -> str:
    """Runs full diagnosis on a pod: status, events, exit code, resource usage."""
    logger.info("Diagnosing pod: %s in %s", pod_name, namespace)

    # Get pod status
    status_output = _run_kubectl(f"get pod {pod_name} -n {namespace} --no-headers")

    # Get pod events
    events_output = _run_kubectl(
        f"get events -n {namespace} --field-selector involvedObject.name={pod_name} --sort-by=.metadata.creationTimestamp"
    )

    # Get describe (last state, exit code)
    describe_output = _run_kubectl(f"describe pod {pod_name} -n {namespace}")

    # Extract key info from describe
    exit_code = ""
    reason = ""
    for line in describe_output.split("\n"):
        if "Exit Code" in line:
            exit_code = line.strip()
        if "Reason:" in line:
            reason = line.strip()

    diagnosis = f"""Pod Diagnosis: {pod_name}
Status: {status_output}
{exit_code}
{reason}

Recent Events:
{events_output[-1000:] if events_output else 'No events found'}

Interpretation:
- Exit 137 = OOMKilled (increase memory limit)
- Exit 1 = Application error (check logs)
- Exit 127 = Command not found (wrong entrypoint)
- ImagePullBackOff = Wrong image name/tag or no ECR access
- Pending = No nodes with enough resources"""

    return diagnosis
