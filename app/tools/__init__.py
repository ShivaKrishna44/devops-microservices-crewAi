from .aws_tools import check_aws_ec2_health
from .github_tools import check_github_workflow_status

ALL_TOOLS = [check_aws_ec2_health, check_github_workflow_status]
