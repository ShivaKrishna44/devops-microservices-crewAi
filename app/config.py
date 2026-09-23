"""
Configuration for the EKS upgrade agent.

All values come from environment variables (loaded from .env if present) so no
secrets or account-specific values are hardcoded. Safe to commit.
"""
import logging
import os

try:
    from dotenv import load_dotenv
    load_dotenv(override=True)
except ImportError:  # dotenv optional; env vars can be set by the shell
    pass


class Settings:
    # LLM used by the CrewAI agents (e.g. "openrouter/openrouter/free", "groq/...")
    CREWAI_LLM: str = os.getenv("CREWAI_LLM", "openrouter/openrouter/free")

    # Cluster / AWS targeting
    CLUSTER_NAME: str = os.getenv("EKS_CLUSTER_NAME", "expense-dev")
    AWS_REGION: str = os.getenv("AWS_REGION", "us-east-1")

    # Path to the Terraform dir that manages the EKS cluster (eks_version var lives here)
    TERRAFORM_DIR: str = os.getenv(
        "TERRAFORM_DIR",
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "Terraform")),
    )

    # Where the approval gate persists its state / audit trail
    APPROVAL_STORE: str = os.getenv(
        "APPROVAL_STORE",
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "state", "approvals.json")),
    )

    # ── Guardrail / approval policy ──────────────────────────────────────
    # Approvals expire after this many minutes (0 = never expire).
    APPROVAL_TTL_MINUTES: int = int(os.getenv("APPROVAL_TTL_MINUTES", "60"))

    # Regions the agent is allowed to operate in. Comma-separated. Empty = any.
    ALLOWED_REGIONS: list = [
        r.strip() for r in os.getenv("ALLOWED_REGIONS", "us-east-1").split(",") if r.strip()
    ]

    # Substrings that mark a cluster as production (triggers stricter rules).
    PROD_CLUSTER_MARKERS: list = [
        m.strip().lower()
        for m in os.getenv("PROD_CLUSTER_MARKERS", "prod,production,live").split(",")
        if m.strip()
    ]

    # Require two distinct approvers for production clusters.
    REQUIRE_TWO_PERSON_FOR_PROD: bool = (
        os.getenv("REQUIRE_TWO_PERSON_FOR_PROD", "true").lower() == "true"
    )

    # Sequence gate: max acceptable latency (seconds) for `kubectl get nodes`
    # after the control-plane upgrade before it's considered "responsive". The
    # node phase will not start until the API server answers within this bound.
    APISERVER_LATENCY_THRESHOLD_S: float = float(
        os.getenv("APISERVER_LATENCY_THRESHOLD_S", "10")
    )

    # Live availability monitor (runs DURING the node rollover).
    # Max fraction of a critical deployment's baseline healthy pods that may be
    # lost before the alarm fires. 0.20 = alarm if a deployment drops >20%.
    AVAILABILITY_DROP_THRESHOLD: float = float(
        os.getenv("AVAILABILITY_DROP_THRESHOLD", "0.20")
    )
    # Namespaces whose deployments are 'critical' for the availability alarm.
    # Empty = treat all app namespaces (excluding kube-system etc.) as critical.
    CRITICAL_NAMESPACES: list = [
        n.strip() for n in os.getenv("CRITICAL_NAMESPACES", "").split(",") if n.strip()
    ]
    # Optional webhook (Slack-style {"text": ...}) to sound the alarm externally.
    ALARM_WEBHOOK_URL: str = os.getenv("ALARM_WEBHOOK_URL", "")

    # On an availability breach during the rollover, also HALT further node
    # draining (cordon remaining old nodes) so a human can debug, instead of
    # only sounding the alarm. Default on.
    HALT_ON_AVAILABILITY_BREACH: bool = (
        os.getenv("HALT_ON_AVAILABILITY_BREACH", "true").lower() == "true"
    )


settings = Settings()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] eks-upgrade-agent: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("eks-upgrade-agent")
