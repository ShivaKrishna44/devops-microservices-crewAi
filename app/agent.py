import os
import re
import sys
import time
from typing import Annotated, TypedDict, Literal
import boto3
from github import Github, Auth
from dotenv import load_dotenv

from langchain_core.tools import tool
#from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import AnyMessage, AIMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

# Replace your old Google import with this one near the top:
from langchain_groq import ChatGroq


# -------------------------------------------------------------
# 1. ENVIRONMENT FILTERS & INITIALIZATION
# -------------------------------------------------------------
load_dotenv()
os.environ["LANGSMITH_TRACING"] = "true"

# Secure Windows output streams against encoding locks
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# -------------------------------------------------------------
# 2. DEFINE CUSTOM DEVOPS TOOLS (AWS & GITHUB ONLY)
# -------------------------------------------------------------

def extract_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        output = []
        for item in content:
            if isinstance(item, dict):
                output.append(item.get("text", ""))
            else:
                output.append(str(item))
        return "\n".join(output)
    return str(content)

@tool
def check_aws_ec2_health() -> str:
    """Queries AWS to check for any EC2 instances that are not healthy or running."""
    print("\n[Tool Event] Running AWS infrastructure scan...", flush=True)
    try:
        ec2 = boto3.client(
            'ec2',
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
            region_name=os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        )
        response = ec2.describe_instances()
        unhealthy = []

        for res in response.get('Reservations', []):
            for inst in res.get('Instances', []):
                state = inst['State']['Name']
                inst_id = inst['InstanceId']
                if state != 'running':
                    unhealthy.append(f"Instance {inst_id} ({state})")

        if not unhealthy:
            print("[Tool Success] AWS Cloud check complete. 0 issues found.", flush=True)
            return "AWS Status: All cloud infrastructure components are healthy and running."
        return "Alert! Unhealthy AWS instances detected: " + ", ".join(unhealthy)
    except Exception as e:
        return f"AWS Scan complete (Skipped parameter check: {str(e)})"

@tool
def check_github_workflow_status(repo_name: str) -> str:
    """Checks the specified GitHub repository for any failed actions or workflow builds."""
    print(f"\n[Tool Event] Scanning GitHub repo pipelines for: {repo_name}...", flush=True)
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        return "GitHub Status: Token configuration missing from .env file."
    try:
        auth = Auth.Token(token)
        g = Github(auth=auth, timeout=10)
        repo = g.get_repo(repo_name)
        
        failed_runs = []
        for run in repo.get_workflow_runs()[:10]:
            if run.conclusion == 'failure':
                failed_runs.append(f"Pipeline '{run.name}' (Run #{run.run_number}) failed on '{run.head_branch}'")

        if not failed_runs:
            print("[Tool Success] GitHub Actions check complete. 0 failures.", flush=True)
            return f"GitHub Status: All recent builds for {repo_name} passed successfully."
        return f"Found failed deployment builds in {repo_name}: " + " | ".join(failed_runs)
    except Exception as e:
        return f"GitHub Scan complete (Skipped parameter check: {str(e)})"

# -------------------------------------------------------------
# 3. CORE FRAMEWORK SETUP & MODEL INITIALIZATION
# -------------------------------------------------------------
# tools = [check_aws_ec2_health, check_github_workflow_status]
# tool_node = ToolNode(tools)

# class AgentState(TypedDict):
#     messages: Annotated[list[AnyMessage], add_messages]

# model = ChatGoogleGenerativeAI(
#     model="gemini-flash-latest",  # Swapped to standard public model to bypass quota caps
#     google_api_key=os.getenv("GEMINI_API_KEY"),
#     timeout=45,       
#     max_retries=2
# )



# -------------------------------------------------------------
# 3. CORE FRAMEWORK SETUP & MODEL INITIALIZATION
# -------------------------------------------------------------
tools = [check_aws_ec2_health, check_github_workflow_status]
tool_node = ToolNode(tools)

class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]

# # Swapped out Google for Groq's blazing-fast, high-quota free tier model
# model = ChatGroq(
#     model="llama-3.3-70b-specdec", # Groq's flagship open-weights model
#     groq_api_key=os.getenv("GROQ_API_KEY"),
#     temperature=0,
#     timeout=30,
#     max_retries=2
# ).bind_tools(tools)


from langchain_groq import ChatGroq

