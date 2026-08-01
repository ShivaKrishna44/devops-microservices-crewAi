"""Cost optimization tools — find idle resources, oversized instances."""
import boto3
from langchain_core.tools import tool
from config import settings, logger


def _get_ec2_client():
    return boto3.client(
        "ec2",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_DEFAULT_REGION,
    )


def _get_cloudwatch_client():
    return boto3.client(
        "cloudwatch",
        aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
        region_name=settings.AWS_DEFAULT_REGION,
    )


@tool
def find_idle_ec2_instances() -> str:
    """Finds EC2 instances that are running but have very low CPU usage (potential cost waste).
    Checks instances with < 5% average CPU over last 24 hours."""
    logger.info("Scanning for idle EC2 instances...")

    try:
        ec2 = _get_ec2_client()
        cw = _get_cloudwatch_client()

        from datetime import datetime, timedelta

        reservations = ec2.describe_instances(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]
        ).get("Reservations", [])

        idle_instances = []
        for res in reservations:
            for inst in res.get("Instances", []):
                instance_id = inst["InstanceId"]
                instance_type = inst["InstanceType"]
                name = next(
                    (t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"),
                    "unnamed",
                )

                # Get average CPU over last 24 hours
                try:
                    metrics = cw.get_metric_statistics(
                        Namespace="AWS/EC2",
                        MetricName="CPUUtilization",
                        Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
                        StartTime=datetime.utcnow() - timedelta(hours=24),
                        EndTime=datetime.utcnow(),
                        Period=86400,
                        Statistics=["Average"],
                    )
                    datapoints = metrics.get("Datapoints", [])
                    avg_cpu = datapoints[0]["Average"] if datapoints else -1

                    if 0 <= avg_cpu < 5:
                        idle_instances.append(
                            f"{name} ({instance_id}, {instance_type}) — avg CPU: {avg_cpu:.1f}%"
                        )
                except Exception:
                    pass

        if not idle_instances:
            return "Cost Check: No idle instances found. All running instances have > 5% CPU usage."

        monthly_waste = len(idle_instances) * 30  # Rough estimate: ~$30/month per idle t3.medium
        return (
            f"COST ALERT: {len(idle_instances)} idle instances found (< 5% CPU):\n"
            + "\n".join(f"  - {i}" for i in idle_instances)
            + f"\n\nEstimated waste: ~${monthly_waste}/month. Consider stopping or right-sizing."
        )

    except Exception as e:
        logger.error("Cost scan failed: %s", e)
        return f"Cost scan failed: {e}"


@tool
def find_unattached_ebs_volumes() -> str:
    """Finds EBS volumes that are not attached to any instance (wasting money doing nothing)."""
    logger.info("Scanning for unattached EBS volumes...")

    try:
        ec2 = _get_ec2_client()

        volumes = ec2.describe_volumes(
            Filters=[{"Name": "status", "Values": ["available"]}]
        ).get("Volumes", [])

        if not volumes:
            return "Cost Check: No unattached EBS volumes found. Storage looks clean."

        total_gb = sum(v["Size"] for v in volumes)
        monthly_cost = total_gb * 0.10  # gp3 = ~$0.08-0.10/GB/month

        volume_list = [
            f"  - {v['VolumeId']} ({v['Size']}GB, {v['VolumeType']}, created {v['CreateTime'].strftime('%Y-%m-%d')})"
            for v in volumes[:10]  # Show max 10
        ]

        return (
            f"COST ALERT: {len(volumes)} unattached EBS volumes found ({total_gb}GB total):\n"
            + "\n".join(volume_list)
            + f"\n\nEstimated waste: ~${monthly_cost:.0f}/month."
            + "\nAction: Delete if data not needed, or snapshot and delete."
        )

    except Exception as e:
        logger.error("EBS scan failed: %s", e)
        return f"EBS scan failed: {e}"


@tool
def get_cost_summary() -> str:
    """Gets a quick AWS cost summary for the current month using Cost Explorer."""
    logger.info("Getting AWS cost summary...")

    try:
        ce = boto3.client(
            "ce",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name="us-east-1",  # Cost Explorer only works in us-east-1
        )

        from datetime import datetime

        today = datetime.utcnow()
        start = today.replace(day=1).strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")

        response = ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Metrics=["UnblendedCost"],
            GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
        )

        results = response.get("ResultsByTime", [])
        if not results:
            return "Cost Explorer: No data available for current month."

        groups = results[0].get("Groups", [])
        total = 0.0
        top_services = []

        for g in sorted(groups, key=lambda x: float(x["Metrics"]["UnblendedCost"]["Amount"]), reverse=True)[:5]:
            service = g["Keys"][0]
            amount = float(g["Metrics"]["UnblendedCost"]["Amount"])
            total += amount
            if amount > 0.01:
                top_services.append(f"  - {service}: ${amount:.2f}")

        return (
            f"AWS Cost Summary (this month so far): ${total:.2f}\n"
            f"Top services:\n" + "\n".join(top_services)
        )

    except Exception as e:
        logger.error("Cost summary failed: %s", e)
        return f"Cost summary failed: {e}"
