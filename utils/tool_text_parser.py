"""Fallback parser for tool calls written as text markup instead of JSON.

Most OpenAI-compatible endpoints return tool calls in the ``tool_calls`` field,
but some chat templates (e.g. DeepSeek's ``deepseek-flash``) emit the raw template
syntax into ``message.content``::

    <｜｜DSML｜｜ calls>
    <｜｜DSML｜｜ invoke name="read_kernel_file">
    <｜｜DSML｜｜ parameter name="file_path" string="true">crypto/arc4.c</｜｜DSML｜｜ parameter>
    </｜｜DSML｜｜ invoke>
    </｜｜DSML｜｜ calls>

The agent loops expect ``{"tool_call": {"name": ..., "params": {...}}}``; this
module converts that markup into the same shape, so such models can still drive
the ReAct loop.  It is deliberately tag-name agnostic: any ``<... invoke ...>`` /
``<... parameter ...>`` pair inside a block mentioning ``DSML`` is accepted.
"""

from __future__ import annotations

import html
import re
from typing import Any, Dict, List

__all__ = ["parse_text_tool_calls", "has_text_tool_calls"]

_MARK = r"(?:DSML|\uff5c\uff5cDSML\uff5c\uff5c)"

_INVOKE_RE = re.compile(
    r"<(?:[^<>]*?)" + _MARK + r"(?:[^<>]*?)\binvoke\b(?P<attrs>[^<>]*)>"
    r"(?P<body>.*?)"
    r"(?:</(?:[^<>]*?)" + _MARK + r"(?:[^<>]*?)\binvoke\s*>|\Z)",
    re.S | re.I,
)
_PARAM_RE = re.compile(
    r"<(?:[^<>]*?)" + _MARK + r"(?:[^<>]*?)\bparameter\b(?P<attrs>[^<>]*)>"
    r"(?P<value>.*?)"
    r"(?:</(?:[^<>]*?)" + _MARK + r"(?:[^<>]*?)\bparameter\s*>|\Z)",
    re.S | re.I,
)
_ATTR_RE = re.compile(r'(?P<key>[A-Za-z_][\w-]*)\s*=\s*"(?P<val>[^"]*)"')


def _attrs(raw: str) -> Dict[str, str]:
    return {m.group("key"): m.group("val") for m in _ATTR_RE.finditer(raw or "")}


def _clean(value: str) -> str:
    text = html.unescape(value or "").strip()
    # drop a stray closing tag that the simplistic regex may have swallowed
    if text.endswith(">"):
        text = re.sub(r"</[^<>]*>\s*$", "", text).strip()
    return text


def parse_text_tool_calls(text: str) -> List[Dict[str, Any]]:
    """Return ``[{"name": str, "params": dict}, ...]`` parsed from markup text."""
    if not text or "invoke" not in text:
        return []

    calls: List[Dict[str, Any]] = []
    for match in _INVOKE_RE.finditer(text):
        attrs = _attrs(match.group("attrs"))
        name = (attrs.get("name") or attrs.get("tool") or "").strip()
        if not name:
            continue

        params: Dict[str, Any] = {}
        body = match.group("body") or ""
        for param in _PARAM_RE.finditer(body):
            p_attrs = _attrs(param.group("attrs"))
            p_name = (p_attrs.get("name") or "").strip()
            if not p_name:
                continue
            raw_value = _clean(param.group("value"))
            # best-effort typing: ints/floats/bools stay strings unless obvious
            if p_attrs.get("string", "").lower() in ("false", "0"):
                try:
                    raw_value = int(raw_value)
                except ValueError:
                    try:
                        raw_value = float(raw_value)
                    except ValueError:
                        pass
            params[p_name] = raw_value

        calls.append({"name": name, "params": params})

    return calls


def has_text_tool_calls(text: str) -> bool:
    return bool(parse_text_tool_calls(text))
