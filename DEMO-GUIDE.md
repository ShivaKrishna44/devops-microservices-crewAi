# Demo Guide — CrewAI Multi-Agent DevOps Platform

## What is this project?

An AI-powered DevOps monitoring platform using **CrewAI** with 6 specialized agents that work as a team — a Manager delegates tasks to specialists who check Kubernetes, AWS, costs, deployments, incidents, and CI/CD migrations autonomously.

---

## The Big Picture (Simple)

```
YOU: "check my infrastructure health"
  │
  ▼
MANAGER AGENT (CrewAI auto-creates this)
  │
  │ Thinks: "This needs K8s check + AWS check + Cost scan"
  │
  ├──→ K8s Agent: runs kubectl, checks pods/nodes/deployments
  ├──→ AWS Agent: checks EC2 health, GitHub Actions status
  ├──→ Cost Agent: finds idle instances, unattached EBS volumes
  │
  ▼
FINAL REPORT: "3 pods healthy, 1 failed workflow, 2 idle instances ($60/month waste)"
```

---

## How CrewAI Works (Simple Explanation)

Think of it like a **team at work**:

| Real World | CrewAI Equivalent |
|---|---|
| Team Manager | Manager Agent (auto-created) |
| K8s Engineer | K8s Agent |
| AWS Admin | AWS Agent |
| FinOps Analyst | Cost Agent |
| SRE On-call | Incident Agent |
| Platform Engineer | Migration Agent |
| Their skills | Tools (kubectl, boto3, GitHub API) |
| Team meeting | Hierarchical Process |
| Meeting notes | CrewAI Traces |

The Manager reads your request, decides which specialist(s) to involve, delegates work, collects results, and gives you a final answer.

---

## What Each Agent Does

### K8s Agent (Kubernetes Specialist)
```
Tools: check_k8s_pod_health, check_k8s_node_health, check_k8s_deployments
       get_pod_crash_logs, get_cluster_events, diagnose_pod

What it checks:
- Are all pods Running? Any CrashLoopBackOff?
- Are nodes Ready? Any resource pressure?
- Do deployments have correct replica count?
- What caused a pod to crash? (reads logs, exit codes)
```

### AWS Agent (Infrastructure Specialist)
```
Tools: check_aws_ec2_health, check_github_workflow_status

What it checks:
- Are all EC2 instances running and healthy?
- Did any GitHub Actions workflows fail recently?
```

### Cost Agent (FinOps Specialist)
```
Tools: find_idle_ec2_instances, find_unattached_ebs_volumes, get_cost_summary

What it checks:
- Any instances running with < 5% CPU? (wasting money)
- Any EBS volumes not attached to anything? (paying for nothing)
- What's the current month AWS bill by service?
```

### Deploy Agent (Deployment Specialist)
```
Tools: check_rollout_status, rollback_deployment, restart_deployment, 
       get_deployment_history

What it does:
- Is a rollout stuck or progressing?
- Roll back to previous version if something broke
- Trigger rolling restart (zero downtime)
```

### Incident Agent (SRE Specialist)
```
Tools: get_pod_crash_logs, get_cluster_events, diagnose_pod

What it does:
- Pull crash logs from failing containers
- Read cluster warning events
- Full diagnosis: exit code + events + interpretation
  (137 = OOMKilled, 1 = app error, 127 = bad entrypoint)
```

### Migration Agent (Platform Specialist)
```
Tools: convert_jenkinsfile_to_github_actions, analyze_jenkinsfile_complexity

What it does:
- Analyze a Jenkinsfile → determine complexity (Tier 1/2/3)
- Generate equivalent GitHub Actions YAML
- Recommend migration strategy
```

---

## How to Demo (Step by Step)

### Demo 1: Run from Terminal
```bash
cd C:\Devops\Repository\devops-microservices-crewAi\app

# Full health check (all agents):
venv/Scripts/python.exe agent.py

# Specific queries:
venv/Scripts/python.exe agent.py "check kubernetes pods"
venv/Scripts/python.exe agent.py "find cost waste"
venv/Scripts/python.exe agent.py "check GitHub Actions status"
```

**What to show**: Manager delegates to the right agent, tools execute, final report generated.

### Demo 2: Web Dashboard
```bash
venv/Scripts/python.exe -m uvicorn web.app:app --reload --port 8000
```
Open: http://localhost:8000

**What to show**: Click "Run Health Check Now", see results populate with pass/fail per component.

### Demo 3: CrewAI Traces (Observability)
After running the agent, go to: https://app.crewai.com → Traces

**What to show**: Click "View Execution" → see the timeline:
- Which agent handled which task
- Tool calls and arguments
- LLM reasoning at each step
- Total execution time

---

## vs LangGraph (Previous Version)

