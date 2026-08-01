# DevOps AI Agent Execution Flow

## Overview
This document explains how the LangGraph-powered DevOps AI Agent processes a user request, invokes external tools, and generates a consolidated infrastructure health report.

---

## High-Level Architecture

```text
                 User Request
                      │
                      ▼
             LangGraph Agent Node
                      │
          ┌───────────┴────────────┐
          │                        │
   Needs Tool?               No Tool Needed
          │                        │
          ▼                        ▼
     Tool Router             Final Response
          │
          ▼
  GitHub / AWS / Docker / ...
          │
          ▼
      Tool Response
          │
          ▼
     LangGraph Agent
          │
          ▼
     Final Summary
```

---

## Step-by-Step Walkthrough

### Step 1 – User Prompt
The monitoring application starts by creating a system request:
- Perform a systems health check.
- Audit GitHub workflows.
- Inspect AWS infrastructure.
- Generate a concise health report.

**Terminal Execution:**
```bash
=== Triage Loop Initiated ===
```

### Step 2 – LangGraph Receives Request
The request is sent to the LangGraph workflow graph engine:
```text
START ──> Agent Node
```
- The LLM analyzes the request payload.
- Instead of answering immediately, it determines that external information is required to satisfy the operational constraints.

### Step 3 – Tool Selection
The LLM decides which tool should be executed first.
- **Tool Selected:** `check_github_workflow_status()`
- **LangSmith Structural Trace:** `AI` ──> `Tool Call` (`check_github_workflow_status`)

### Step 4 – GitHub Tool Execution
The GitHub tool connects directly to the GitHub REST API using the following sequence:
1. Authenticate using Personal Access Token.
2. Retrieve the last 10 GitHub Actions workflow runs.
3. Check workflow conclusions.
4. Detect failures.

**Terminal Output:**
```bash
Scanning GitHub repo pipelines...
GitHub Actions check complete. 0 failures.
```

**Tool Output String:**
```text
GitHub Status: All recent builds passed successfully.
```

### Step 5 – LangGraph Re-evaluates State
After receiving the GitHub result, LangGraph appends the message and loops execution back to the LLM node.
- **Current State Memory:** `User Prompt` + `GitHub Status`
- The model evaluates the updated thread history and determines that AWS status information is still required.

### Step 6 – AWS Tool Invocation
LangGraph routes the next graph turn execution straight to the AWS endpoint vector:
- **Target Node:** `check_aws_ec2_health()`

**Terminal Status:**
```bash
Running AWS infrastructure scan...
```

**Operations Lifecycle:**
1. Connect to AWS EC2 endpoint wrapper.
2. List available instances.
3. Verify instance execution states.
4. Detect unhealthy infrastructure resources.

**Result Matrix:**
```bash
AWS Cloud check complete. 0 issues found.
```

### Step 7 – State Update
The core conversational memory graph state now holds the complete dataset:
- `User Prompt` + `GitHub Result` + `AWS Result`

LangGraph queries the conditional edge router function: **Do we still need more tools?**
- **Evaluation Result:** `No`
- **Workflow Edge Resolution:** `should_continue()` ──> `END`

### Step 8 – Final Response Generation
The LLM consumes the compiled tool context from state memory and flattens it into a single clean operational text summary.
- **Generated Summary:** All recent GitHub builds passed successfully. AWS infrastructure is healthy and running.

**Terminal Finalization:**
```bash
Run completed successfully. System triage complete.
```

---

## LangGraph Execution Trace

```text
  START
    │
    ▼
  Agent
    │
    ▼
Tool Decision ──> GitHub Tool ──> Agent
    │
    ▼
Tool Decision ──> AWS Tool ──> Agent
    │
    ▼
   END
```

---

## LangSmith Trace Explanation

Each transaction trace block in LangSmith represents one explicit graph execution frame:

| Stage | Description |
| :--- | :--- |
| **User** | Initial system monitoring context request prompt. |
| **AI** | LLM orchestrator core reasoning state. |
| **Tool Call** | LLM structural request to execute an external tool. |
| **Tool** | Python execution script executing live queries. |
| **AI** | Model consuming and processing tool result arrays. |
| **should_continue** | Conditional graph edge determining step directions. |
| **END** | Workflow execution route finalized and closed. |

---

## Key Benefits of LangGraph
- **Deterministic Control Workflows:** Guarantees strict loop routing paths.
- **Dynamic Tool Selection:** Models determine vectors at run-time based on state data.
- **Stateful Conversations:** Appends chat logs natively using custom channel reducers.
- **Modular Integration:** New DevOps vectors can be appended as standard functions.
- **Streamlined Debugging:** Out-of-the-box system tracing and logging via LangSmith.
- **Human-in-the-Loop Capable:** Supports workflow interrupts for authorization check gates.

---

## Current Tool Matrix Implementation

| Category | Tool | What it does |
| :--- | :--- | :--- |
| **AWS** | `check_aws_ec2_health()` | Finds EC2 instances that are not running/healthy |
| **GitHub** | `check_github_workflow_status(repo)` | Checks last 10 workflow runs for failures |
| **Kubernetes** | `check_k8s_pod_health(namespace)` | Finds pods NOT in Running/Completed state |
| **Kubernetes** | `check_k8s_node_health()` | Finds nodes in NotReady state |
| **Kubernetes** | `check_k8s_deployments(namespace)` | Finds deployments with missing replicas |
| **Deployment** | `check_rollout_status(deployment, ns)` | Is rollout complete, progressing, or stuck? |
| **Deployment** | `rollback_deployment(deployment, ns)` | Rolls back to previous version |
| **Deployment** | `get_deployment_history(deployment, ns)` | Shows revision history |
| **Deployment** | `restart_deployment(deployment, ns)` | Rolling restart (zero downtime) |
| **Incident** | `get_pod_crash_logs(pod, ns)` | Gets logs from crashed container (--previous) |
| **Incident** | `get_cluster_events(ns)` | Shows recent Warning events |
| **Incident** | `diagnose_pod(pod, ns)` | Full diagnosis: status + exit code + events |
| **Migration** | `convert_jenkinsfile_to_github_actions(content)` | Generates GHA YAML from Jenkinsfile |
| **Migration** | `analyze_jenkinsfile_complexity(content)` | Determines Tier 1/2/3 migration effort |
| **Cost** | `find_idle_ec2_instances()` | Finds instances with < 5% CPU (wasting money) |
| **Cost** | `find_unattached_ebs_volumes()` | Finds EBS volumes not attached to anything |
| **Cost** | `get_cost_summary()` | Current month AWS spend by service |
