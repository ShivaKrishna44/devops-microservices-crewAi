# bin/ — helper scripts

Convenience scripts so you don't have to remember the venv / Python-version dance.

| Script | What it does |
|--------|--------------|
| `setup.sh` / `setup.ps1` | Creates a Python **3.13** venv (CrewAI needs 3.10–3.13, not 3.14), installs deps, copies `.env.example` → `.env` on first run, and runs a safe `--status` smoke check. |
| `run.sh` / `run.ps1` | Runs the agent through the venv's Python. Passes all args to `main.py`. |
| `test.sh` | Runs the unit test suite (no cluster/AWS/CrewAI needed). |

## Quick start

**Git Bash:**
```bash
bash bin/setup.sh                       # one-time
# edit app/.env, then:
aws eks update-kubeconfig --name expense-dev --region us-east-1
bash bin/run.sh --status
bash bin/run.sh --target-version 1.34   # pre-checks; abort at APPROVE to stay safe
```

**PowerShell:**
```powershell
./bin/setup.ps1
./bin/run.ps1 --status
./bin/run.ps1 --target-version 1.34
```

## Notes
- The venv lives at `app/venv/` and is git-ignored — it won't be committed.
- Unit tests run on any Python (incl. 3.14) via `bin/test.sh`; the live agent
  needs the 3.13 venv that `setup.sh` creates.
- `run.sh`/`run.ps1` don't require you to activate the venv — they call its
  Python directly.
