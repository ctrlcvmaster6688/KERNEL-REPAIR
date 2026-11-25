"""Root-cause hypotheses -> ``rc_by_rank.jsonl`` (one line per case).

Why this module exists
----------------------
The function-level stage already asks the LLM for a ``root_cause_hypothesis``
next to every ranked function and stores it in ``fl_method_result.json`` /
``fl_method_result.txt``.  Nothing in the original tree ever exported those
hypotheses into the file the repair half reads (``rc_by_rank.jsonl``): there is
no writer for that file in the localization repository, in the repair
repository, nor anywhere else on the machine (only *readers* were found, e.g.
``repair main.py::load_task_queue_from_loc_result``).  The published Top-K file
``file_func_result_v32.txt`` is a flattened ``<function>:<file>`` export and
therefore drops the hypotheses entirely.

This module is the missing exporter.  It is **an added step**, in two tiers:

1. reuse - the hypothesis produced by the function-level stage is copied
   verbatim when it is non-empty (no extra LLM call, the pipeline's own output
   is not rewritten);
2. fallback - for a case/rank with no hypothesis at all, one LLM call is made
   with the crash report plus the Top-K ``<function>:<file>`` list as input,
   using the same LLM client as every other stage (``localization/llm.py``,
   i.e. ``config.Config.LLM_*`` - the repair half's configuration).

Missing text is never invented: a rank stays empty if both tiers yield nothing,
and the caller prints how many hypotheses were produced by which tier.

Output format (the one the repair half parses)::

    {"case_id": "<syzid>", "hypotheses": [
        {"rank": 1, "function_name": "<func>", "hypothesis": "<text>"}, ...]}
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parent.parent
_LOCALIZATION_DIR = Path(__file__).resolve().parent
for _path in (str(_REPO_ROOT), str(_LOCALIZATION_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import settings  # noqa: E402

PROMPT_NAME = "root_cause_prompt.txt"
STANDARD_OUTPUT_NAME = "root_cause_standard_output.txt"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _extract_json(raw: str) -> Optional[dict]:
    if not raw:
        return None
    raw = raw.strip()
    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {"hypotheses": payload}
    except json.JSONDecodeError:
        pass
    start, stop = raw.find("{"), raw.rfind("}")
    if start == -1 or stop <= start:
        return None
    try:
        payload = json.loads(raw[start:stop + 1])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else {"hypotheses": payload}


def _llm_hypotheses(case_id: str, items: List[dict], report_dir: Path,
                    max_functions: int = 10) -> Dict[str, str]:
    """One LLM call: crash report + Top-K functions -> hypothesis per function."""
    from llm import call_text  # imported lazily so --help works without openai

    prompt_template = _read_text(settings.PROMPT_DIR / PROMPT_NAME)
    if not prompt_template:
        return {}
    standard_output = _read_text(settings.PROMPT_DIR / STANDARD_OUTPUT_NAME)

    report_path = report_dir / f"{case_id}.txt"
    report = _read_text(report_path)
    if not report:
        report = "(bug report not available)"

    function_list = "\n".join(
        f"{item['rank']}. {item['function']}:{item['file']}" for item in items[:max_functions]
    )
    prompt = (
        prompt_template.replace("{BUG_REPORT_HERE}", report)
        .replace("{FUNCTION_LIST_HERE}", function_list)
        .replace("{CASE_ID}", case_id)
    )
    raw = call_text(prompt, system_prompt=standard_output or None)
    payload = _extract_json(raw)
    if not payload:
        print(f"[root_cause] {case_id}: LLM answer was not valid JSON, keeping hypotheses empty")
        return {}

    by_function: Dict[str, str] = {}
    for entry in payload.get("hypotheses") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("function_name") or entry.get("function") or "").strip()
        text = str(entry.get("hypothesis") or entry.get("root_cause_hypothesis") or "").strip()
        if name and text:
            by_function[name] = text
    if not by_function:
        print(f"[root_cause] {case_id}: LLM answer had no usable hypotheses")
    return by_function


def build_records(records: Dict[str, List[dict]], report_dir: Path,
                  use_llm: bool = True, max_functions: int = 10,
                  order: Optional[List[str]] = None) -> Tuple[Dict[str, list], Dict[str, int]]:
    """Turn localization records into ``rc_by_rank`` records.

    Returns ``(records_by_case, statistics)`` where statistics counts
    hypotheses reused from the pipeline, generated by the LLM and still empty.
    """
    stats = {"reused": 0, "generated": 0, "empty": 0}
    case_ids = [cid for cid in (order or []) if cid in records]
    case_ids += [cid for cid in records if cid not in set(case_ids)]

    output: Dict[str, list] = {}
    for case_id in case_ids:
        items = records[case_id]
        hypotheses: List[dict] = []
        missing: List[dict] = []
        for item in items:
            text = (item.get("root_cause_hypothesis") or "").strip()
            hypotheses.append(
                {
                    "rank": item["rank"],
                    "function_name": item["function"],
                    "hypothesis": text,
                }
            )
            if text:
                stats["reused"] += 1
            else:
                missing.append(item)

        if missing and use_llm:
            generated = _llm_hypotheses(case_id, items, report_dir, max_functions)
            for entry in hypotheses:
                if not entry["hypothesis"]:
                    text = generated.get(entry["function_name"], "")
                    if text:
                        entry["hypothesis"] = text
                        stats["generated"] += 1

        stats["empty"] += sum(1 for entry in hypotheses if not entry["hypothesis"])
        output[case_id] = hypotheses
    return output, stats


def write_jsonl(records: Dict[str, list], output_path: Path,
                order: Optional[List[str]] = None) -> int:
    case_ids = [cid for cid in (order or []) if cid in records]
    case_ids += [cid for cid in records if cid not in set(case_ids)]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        for case_id in case_ids:
            handle.write(
                json.dumps(
                    {"case_id": case_id, "hypotheses": records[case_id]},
                    ensure_ascii=False,
                )
                + "\n"
            )
    return len(case_ids)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build rc_by_rank.jsonl (per-case root-cause hypotheses) from "
                    "function-level localization results.",
    )
    parser.add_argument("--method-json", default=None,
                        help="fl_method_result.json of the run.")
    parser.add_argument("--method-txt", default=None,
                        help="fl_method_result.txt of the run (used if the JSON is absent).")
    parser.add_argument("--report-dir", default=str(settings.BUG_REPORT_DIR),
                        help="Directory with <syzid>.txt crash reports.")
    parser.add_argument("--output", required=True, help="Path of the rc_by_rank.jsonl to write.")
    parser.add_argument("--no-llm", action="store_true",
                        help="Never call the LLM: only reuse hypotheses already present "
                             "in the function-level result.")
    parser.add_argument("--max-functions", type=int, default=10,
                        help="How many ranked functions to describe per case (default 10).")
    args = parser.parse_args(argv)

    import export  # local module

    records = export.load_records(
        method_json=Path(args.method_json) if args.method_json else None,
        method_txt=Path(args.method_txt) if args.method_txt else None,
    )
    built, stats = build_records(
        records,
        report_dir=Path(args.report_dir),
        use_llm=not args.no_llm,
        max_functions=args.max_functions,
    )
    written = write_jsonl(built, Path(args.output))
    print(f"[root_cause] cases={written} reused={stats['reused']} "
          f"generated={stats['generated']} empty={stats['empty']}")
    print(f"[root_cause] wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
