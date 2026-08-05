import boto3
from crewai.tools import tool
from config import settings, logger


@tool("Check AWS EC2 Health")
def check_aws_ec2_health() -> str:
    """Queries AWS to check for any EC2 instances that are not healthy or running."""
    logger.info("Running AWS EC2 health scan...")
    try:
        ec2 = boto3.client(
            "ec2",
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_DEFAULT_REGION,
        )
        reservations = ec2.describe_instances().get("Reservations", [])
        unhealthy = [
            f"Instance {inst['InstanceId']} ({inst['State']['Name']})"
            for res in reservations
            for inst in res.get("Instances", [])
            if inst["State"]["Name"] != "running"
        ]

        if not unhealthy:
            logger.info("AWS EC2 health check passed. No issues found.")
            return "AWS Status: All EC2 instances are healthy and running."

        logger.warning("Unhealthy EC2 instances detected: %s", unhealthy)
        return "Alert! Unhealthy AWS instances: " + ", ".join(unhealthy)

    except Exception as exc:
        logger.error("AWS EC2 health check failed: %s", exc)
        return f"AWS health check failed: {exc}"
