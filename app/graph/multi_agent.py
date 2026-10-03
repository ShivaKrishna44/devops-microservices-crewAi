"""
Multi-Agent Architecture with Supervisor Pattern
=================================================
Supervisor agent routes tasks to specialized sub-agents:

                    User Query
                        │
                        ▼
                  ┌───────────┐
                  │ SUPERVISOR │  (decides which agent to call)
                  └───────────┘
                        │
         ┌──────────────┼──────────────┬──────────────┐
         ▼              ▼              ▼              ▼
    ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐
    │ K8s     │   │ AWS     │   │ Deploy  │   │ Cost    │
    │ Agent   │   │ Agent   │   │ Agent   │   │ Agent   │
    └─────────┘   └─────────┘   └─────────┘   └─────────┘
         │              │              │              │
    K8s tools      AWS tools     Deploy tools   Cost tools

Each sub-agent has its own LLM + tools (isolated, focused).
Supervisor collects results and produces final summary.
"""

from typing import Annotated, TypedDict, Literal
from langchain_core.messages import AnyMessage, AIMessage, HumanMessage, SystemMessage
from langchain_groq import ChatGroq
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

from config import settings, logger
from tools.aws_tools import check_aws_ec2_health
from tools.github_tools import check_github_workflow_status
from tools.k8s_tools import check_k8s_pod_health, check_k8s_node_health, check_k8s_deployments
from tools.deploy_tools import check_rollout_status, rollback_deployment, restart_deployment, get_deployment_history
from tools.incident_tools import get_pod_crash_logs, get_cluster_events, diagnose_pod
from tools.cost_tools import find_idle_ec2_instances, find_unattached_ebs_volumes, get_cost_summary
from tools.migration_tools import convert_jenkinsfile_to_github_actions, analyze_jenkinsfile_complexity


# ═══════════════════════════════════════════════════════
# State Definition
# ═══════════════════════════════════════════════════════

class MultiAgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    next_agent: str  # Which sub-agent to call next
    results: dict    # Collected results from sub-agents


# ═══════════════════════════════════════════════════════
# Sub-Agent Definitions (each has own tools + system prompt)
# ═══════════════════════════════════════════════════════

K8S_TOOLS = [check_k8s_pod_health, check_k8s_node_health, check_k8s_deployments,
             get_pod_crash_logs, get_cluster_events, diagnose_pod]

AWS_TOOLS = [check_aws_ec2_health, check_github_workflow_status]

DEPLOY_TOOLS = [check_rollout_status, rollback_deployment, restart_deployment, get_deployment_history]

COST_TOOLS = [find_idle_ec2_instances, find_unattached_ebs_volumes, get_cost_summary]

MIGRATION_TOOLS = [convert_jenkinsfile_to_github_actions, analyze_jenkinsfile_complexity]


def _create_llm(tools: list):
    """Create an LLM bound with specific tools."""
    return ChatGroq(
        model=settings.GROQ_MODEL,
        groq_api_key=settings.GROQ_API_KEY,
        temperature=0,
        timeout=30,
        max_retries=2,
    ).bind_tools(tools)


# ═══════════════════════════════════════════════════════
# Supervisor Node — Routes to the right sub-agent
# ═══════════════════════════════════════════════════════

SUPERVISOR_PROMPT = """You are a DevOps platform supervisor agent.
Your job is to analyze the user's request and decide which specialist agent should handle it.

Available agents:
- "k8s_agent": Kubernetes issues (pods, nodes, deployments, DNS, ingress, crashes)
- "aws_agent": AWS + GitHub issues (EC2 health, workflow status, IAM)
- "deploy_agent": Deployment operations (rollout status, rollback, restart)
- "cost_agent": Cost optimization (idle instances, unused volumes, spending)
- "migration_agent": Jenkins to GitHub Actions migration (convert Jenkinsfiles)
- "DONE": All tasks completed, generate final summary

Based on the user request and any results collected so far, respond with ONLY the next agent name.
If the task requires multiple agents, call them one at a time.
When all needed checks are done, respond with "DONE".
"""


def supervisor_node(state: MultiAgentState) -> dict:
    """Supervisor decides which sub-agent to call next."""
    logger.info("Supervisor evaluating next action...")

    llm = ChatGroq(
        model=settings.GROQ_MODEL,
        groq_api_key=settings.GROQ_API_KEY,
        temperature=0,
        timeout=15,
    )

    # Build context with results so far
    results_text = ""
    if state.get("results"):
        results_text = "\n\nResults collected so far:\n"
        for agent_name, result in state["results"].items():
            results_text += f"- {agent_name}: {result[:200]}\n"

    messages = [
        SystemMessage(content=SUPERVISOR_PROMPT),
        HumanMessage(content=f"User request: {state['messages'][0].content if state['messages'] else 'health check'}{results_text}\n\nWhich agent should handle this next? Reply with ONLY the agent name."),
    ]

    response = llm.invoke(messages)
    next_agent = response.content.strip().lower().replace('"', '').replace("'", "")

    # Normalize the response
    valid_agents = ["k8s_agent", "aws_agent", "deploy_agent", "cost_agent", "migration_agent", "done"]
    if next_agent not in valid_agents:
        # Try to find a match
        for va in valid_agents:
            if va.replace("_agent", "") in next_agent:
                next_agent = va
                break
        else:
            next_agent = "done"

    logger.info("Supervisor decision: route to → %s", next_agent)
    return {"next_agent": next_agent}


def supervisor_router(state: MultiAgentState) -> str:
    """Routes to the next agent based on supervisor's decision."""
    return state.get("next_agent", "done")