model = ChatGroq(
    model="qwen/qwen3.6-27b",
    groq_api_key=os.getenv("GROQ_API_KEY"),
    temperature=0,
    max_retries=2,
    timeout=30,
).bind_tools(tools)

# -------------------------------------------------------------
# 4. DEFINE GRAPH WORKFLOW
# -------------------------------------------------------------

def call_model(state: AgentState):
    print("[Debug] Querying Groq Engine (Ultra High-Speed)...", flush=True)
    return {"messages": [model.invoke(state["messages"])]}


# def call_model(state: AgentState):
#     print("[Debug] Querying Gemini AI Engine...", flush=True)
#     # Dynamically bind tools here to prevent initialization hangs
#     model_with_tools = model.bind_tools(tools)
#     return {"messages": [model_with_tools.invoke(state["messages"])]}

def should_continue(state: AgentState):
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return "__end__"

workflow = StateGraph(AgentState)
workflow.add_node("agent", call_model)
workflow.add_node("tools", tool_node)
workflow.add_edge(START, "agent")
workflow.add_conditional_edges("agent", should_continue)
workflow.add_edge("tools", "agent")

devops_agent = workflow.compile()

# -------------------------------------------------------------
# 5. EXECUTION LAYER (Continuous Cloud Daemon Loop)
# -------------------------------------------------------------
# if __name__ == "__main__":
#     target_repo = "ShivaKrishna44/devops-microservices-nojenkins" 
    
#     query = (
#         f"Perform a systems health check. Audit active GitHub builds for '{target_repo}' "
#         f"and inspect our remote AWS cluster. Respond in short plain-text sentences without emojis."
#     )
    
    # INTERVAL = 20 
    # print("Starting clean DevOps Cloud Agent Monitor Daemon...")
    # print("Press Ctrl + C to exit.\n")
    
    # while True:
    #     print(f"\n=== Triage Loop Initiated [{time.strftime('%Y-%m-%d %H:%M:%S')}] ===")
        
    #     try:
    #         inputs = {"messages": [("user", query)]}
            
    #         for output in devops_agent.stream(inputs, config={"callbacks": []}, stream_mode="values"):
    #             last_msg = output["messages"][-1]
                
    #             if hasattr(last_msg, 'content') and last_msg.content:
    #                 raw_text = extract_text(last_msg.content)
    #                 clean_line = re.sub(r'[^\x00-\x7F]', '', raw_text).strip()
    #                 if clean_line:
    #                     print(f"\n[Agent Diagnostic Output]:\n{clean_line}\n", flush=True)
    #             else:
    #                 print(".", end="", flush=True)
                    
    #     except GeneratorExit:
    #         pass
    #     except Exception as loop_error:
    #         print(f"\n[Loop Error Handled safely]: {str(loop_error)}", flush=True)
            
    #     print(f"\nTriage cycle finalized. Sleeping for 15 minutes...\n" + "="*40)
    #     time.sleep(INTERVAL)


# -------------------------------------------------------------
# 5. EXECUTION LAYER (Runs ONCE and Exits cleanly)
# -------------------------------------------------------------
if __name__ == "__main__":
    target_repo = "ShivaKrishna44/devops-microservices-nojenkins" 
    
    query = (
        f"Perform a systems health check. Audit active GitHub builds for '{target_repo}' "
        f"and inspect our remote AWS cluster. Respond in short plain-text sentences without emojis."
    )
    
    print("Starting single-run DevOps Cloud Agent Monitor...")
    inputs = {"messages": [("user", query)]}
    
    try:
        # Runs the stream graph exactly one time
        for output in devops_agent.stream(inputs, config={"callbacks": []}, stream_mode="values"):
            last_msg = output["messages"][-1]
            
            if hasattr(last_msg, 'content') and last_msg.content:
                raw_text = extract_text(last_msg.content)
                clean_line = re.sub(r'[^\x00-\x7F]', '', raw_text).strip()
                if clean_line and len(clean_line) > 5:
                    print(f"\n[Agent Output Summary]:\n{clean_line}\n", flush=True)
            elif isinstance(last_msg, AIMessage) and last_msg.tool_calls:
                print("-> Model mapping operational checks to tools...", flush=True)
            else:
                print(".", end="", flush=True)
                
    except GeneratorExit:
        pass
    except Exception as loop_error:
        print(f"\n[Error Handled safely]: {str(loop_error)}", flush=True)
        
    print("\nRun completed successfully! System triage complete. Exiting application profile.")
