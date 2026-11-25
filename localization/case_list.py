"""Case-list handling for the localization stages.

The stage scripts expect every line of a case list to carry the five fields
``<syzid>/<mutate1>/<mutate2>/<mutate3>/<mutate4>`` (the mutant ids selected by
the contrastive-reasoning stage).  A plain list of syzids - which is what
``datasets/case_list_all.txt`` and ``datasets/case_list_demo.txt`` contain -
makes ``fl_file`` / ``fl_method`` raise ``IndexError`` for every case, which is
one of the reasons the original pipeline had to be driven step by step with a
hand-prepared ``reproduce.txt``.

``normalize_case_list`` closes that gap: it accepts either layout and always
writes the five-field form.  Missing mutant ids become ``0``, which the stage
scripts interpret as "no mutation hint available" (the prompt then receives
``N/A`` instead of a mutation), i.e. a documented degradation, never invented
data.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

PLACEHOLDER_MUTANTS = ["0", "0", "0", "0"]


def read_case_ids(case_list: Path) -> List[str]:
    """The syzids of a case list, in file order, duplicates removed."""
    ids: List[str] = []
    seen = set()
    with open(case_list, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            case_id = line.split("/")[0].strip()
            if case_id and case_id not in seen:
                seen.add(case_id)
                ids.append(case_id)
    return ids


def normalize_case_list(case_list: Path, output_path: Path) -> Tuple[List[str], int]:
    """Rewrite ``case_list`` into the five-field form.

    Returns ``(case_ids, number_of_cases_without_mutation_hints)``.
    """
    case_ids: List[str] = []
    seen = set()
    rows: List[str] = []
    without_hints = 0
    with open(case_list, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [part.strip() for part in line.split("/")]
            case_id = parts[0]
            if not case_id or case_id in seen:
                continue
            seen.add(case_id)
            case_ids.append(case_id)
            mutants = parts[1:5]
            if len(mutants) < 4 or all(m in ("", "0") for m in mutants):
                without_hints += 1
            mutants = (mutants + PLACEHOLDER_MUTANTS)[:4]
            rows.append("/".join([case_id] + mutants))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(rows) + ("\n" if rows else ""), encoding="utf-8")
    return case_ids, without_hints


def empty_mutation_file(path: Path) -> Path:
    """An empty mutation-result file for runs without the contrastive stage.

    The stage scripts require a ``--mutation_output`` path even when no mutant
    was ever generated; an empty file yields an empty mutation map, so every
    prompt placeholder resolves to ``N/A``.  Nothing is fabricated.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path
