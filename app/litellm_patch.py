"""
Monkey patch for litellm to work with Groq via CrewAI.
CrewAI/LiteLLM injects unsupported parameters that Groq rejects.
This strips them at the litellm.completion level before they reach the API.
"""

import litellm
import copy

_original_completion = litellm.completion

# Parameters that Groq does not support
UNSUPPORTED_KWARGS = [
    "is_litellm",
    "cache_control",
    "cache_breakpoint",
    "enable_cache",
    "prompt_caching",
]


def _clean_messages(messages: list) -> list:
    """Remove unsupported properties from messages before sending to Groq."""
    if not messages:
        return messages

    cleaned = []
    for msg in messages:
        msg = copy.deepcopy(msg)
        # Remove unsupported top-level message properties
        for key in ["cache_control", "cache_breakpoint", "enable_cache"]:
            msg.pop(key, None)
        # If content is a list of blocks, clean each block
        if isinstance(msg.get("content"), list):
            new_content = []
            for block in msg["content"]:
                if isinstance(block, dict):
                    block = {k: v for k, v in block.items()
                             if k not in ("cache_control", "cache_breakpoint")}
                new_content.append(block)
            msg["content"] = new_content
        cleaned.append(msg)
    return cleaned


def _patched_completion(*args, **kwargs):
    """Strip all unsupported parameters before sending to Groq."""
    # Remove unsupported top-level kwargs
    for key in UNSUPPORTED_KWARGS:
        kwargs.pop(key, None)

    # Also check for any extra_body or metadata that might contain these
    if "extra_body" in kwargs and isinstance(kwargs["extra_body"], dict):
        for key in UNSUPPORTED_KWARGS:
            kwargs["extra_body"].pop(key, None)
        if not kwargs["extra_body"]:
            del kwargs["extra_body"]

    # Clean messages
    if "messages" in kwargs:
        kwargs["messages"] = _clean_messages(kwargs["messages"])

    return _original_completion(*args, **kwargs)


def apply_patch():
    """
    Apply monkey patch to litellm.completion.
    Call this once at startup before crew.kickoff().
    """
    litellm.completion = _patched_completion
