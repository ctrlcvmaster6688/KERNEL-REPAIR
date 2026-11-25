"""Export localization results into the formats the repair half consumes.

Two artifacts are written per run:

``file_func_result_v32.txt``
    The Top-K ``<function>:<file>`` ranking, in exactly the block format the
    repair half parses (``repair main.py::load_task_queue_from_loc_result``)::

        Response for case <syzid>
        1. <function>:<file>
        2. <function>:<file>

    The historical file name is kept because ``paths.LOC_RESULT_FILE`` points
    at it.

``file_func_result_v32.json``
    The same ranking with the root-cause text kept, so the two artifacts and
    the raw function-level output stay traceable to each other.

Input is the function-level stage output: either ``fl_method_result.json``
(``{case_id: {"top_10": [{"rank", "function", "file",
"root_cause_hypothesis"}]}}``) or the raw ``fl_method_result.txt``
(``response for <syzid>:`` blocks followed by the same JSON payload).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Dict, List, Optional

LOC_RESULT_HEADER = "Response for case"

_TXT_BLOCK = re.compile(r"^response for (\w+):\s*$", re.IGNORECASE | re.MULTILINE)


def _clean_items(items) -> List[Dict]:
    cleaned: List[Dict] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        function = str(item.get("function", "")).strip()
        if not function:
            continue
        cleaned.append(
            {
                "rank": len(cleaned) + 1,
                "function": function,
                "file": str(item.get("file", "")).strip(),
                "root_cause_hypothesis": str(item.get("root_cause_hypothesis", "")).strip(),
            }
        )
    return cleaned


def load_from_json(method_json: Path) -> Dict[str, List[Dict]]:
    data = json.loads(Path(method_json).read_text(encoding="utf-8"))
    records: Dict[str, List[Dict]] = {}
    for case_id, payload in data.items():
        top = payload.get("top_10") if isinstance(payload, dict) else None
        items = _clean_items(top)
        if items:
            records[case_id] = items
    return records


def load_from_txt(method_txt: Path) -> Dict[str, List[Dict]]:
    """Parse the raw ``fl_method_result.txt`` produced by the function stage."""
    text = Path(method_txt).read_text(encoding="utf-8", errors="replace")
    matches = list(_TXT_BLOCK.finditer(text))
    records: Dict[str, List[Dict]] = {}
    for index, match in enumerate(matches):
        case_id = match.group(1)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end():end].strip()
        start, stop = body.find("{"), body.rfind("}")
        if start == -1 or stop <= start:
            continue
        try:
            payload = json.loads(body[start:stop + 1])
        except json.JSONDecodeError:
            continue
        items = _clean_items(payload.get("ranked_results") or payload.get("top_10"))
        if items:
            records[case_id] = items
    return records


def load_records(method_json: Optional[Path] = None,
                 method_txt: Optional[Path] = None) -> Dict[str, List[Dict]]:
    """Load localization records, preferring the structured JSON output."""
    if method_json and Path(method_json).exists():
        return load_from_json(method_json)
    if method_txt and Path(method_txt).exists():
        return load_from_txt(method_txt)
    raise FileNotFoundError(
        "No function-level result found: pass --method-json or --method-txt."
    )


def write_loc_result(records: Dict[str, List[Dict]], output_path: Path,
                     order: Optional[List[str]] = None) -> int:
    """Write ``Response for case <syzid>`` blocks; returns the case count."""
    case_ids = [cid for cid in (order or []) if cid in records]
    case_ids += [cid for cid in records if cid not in set(case_ids)]

    lines: List[str] = []
    for case_id in case_ids:
        lines.append(f"{LOC_RESULT_HEADER} {case_id}")
        for item in records[case_id]:
            file_path = item["file"]
            if ":" in file_path:  # the repair parser splits on the first ':'
                continue
            lines.append(f"{item['rank']}. {item['function']}:{file_path}")
        lines.append("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return len(case_ids)


def write_json(records: Dict[str, List[Dict]], output_path: Path,
               order: Optional[List[str]] = None) -> None:
    case_ids = [cid for cid in (order or []) if cid in records]
    case_ids += [cid for cid in records if cid not in set(case_ids)]
    payload = {case_id: {"top_10": records[case_id]} for case_id in case_ids}
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
