"""
DevOps AI Agent — Web Dashboard (CrewAI)
==========================================
A web UI where engineers can:
- Trigger health checks on demand (powered by CrewAI)
- View historical reports
- See status per check category

Run: uvicorn web.app:app --reload --port 8000
Open: http://localhost:8000
"""

import json
import sys
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

# Add parent dir to path so we can import agent modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import settings, logger

app = FastAPI(title="DevOps AI Agent Dashboard (CrewAI)")

# Store reports in a JSON file (simple — no DB needed)
REPORTS_FILE = Path(__file__).parent / "reports.json"


def load_reports() -> list:
    """Load saved reports from file."""
    if REPORTS_FILE.exists():
        return json.loads(REPORTS_FILE.read_text())
    return []


def save_report(report: dict):
    """Save a new report to file."""
    reports = load_reports()
    reports.insert(0, report)  # newest first
    reports = reports[:50]  # keep last 50 reports only
    REPORTS_FILE.write_text(json.dumps(reports, indent=2, default=str))


def run_agent_check() -> dict:
    """Run the CrewAI multi-agent health check and return results."""
    from crew import run_devops_crew

    logger.info("Web dashboard triggering CrewAI health check...")

    result_text = run_devops_crew("")  # Empty = full health check

    # Parse result for status indicators
    result_lower = result_text.lower()

    checks = {
        "github_actions": "pass",
        "aws_ec2": "pass",
        "k8s_pods": "pass",
        "k8s_nodes": "pass",
        "k8s_deployments": "pass",
        "cost_idle_instances": "pass",
        "cost_ebs_volumes": "pass",
        "cost_summary": "info",
    }

    # Determine statuses from result text
    if "failed" in result_lower and "github" in result_lower:
        checks["github_actions"] = "fail"
    if "unhealthy" in result_lower and "ec2" in result_lower:
        checks["aws_ec2"] = "fail"
    if "unreachable" in result_lower and "k8s" in result_lower:
        checks["k8s_pods"] = "unreachable"
        checks["k8s_nodes"] = "unreachable"
        checks["k8s_deployments"] = "unreachable"
    elif "crashloop" in result_lower or "not running" in result_lower:
        checks["k8s_pods"] = "fail"
    if "notready" in result_lower:
        checks["k8s_nodes"] = "fail"
    if "idle" in result_lower and "instance" in result_lower:
        checks["cost_idle_instances"] = "warn"
    if "unattached" in result_lower and "ebs" in result_lower:
        checks["cost_ebs_volumes"] = "warn"

    # Overall status
    if any(v == "fail" for v in checks.values()):
        overall = "critical"
    elif any(v == "warn" for v in checks.values()):
        overall = "warning"
    elif any(v == "unreachable" for v in checks.values()):
        overall = "degraded"
    else:
        overall = "healthy"

    report = {
        "timestamp": datetime.now().isoformat(),
        "result": result_text[:5000],  # Cap at 5k chars
        "checks": checks,
        "repo": settings.TARGET_REPO,
        "status": overall,
        "engine": "CrewAI (hierarchical)",
    }

    save_report(report)
    return report


# ═══════════════════════════════════════════
# API Endpoints
# ═══════════════════════════════════════════

def _status_icon(status: str) -> str:
    """Return emoji for status."""
    icons = {"pass": "&#x1F7E2;", "fail": "&#x1F534;", "warn": "&#x1F7E1;",
             "unreachable": "&#x26AA;", "info": "&#x1F535;"}
    return icons.get(status, "&#x26AA;")