# ═══════════════════════════════════════════════════════
# Sub-Agent Nodes (each runs its own tools)
# ═══════════════════════════════════════════════════════

def _run_sub_agent(state: MultiAgentState, tools: list, system_prompt: str, agent_name: str) -> dict:
    """Generic sub-agent runner — calls LLM with tools, loops until done."""
    import time
    logger.info("Running sub-agent: %s", agent_name)
    time.sleep(2)  # Rate limit protection for Groq free tier

    llm = _create_llm(tools)
    tool_node = ToolNode(tools)

    # Create sub-agent query from original user message
    user_msg = state["messages"][0].content if state["messages"] else "check health"
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_msg),
    ]

    # Run tool loop (max 5 iterations to prevent infinite loops)
    for _ in range(5):
        response = llm.invoke(messages)
        messages.append(response)

        if isinstance(response, AIMessage) and response.tool_calls:
            # Execute tools
            tool_results = tool_node.invoke({"messages": messages})
            messages.extend(tool_results["messages"])
        else:
            # No more tools needed — we have the answer
            break

    # Extract final answer
    result = response.content if hasattr(response, "content") else str(response)
    logger.info("Sub-agent %s completed. Result length: %d", agent_name, len(result))

    # Store result
    results = state.get("results", {})
    results[agent_name] = result

    return {"results": results}


def k8s_agent_node(state: MultiAgentState) -> dict:
    # Quick check: is kubectl even reachable?
    import subprocess
    try:
        result = subprocess.run("kubectl cluster-info", shell=True, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            results = state.get("results", {})
            results["k8s_agent"] = "K8s: Cluster unreachable (kubectl not connected). Skipped."
            logger.warning("K8s cluster unreachable, skipping K8s agent.")
            return {"results": results}
    except Exception:
        results = state.get("results", {})
        results["k8s_agent"] = "K8s: kubectl not available. Skipped."
        return {"results": results}

    return _run_sub_agent(
        state, K8S_TOOLS,
        "You are a Kubernetes specialist. Check pod health, node health, and deployments. Report any issues found. Be concise.",
        "k8s_agent"
    )


def aws_agent_node(state: MultiAgentState) -> dict:
    return _run_sub_agent(
        state, AWS_TOOLS,
        "You are an AWS infrastructure specialist. Check EC2 health and GitHub Actions status. Report any issues. Be concise.",
        "aws_agent"
    )


def deploy_agent_node(state: MultiAgentState) -> dict:
    return _run_sub_agent(
        state, DEPLOY_TOOLS,
        "You are a deployment specialist. Check rollout status of deployments. Report if anything is stuck or failing. Be concise.",
        "deploy_agent"
    )


def cost_agent_node(state: MultiAgentState) -> dict:
    return _run_sub_agent(
        state, COST_TOOLS,
        "You are a cost optimization specialist. Find idle EC2 instances, unattached EBS volumes, and get cost summary. Report any waste. Be concise.",
        "cost_agent"
    )


def migration_agent_node(state: MultiAgentState) -> dict:
    return _run_sub_agent(
        state, MIGRATION_TOOLS,
        "You are a migration specialist. Analyze Jenkinsfiles and convert them to GitHub Actions. Be concise.",
        "migration_agent"
    )


# ═══════════════════════════════════════════════════════
# Summary Node — Collects all results into final report
# ═══════════════════════════════════════════════════════

def summary_node(state: MultiAgentState) -> dict:
    """Generates final summary from all sub-agent results."""
    logger.info("Generating final summary...")

    results = state.get("results", {})
    if not results:
        return {"messages": [AIMessage(content="No results collected. Please specify what to check.")]}

    summary_parts = ["=== Multi-Agent Infrastructure Report ===\n"]
    for agent_name, result in results.items():
        display_name = agent_name.replace("_agent", "").upper()
        summary_parts.append(f"[{display_name}]\n{result}\n")

    summary = "\n".join(summary_parts)
    return {"messages": [AIMessage(content=summary)]}


# ═══════════════════════════════════════════════════════
# Build the Multi-Agent Graph
# ═══════════════════════════════════════════════════════

def build_multi_agent():
    """Builds and compiles the multi-agent LangGraph workflow."""
    workflow = StateGraph(MultiAgentState)

    # Add nodes
    workflow.add_node("supervisor", supervisor_node)
    workflow.add_node("k8s_agent", k8s_agent_node)
    workflow.add_node("aws_agent", aws_agent_node)
    workflow.add_node("deploy_agent", deploy_agent_node)
    workflow.add_node("cost_agent", cost_agent_node)
    workflow.add_node("migration_agent", migration_agent_node)
    workflow.add_node("summary", summary_node)

    # Entry point
    workflow.add_edge(START, "supervisor")

    # Supervisor routes to sub-agents
    workflow.add_conditional_edges("supervisor", supervisor_router, {
        "k8s_agent": "k8s_agent",
        "aws_agent": "aws_agent",
        "deploy_agent": "deploy_agent",
        "cost_agent": "cost_agent",
        "migration_agent": "migration_agent",
        "done": "summary",
    })

    # After each sub-agent completes, go back to supervisor (for next decision)
    workflow.add_edge("k8s_agent", "supervisor")
    workflow.add_edge("aws_agent", "supervisor")
    workflow.add_edge("deploy_agent", "supervisor")
    workflow.add_edge("cost_agent", "supervisor")
    workflow.add_edge("migration_agent", "supervisor")

    # Summary is the final node
    workflow.add_edge("summary", END)

    return workflow.compile()
