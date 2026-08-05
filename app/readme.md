# DevOps AI Agent — CrewAI Multi-Agent Platform

## Overview
This project uses **CrewAI** with a **hierarchical process** (manager agent auto-delegates) to run a team of specialized DevOps agents that monitor infrastructure, detect issues, and optimize costs.

---

## Architecture (CrewAI)

```text
                    User Query
                        │
                        ▼
              ┌─────────────────┐
              │  MANAGER AGENT  │  (auto — CrewAI hierarchical)
              │  (decides who   │
              │   to delegate)  │
              └─────────────────┘
                        │
      ┌─────────┬──────┼──────┬──────────┬───────────┐
      ▼         ▼      ▼      ▼          ▼           ▼
 ┌────────┐ ┌──────┐ ┌──────┐ ┌────────┐ ┌────────┐ ┌──────────┐
 │  K8s   │ │ AWS  │ │Deploy│ │  Cost  │ │Incident│ │Migration │
 │ Agent  │ │Agent │ │Agent │ │ Agent  │ │ Agent  │ │  Agent   │
 └────────┘ └──────┘ └──────┘ └────────┘ └────────┘ └──────────┘
      │         │       │          │          │           │
  K8s tools  AWS+GH  rollout   cost scan  crash logs  Jenkinsfile
              tools    tools     tools     + events    converter
```

---

## vs Previous LangGraph Architecture

| Aspect | LangGraph (before) | CrewAI (now) |
|--------|-------------------|--------------|
| Orchestration | Manual StateGraph + supervisor node | Built-in hierarchical process |
| Code complexity | 200+ lines in multi_agent.py | ~50 lines in crew.py |
| Agent routing | Custom conditional edges | Manager auto-delegates |
| Tool loops | Manual 5-iteration loop | CrewAI handles automatically |
| Memory | Manual state dict | Built-in (optional) |
| Delegation | Not supported | Native agent-to-agent delegation |

---

## Quick Start

```bash
# 1. Install dependencies
cd app
pip install -r requirements.txt

# 2. Copy .env and fill in your keys
cp .env.example .env

# 3. Run full health check
python agent.py

# 4. Run with custom query
python agent.py "check kubernetes pods in order-service namespace"
python agent.py "find cost waste"
python agent.py "convert Jenkinsfile to GitHub Actions"

# 5. Run web dashboard
uvicorn web.app:app --reload --port 8000
```

---

## File Structure

```
app/
├── agent.py              ← Main entry point (CrewAI)
├── crew.py               ← CrewAI agents, tasks, and crew definition
├── multi_agent_run.py    ← Alternative entry with custom query support
├── config.py             ← Settings + environment configuration
├── requirements.txt      ← Python dependencies (includes crewai)
├── .env.example          ← Template for environment variables
├── tools/                ← All agent tools (reused from LangChain @tool)
│   ├── aws_tools.py      ← EC2 health check
│   ├── github_tools.py   ← GitHub Actions status
│   ├── k8s_tools.py      ← Pod/node/deployment health
│   ├── deploy_tools.py   ← Rollout status, rollback, restart
│   ├── incident_tools.py ← Crash logs, events, diagnosis
│   ├── cost_tools.py     ← Idle instances, EBS waste, spend
│   └── migration_tools.py← Jenkinsfile → GHA conversion
├── web/
│   └── app.py            ← FastAPI web dashboard
└── graph/                ← (DEPRECATED) Old LangGraph implementation
    ├── workflow.py        ← Single-agent LangGraph (still works)
    └── multi_agent.py    ← Old supervisor pattern (replaced by crew.py)
```

---

## CrewAI Agents

| Agent | Role | Tools |
|-------|------|-------|
| **K8s Agent** | Kubernetes Specialist | pod health, node health, deployments, crash logs, events, diagnosis |
| **AWS Agent** | AWS Infrastructure Specialist | EC2 health, GitHub Actions status |
| **Deploy Agent** | Deployment Specialist | rollout status, rollback, restart, history |
| **Cost Agent** | Cost Optimization Specialist | idle instances, unattached EBS, cost summary |
| **Incident Agent** | Incident Response Specialist | crash logs, cluster events, pod diagnosis |
| **Migration Agent** | CI/CD Migration Specialist | Jenkinsfile conversion, complexity analysis |

---

## How CrewAI Works Here

1. **User sends query** (or default health check runs)
2. **Manager agent** (auto-created by CrewAI hierarchical process) reads the tasks
3. Manager **delegates** each task to the most appropriate specialist agent
4. Each agent uses its **tools** (kubectl, boto3, GitHub API) to gather data
5. Agent returns findings to the manager
6. Manager produces a **final consolidated report**

---

## Configuration

### .env Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `GROQ_API_KEY` | Groq API key (free tier works) | `gsk_abc123...` |
| `CREWAI_LLM` | LLM model in LiteLLM format | `groq/llama-3.3-70b-versatile` |
| `GITHUB_TOKEN` | GitHub PAT for API access | `ghp_abc123...` |
| `TARGET_REPO` | Default repo to monitor | `ShivaKrishna44/devops-microservices-nojenkins` |
| `AWS_ACCESS_KEY_ID` | AWS credentials | `AKIA...` |
| `AWS_SECRET_ACCESS_KEY` | AWS credentials | `wJalr...` |
| `AWS_DEFAULT_REGION` | AWS region | `us-east-1` |

### Supported LLM Models (via Groq free tier)

| Model | Speed | Quality |
|-------|-------|---------|
| `groq/llama-3.3-70b-versatile` | Fast | Best (recommended) |
| `groq/mixtral-8x7b-32768` | Very fast | Good |
| `groq/gemma2-9b-it` | Fastest | Basic |

---

## Tool Matrix

| Category | Tool | Description |
|----------|------|-------------|
| **AWS** | `check_aws_ec2_health()` | Finds EC2 instances not running/healthy |
| **GitHub** | `check_github_workflow_status(repo)` | Checks last 10 runs for failures |
| **K8s** | `check_k8s_pod_health(namespace)` | Finds pods NOT Running/Completed |
| **K8s** | `check_k8s_node_health()` | Finds NotReady nodes |
| **K8s** | `check_k8s_deployments(namespace)` | Finds degraded deployments |
| **Deploy** | `check_rollout_status(deploy, ns)` | Rollout complete/stuck? |
| **Deploy** | `rollback_deployment(deploy, ns)` | Undo to previous version |
| **Deploy** | `restart_deployment(deploy, ns)` | Rolling restart |
| **Deploy** | `get_deployment_history(deploy, ns)` | Revision history |
| **Incident** | `get_pod_crash_logs(pod, ns)` | Previous container logs |
| **Incident** | `get_cluster_events(ns)` | Warning events |
| **Incident** | `diagnose_pod(pod, ns)` | Full pod diagnosis |
| **Cost** | `find_idle_ec2_instances()` | CPU < 5% over 24h |
| **Cost** | `find_unattached_ebs_volumes()` | Orphaned volumes |
| **Cost** | `get_cost_summary()` | Monthly spend by service |
| **Migration** | `convert_jenkinsfile_to_github_actions(content)` | Generate GHA YAML |
| **Migration** | `analyze_jenkinsfile_complexity(content)` | Tier 1/2/3 assessment |