def _build_health_grid(report: dict) -> str:
    """Build HTML grid showing individual check statuses."""
    if not report or "checks" not in report:
        return '<p style="color:#666">No health data yet. Run a check first.</p>'

    checks = report["checks"]
    check_labels = {
        "github_actions": "GitHub Actions",
        "aws_ec2": "AWS EC2 Health",
        "k8s_pods": "K8s Pods",
        "k8s_nodes": "K8s Nodes",
        "k8s_deployments": "K8s Deployments",
        "cost_idle_instances": "Idle Instances",
        "cost_ebs_volumes": "EBS Volumes",
        "cost_summary": "Cost Summary",
    }

    grid = ""
    for key, label in check_labels.items():
        status = checks.get(key, "unknown")
        icon = _status_icon(status)
        bg = "#1b3a1b" if status == "pass" else "#3a1b1b" if status == "fail" else "#3a3a1b" if status == "warn" else "#1b1b2e"
        grid += (
            f'<div class="check-card" style="background:{bg}">'
            f'<span class="check-icon">{icon}</span>'
            f'<span class="check-label">{label}</span>'
            f'<span class="check-status">{status.upper()}</span>'
            f'</div>'
        )

    return grid


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Main dashboard — shows all reports."""
    reports = load_reports()

    rows = ""
    for r in reports[:20]:
        timestamp = r.get("timestamp", "unknown")[:19]
        status = r.get("status", "unknown")
        status_icons = {"healthy": "&#x1F7E2;", "critical": "&#x1F534;",
                        "warning": "&#x1F7E1;", "degraded": "&#x26AA;"}
        status_icon = status_icons.get(status, "&#x26AA;")
        repo = r.get("repo", "")
        engine = r.get("engine", "unknown")
        result_preview = r.get("result", "")[:150].replace("<", "&lt;").replace(">", "&gt;")

        rows += f"""
        <tr>
            <td>{status_icon} {status}</td>
            <td>{timestamp}</td>
            <td>{repo}</td>
            <td>{engine}</td>
            <td style="font-size:12px">{result_preview}...</td>
        </tr>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>DevOps AI Agent - CrewAI Dashboard</title>
        <style>
            body {{ font-family: -apple-system, sans-serif; margin: 40px; background: #1a1a2e; color: #eee; }}
            h1 {{ color: #00d4aa; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
            th {{ background: #16213e; padding: 12px; text-align: left; }}
            td {{ padding: 10px; border-bottom: 1px solid #333; }}
            tr:hover {{ background: #16213e; }}
            .btn {{ background: #00d4aa; color: #000; padding: 12px 24px; border: none;
                    border-radius: 6px; cursor: pointer; font-size: 16px; font-weight: bold; }}
            .btn:hover {{ background: #00b894; }}
            .header {{ display: flex; justify-content: space-between; align-items: center; }}
            .stats {{ display: flex; gap: 20px; margin: 20px 0; }}
            .stat-box {{ background: #16213e; padding: 20px; border-radius: 8px; text-align: center; }}
            .stat-num {{ font-size: 32px; color: #00d4aa; }}
            .health-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 20px 0; }}
            .check-card {{ padding: 16px; border-radius: 8px; display: flex; flex-direction: column; align-items: center; gap: 8px; }}
            .check-icon {{ font-size: 28px; }}
            .check-label {{ font-size: 13px; color: #aaa; }}
            .check-status {{ font-size: 11px; font-weight: bold; letter-spacing: 1px; }}
            .badge {{ display: inline-block; background: #7c3aed; color: white; padding: 3px 8px;
                      border-radius: 4px; font-size: 11px; margin-left: 10px; }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>&#x1F916; DevOps AI Agent Dashboard <span class="badge">CrewAI</span></h1>
            <form action="/run" method="get">
                <button class="btn" type="submit">&#x25B6; Run Health Check Now</button>
            </form>
        </div>

        <div class="stats">
            <div class="stat-box">
                <div class="stat-num">{len(reports)}</div>
                <div>Total Reports</div>
            </div>
            <div class="stat-box">
                <div class="stat-num">{sum(1 for r in reports if r.get('status') == 'healthy')}</div>
                <div>Healthy</div>
            </div>
            <div class="stat-box">
                <div class="stat-num">{sum(1 for r in reports if r.get('status') in ('critical', 'warning', 'degraded'))}</div>
                <div>Issues</div>
            </div>
            <div class="stat-box">
                <div class="stat-num">6</div>
                <div>AI Agents</div>
            </div>
        </div>

        <!-- Latest Health Check Status Grid -->
        <h2>Current Health Status</h2>
        <div class="health-grid">
            {_build_health_grid(reports[0] if reports else None)}
        </div>

        <h2>Recent Reports</h2>
        <table>
            <tr>
                <th>Status</th>
                <th>Time</th>
                <th>Repository</th>
                <th>Engine</th>
                <th>Summary</th>
            </tr>
            {rows if rows else '<tr><td colspan="5">No reports yet. Click "Run Health Check Now" to start.</td></tr>'}
        </table>

        <p style="margin-top:40px; color:#666;">
            Powered by CrewAI (hierarchical process) with 6 specialized agents.<br>
            Agents: K8s, AWS, Deploy, Cost, Incident, Migration<br>
            Reports stored locally. Refresh page to see latest.
        </p>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/run", response_class=HTMLResponse)
async def run_check():
    """Trigger a new health check and redirect back to dashboard."""
    try:
        report = run_agent_check()
        status = report.get("status", "unknown")
        message = f"Health check complete. Status: {status}"
    except Exception as e:
        message = f"Agent run failed: {e}"
        logger.error("Web agent run failed: %s", e)

    html = f"""
    <html>
    <head>
        <meta http-equiv="refresh" content="3;url=/" />
        <style>
            body {{ font-family: sans-serif; text-align: center; margin-top: 100px; background: #1a1a2e; color: #eee; }}
        </style>
    </head>
    <body>
        <h2>&#x2705; {message}</h2>
        <p>Redirecting to dashboard in 3 seconds...</p>
        <a href="/" style="color:#00d4aa;">Go to Dashboard</a>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.get("/api/reports")
async def api_reports():
    """API endpoint — returns reports as JSON (for integrations)."""
    return load_reports()


@app.get("/api/latest")
async def api_latest():
    """API endpoint — returns the most recent report."""
    reports = load_reports()
    return reports[0] if reports else {"message": "No reports yet"}
