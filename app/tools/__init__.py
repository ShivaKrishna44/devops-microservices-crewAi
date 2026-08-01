"""All agent tools — registered here for LangGraph to use."""

# Existing tools
from .aws_tools import check_aws_ec2_health
from .github_tools import check_github_workflow_status

# New: Kubernetes health tools
from .k8s_tools import (
    check_k8s_pod_health,
    check_k8s_node_health,
    check_k8s_deployments,
)

# New: Deployment tools
from .deploy_tools import (
    check_rollout_status,
    rollback_deployment,
    get_deployment_history,
    restart_deployment,
)

# New: Incident response tools
from .incident_tools import (
    get_pod_crash_logs,
    get_cluster_events,
    diagnose_pod,
)

# New: Migration tools
from .migration_tools import (
    convert_jenkinsfile_to_github_actions,
    analyze_jenkinsfile_complexity,
)

# New: Cost optimization tools
from .cost_tools import (
    find_idle_ec2_instances,
    find_unattached_ebs_volumes,
    get_cost_summary,
)

# All tools available to the agent
ALL_TOOLS = [
    # AWS
    check_aws_ec2_health,
    # GitHub
    check_github_workflow_status,
    # Kubernetes
    check_k8s_pod_health,
    check_k8s_node_health,
    check_k8s_deployments,
    # Deployment
    check_rollout_status,
    rollback_deployment,
    get_deployment_history,
    restart_deployment,
    # Incident Response
    get_pod_crash_logs,
    get_cluster_events,
    diagnose_pod,
    # Migration
    convert_jenkinsfile_to_github_actions,
    analyze_jenkinsfile_complexity,
    # Cost Optimization
    find_idle_ec2_instances,
    find_unattached_ebs_volumes,
    get_cost_summary,
]
