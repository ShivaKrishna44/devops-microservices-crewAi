# Cleanup before publishing

This repo was cloned from a microservices-monitoring base, so it carries files
that are **not part of the EKS upgrade agent**. The `.gitignore` already prevents
them (and any secrets/state/binaries) from being published — but it's cleaner to
delete them from disk. Run the commands below once.

## 1. Rotate the exposed secrets FIRST (critical)

`app/.env` (inherited) contained **live credentials**. Treat them as compromised
and rotate before doing anything else:

- **AWS** access key `AKIAYSOSY5PJB7AGSZ5P` — deactivate/delete in IAM, issue a new one
- **GitHub PAT** (`github_pat_...`) — revoke
- **Groq**, **CrewAI**, **LangSmith** keys — regenerate

## 2. Delete the inherited cruft (PowerShell, from the repo root)

```powershell
# secrets / state / binaries
Remove-Item -Force app\.env, terraform.tfstate, helm.exe, kubectl.exe -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force .terraform -ErrorAction SilentlyContinue

# old microservices stack (not used by the upgrade agent)
Remove-Item -Recurse -Force charts, kubernetes, mcp-server, scripts, Terraform -ErrorAction SilentlyContinue

# old crewAi monitoring app files
Remove-Item -Force app\agent.py, app\agent.py.bk, app\multi_agent_run.py, app\litellm_patch.py, app\readme.md -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force app\graph, app\web, app\order-service, app\payment-service, app\user-service, app\venv, app\__pycache__ -ErrorAction SilentlyContinue

# old crewAi tools (keep only eks_tools, upgrade_tools, health_tools, __init__)
Remove-Item -Force app\tools\aws_tools.py, app\tools\cost_tools.py, app\tools\deploy_tools.py, app\tools\github_tools.py, app\tools\incident_tools.py, app\tools\k8s_tools.py, app\tools\migration_tools.py -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force app\tools\__pycache__ -ErrorAction SilentlyContinue
```

## 3. Verify what remains (should be ONLY the upgrade agent)

```powershell
Get-ChildItem -Recurse -File | Where-Object { $_.FullName -notmatch '\\.git\\' } | Select-Object FullName
```

Expected keep-list:
```
app/main.py  app/crew.py  app/guardrails.py  app/approval_gate.py  app/config.py
app/requirements.txt  app/.env.example
app/tools/__init__.py  app/tools/eks_tools.py  app/tools/upgrade_tools.py  app/tools/health_tools.py
tests/  (all test_*.py, conftest.py, README.md)
.github/workflows/eks-upgrade.yml
docs/DEPLOYMENT-GUIDE.md
README.md  DEMO-GUIDE.md  pytest.ini  .gitignore  .gitattributes
```

## 4. If this git history came from the clone, start fresh

The cloned `.git` history may still contain the old files AND the committed
secrets. For a clean public repo, re-initialize history:

```powershell
Remove-Item -Recurse -Force .git
git init
git add .
git commit -m "Initial commit: guarded-eks-upgrade-agent"
# then create the GitHub repo 'guarded-eks-upgrade-agent' and push
```

## 5. Final secret scan before pushing

```powershell
# if you have gitleaks installed
gitleaks detect --source . --no-banner
```

Delete this CLEANUP.md once done.
