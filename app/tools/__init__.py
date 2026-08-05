"""All agent tools — registered here for CrewAI to use."""

from .aws_tools import check_aws_ec2_health
from .github_tools import check_github_workflow_status
from .k8s_tools import check_k8s_pod_health, check_k8s_node_health, check_k8s_deployments
from .deploy_tools import check_rollout_status, rollback_deployment, get_deployment_history, restart_deployment
from .incident_tools import get_pod_crash_logs, get_cluster_events, diagnose_pod
from .migration_tools import convert_jenkinsfile_to_github_actions, analyze_jenkinsfile_complexity
from .cost_tools import find_idle_ec2_instances, find_unattached_ebs_volumes, get_cost_summary

ALL_TOOLS = [
    check_aws_ec2_health,
    check_github_workflow_status,
    check_k8s_pod_health,
    check_k8s_node_health,
    check_k8s_deployments,
    check_rollout_status,
    rollback_deployment,
    get_deployment_history,
    restart_deployment,
    get_pod_crash_logs,
    get_cluster_events,
    diagnose_pod,
    convert_jenkinsfile_to_github_actions,
    analyze_jenkinsfile_complexity,
    find_idle_ec2_instances,
    find_unattached_ebs_volumes,
    get_cost_summary,
]
