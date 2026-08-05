from github import Github, Auth
from crewai.tools import tool
from config import settings, logger


@tool("Check GitHub Workflow Status")
def check_github_workflow_status(repo_name: str) -> str:
    """Checks the specified GitHub repository for any failed workflow runs."""
    logger.info("Scanning GitHub Actions for repo: %s", repo_name)

    if not settings.GITHUB_TOKEN:
        return "GitHub Status: GITHUB_TOKEN is not configured."

    try:
        g = Github(auth=Auth.Token(settings.GITHUB_TOKEN), timeout=10)
        repo = g.get_repo(repo_name)

        failed = [
            f"'{run.name}' (Run #{run.run_number}) failed on '{run.head_branch}'"
            for run in repo.get_workflow_runs()[:10]
            if run.conclusion == "failure"
        ]

        if not failed:
            logger.info("GitHub Actions check passed for %s.", repo_name)
            return f"GitHub Status: All recent builds for {repo_name} passed."

        logger.warning("Failed workflow runs in %s: %s", repo_name, failed)
        return f"Failed builds in {repo_name}: " + " | ".join(failed)

    except Exception as exc:
        logger.error("GitHub workflow check failed: %s", exc)
        return f"GitHub check failed: {exc}"
