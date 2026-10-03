"""
Graph module — DEPRECATED, kept for backward compatibility.
The project now uses CrewAI (see crew.py).

For the new CrewAI-based system:
    from crew import run_devops_crew
"""

from .workflow import build_agent
from .multi_agent import build_multi_agent

__all__ = ["build_agent", "build_multi_agent"]
