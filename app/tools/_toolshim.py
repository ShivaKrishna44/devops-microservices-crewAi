"""
Tool decorator shim.

In production, tools use CrewAI's `@tool`. For unit testing the pure logic
without installing CrewAI, we fall back to a no-op decorator that leaves the
function callable directly AND exposes it via `.func` (matching how tests and
main.py invoke tools). This keeps the safety logic testable with zero heavy deps.
"""
try:
    from crewai.tools import tool  # type: ignore
except Exception:  # noqa: BLE001 - CrewAI not installed (e.g. test env)
    def tool(_name=None, *args, **kwargs):
        """No-op stand-in for crewai.tools.tool.

        Supports both @tool and @tool("Name") usage. Returns the original
        function with a `.func` attribute pointing at itself, so callers using
        `getattr(t, "func", t)` work identically to the real wrapper.
        """
        def _decorate(fn):
            try:
                fn.func = fn  # mimic BaseTool.func access used by callers
            except (AttributeError, TypeError):
                pass
            return fn

        # Called as @tool (bare) — _name is the function.
        if callable(_name):
            return _decorate(_name)
        # Called as @tool("Name") — return the decorator.
        return _decorate
