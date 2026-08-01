"""Migration tools — analyze Jenkinsfile and generate GitHub Actions YAML."""
from langchain_core.tools import tool
from config import logger


@tool
def convert_jenkinsfile_to_github_actions(jenkinsfile_content: str) -> str:
    """Analyzes a Jenkinsfile and generates equivalent GitHub Actions workflow YAML.
    Pass the full Jenkinsfile content as input."""
    logger.info("Converting Jenkinsfile to GitHub Actions YAML...")

    if not jenkinsfile_content.strip():
        return "Error: Empty Jenkinsfile content provided."

    # Parse stages from Jenkinsfile
    stages = []
    lines = jenkinsfile_content.split("\n")
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("stage(") or stripped.startswith("stage '"):
            # Extract stage name
            stage_name = stripped.replace("stage(", "").replace("stage '", "")
            stage_name = stage_name.strip("()\"' {")
            stages.append(stage_name.lower().replace(" ", "-"))

    if not stages:
        stages = ["build", "test", "deploy"]

    # Generate GitHub Actions YAML
    yaml_output = f"""# Auto-generated from Jenkinsfile
# Review and customize before using in production
name: CI/CD Pipeline
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

permissions:
  id-token: write
  contents: read

jobs:"""

    for i, stage in enumerate(stages):
        needs = f"\n    needs: [{stages[i-1]}]" if i > 0 else ""
        yaml_output += f"""
  {stage}:
    runs-on: ubuntu-latest{needs}
    steps:
      - uses: actions/checkout@v4
      - name: {stage.replace('-', ' ').title()}
        run: |
          echo "TODO: Add {stage} commands here"
"""

    # Add recommendations
    recommendations = """
# ════════════════════════════════════════════
# MIGRATION RECOMMENDATIONS:
# ════════════════════════════════════════════
# 1. Replace hardcoded credentials with OIDC:
#    - uses: aws-actions/configure-aws-credentials@v4
#      with:
#        role-to-assume: arn:aws:iam::ACCOUNT:role/github-role
#
# 2. Replace Shared Libraries with Reusable Workflows:
#    jobs:
#      build:
#        uses: company/platform/.github/workflows/build.yml@v2
#
# 3. Add security scanning:
#    - uses: aquasecurity/trivy-action@master
#
# 4. Pin action versions to SHA for supply chain security
# ════════════════════════════════════════════"""

    return f"Generated GitHub Actions workflow:\n```yaml\n{yaml_output}\n{recommendations}\n```"


@tool
def analyze_jenkinsfile_complexity(jenkinsfile_content: str) -> str:
    """Analyzes a Jenkinsfile to determine migration complexity (Tier 1/2/3).
    Tier 1 = simple, Tier 2 = moderate (Docker+K8s), Tier 3 = complex (approvals+multi-stage)."""
    logger.info("Analyzing Jenkinsfile complexity...")

    if not jenkinsfile_content.strip():
        return "Error: Empty Jenkinsfile content provided."

    content_lower = jenkinsfile_content.lower()

    # Score complexity
    score = 0
    findings = []

    # Check for Docker
    if "docker" in content_lower:
        score += 1
        findings.append("Docker build detected")

    # Check for Kubernetes/Helm
    if "kubectl" in content_lower or "helm" in content_lower:
        score += 1
        findings.append("Kubernetes/Helm deployment detected")

    # Check for approvals/input
    if "input" in content_lower or "approval" in content_lower:
        score += 2
        findings.append("Manual approval gate detected")

    # Check for multi-branch/parallel
    if "parallel" in content_lower:
        score += 1
        findings.append("Parallel stages detected")

    # Check for Terraform/infra
    if "terraform" in content_lower:
        score += 2
        findings.append("Terraform/Infrastructure provisioning detected")

    # Check for shared libraries
    if "@library" in content_lower or "shared" in content_lower:
        score += 1
        findings.append("Shared Library usage detected")

    # Check for credentials
    cred_count = content_lower.count("credentials(") + content_lower.count("withcredentials")
    if cred_count > 0:
        score += 1
        findings.append(f"{cred_count} credential references found")

    # Determine tier
    if score <= 1:
        tier = "Tier 1 (Simple)"
        effort = "1-2 hours"
        approach = "Direct conversion to GitHub Actions YAML"
    elif score <= 3:
        tier = "Tier 2 (Moderate)"
        effort = "4-8 hours"
        approach = "Convert to reusable workflow + OIDC for credentials"
    else:
        tier = "Tier 3 (Complex)"
        effort = "1-3 days"
        approach = "Break into stages, implement approvals via environments, convert infra separately"

    return f"""Jenkinsfile Analysis:
Complexity: {tier} (score: {score}/10)
Estimated effort: {effort}
Recommended approach: {approach}

Findings:
{chr(10).join(f'  - {f}' for f in findings) if findings else '  - Simple build pipeline (no complex patterns)'}

Migration steps:
  1. Map stages → GitHub Actions jobs
  2. Replace credentials → OIDC + GitHub Secrets
  3. Replace shared libraries → Reusable Workflows
  4. Add security scanning (Trivy, CodeQL)
  5. Test in parallel with Jenkins before cutover"""