| Aspect | LangGraph (before) | CrewAI (now) |
|--------|-------------------|--------------|
| Orchestration | Manual StateGraph + supervisor node (200+ lines) | Built-in hierarchical process (~50 lines) |
| Agent routing | Custom conditional edges + router function | Manager auto-delegates based on agent roles |
| Tool execution | Manual 5-iteration loop | CrewAI handles automatically |
| Agent collaboration | Not supported natively | Built-in (ask_question_to_coworker, delegate_work_to_coworker) |
| Observability | LangSmith | CrewAI Traces |
| Code complexity | High (state management, edge routing) | Low (define agents + tasks, CrewAI handles the rest) |

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                  CrewAI MULTI-AGENT PLATFORM                     │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────┐                                                │
│  │   Web UI    │  FastAPI Dashboard (localhost:8000)             │
│  │  (FastAPI)  │  Trigger checks, view reports                  │
│  └──────┬──────┘                                                │
│         │                                                       │
│         ▼                                                       │
│  ┌─────────────┐                                                │
│  │   crew.py   │  CrewAI Crew Definition                        │
│  │ (6 agents)  │  Hierarchical Process + Manager LLM            │
│  └──────┬──────┘                                                │
│         │                                                       │
│         ▼                                                       │
│  ┌─────────────────────────────────────────────────────┐        │
│  │              MANAGER AGENT (auto)                    │        │
│  │  Reads task → Decides which specialist → Delegates  │        │
│  └────┬────────┬────────┬────────┬────────┬────────┬───┘        │
│       │        │        │        │        │        │            │
│       ▼        ▼        ▼        ▼        ▼        ▼            │
│    ┌─────┐ ┌─────┐ ┌──────┐ ┌──────┐ ┌────────┐ ┌─────────┐   │
│    │ K8s │ │ AWS │ │Deploy│ │ Cost │ │Incident│ │Migration│   │
│    └──┬──┘ └──┬──┘ └──┬───┘ └──┬───┘ └───┬────┘ └────┬────┘   │
│       │       │       │        │         │           │         │
│       ▼       ▼       ▼        ▼         ▼           ▼         │
│   kubectl   boto3  kubectl   boto3    kubectl    Jenkinsfile   │
│   commands  + GH   rollout   + CW    logs/events  parser      │
│             API    commands   API                               │
│                                                                 │
│  ┌─────────────────────────────────────────────────────┐        │
│  │          OBSERVABILITY                               │        │
│  │  CrewAI Traces (app.crewai.com)                     │        │
│  │  Web Dashboard (localhost:8000)                      │        │
│  │  Terminal verbose output                             │        │
│  └─────────────────────────────────────────────────────┘        │
│                                                                 │
│  ┌─────────────────────────────────────────────────────┐        │
│  │          LLM PROVIDER                                │        │
│  │  Groq (Llama 3.1) via LiteLLM                       │        │
│  │  Can swap to: Azure OpenAI, AWS Bedrock, Ollama     │        │
│  └─────────────────────────────────────────────────────┘        │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## Key Points for Interview

1. **Why CrewAI over LangGraph?**
   - Less code (50 lines vs 200+), built-in delegation, agent collaboration out of the box
   - Manager agent auto-routes — no need to write custom conditional logic
   - Better for teams: define agents by role/goal/backstory, CrewAI handles orchestration

2. **Why multiple agents instead of one big agent?**
   - Each agent has focused context (fewer tokens, faster responses)
   - Tools are isolated (K8s agent can't accidentally call AWS tools)
   - Easier to add new capabilities (add a new agent, no code changes to others)
   - Mirrors real team structure (SRE, FinOps, Platform Eng)

3. **Why Groq?**
   - Free tier for development/POC
   - Extremely fast inference (tokens/second)
   - In production: swap to Azure OpenAI or AWS Bedrock (1-line config change)

4. **What's the monkey patch about?**
   - CrewAI sends Anthropic-style cache headers that Groq doesn't support
   - `litellm_patch.py` strips them before requests hit Groq API
   - In production with Azure/Bedrock, this patch isn't needed

5. **How is this different from just writing a script?**
   - Script: hardcoded steps, no reasoning
   - AI Agent: understands the question, decides what to check, interprets results, provides recommendations
   - Example: "find cost waste" → agent decides to check idle instances AND EBS volumes AND monthly spend, then summarizes findings with dollar amounts

---

## Files That Matter (Quick Reference)

```
app/
├── crew.py              ← THE CORE: agents, tasks, crew definition
├── agent.py             ← Entry point (python agent.py "query")
├── config.py            ← Settings (.env loading)
├── litellm_patch.py     ← Groq compatibility fix
├── tools/
│   ├── k8s_tools.py     ← kubectl commands wrapped as AI tools
│   ├── aws_tools.py     ← boto3 EC2 health check
│   ├── cost_tools.py    ← CloudWatch + Cost Explorer queries
│   ├── deploy_tools.py  ← rollout/rollback/restart
│   ├── incident_tools.py← crash logs + diagnosis
│   └── migration_tools.py← Jenkinsfile converter
└── web/
    └── app.py           ← FastAPI dashboard
```

---

## Setup (One-Time)

```bash
cd C:\Devops\Repository\devops-microservices-crewAi\app
py -3.13 -m venv venv
venv/Scripts/python.exe -m pip install -r requirements.txt
# Edit .env with your keys (GROQ_API_KEY, AWS creds, GITHUB_TOKEN)
# Connect to EKS: aws eks update-kubeconfig --name expense-dev --region us-east-1
venv/Scripts/python.exe agent.py
```

---

## Repo Link
GitHub: github.com/ShivaKrishna44/devops-microservices-crewAi
