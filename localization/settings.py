"""Path selection for the localization half.

Absolute filesystem paths are declared **only** in the repository-level
``paths.py``.  This module selects the ones the localization stages need and
derives the per-run directories from them; no literal absolute path may appear
here (or in any other localization module).

Every value can be overridden from the command line of ``localization/main.py``
(``--report-dir``, ``--testcase-dir``, ``--work-dir``, ``--commit-map`` ...),
these constants are just the defaults.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import paths  # noqa: E402  (single source of truth for every absolute path)

# ---------------------------------------------------------------------------
# Resources shipped inside this package
# ---------------------------------------------------------------------------
LOCALIZATION_DIR = Path(paths.LOCALIZATION_DIR)
PROMPT_DIR = Path(paths.LOC_PROMPT_DIR)
# RankNet used to pre-filter candidate functions before the function-level LLM.
FILTER_MODEL_PATH = Path(paths.LOC_FILTER_MODEL_PATH)
# tree-sitter C grammar; the repair half uses the very same shared object.
LANG_LIBRARY_PATH = Path(paths.LANG_SO_PATH)

# ---------------------------------------------------------------------------
# Task dataset (shared with the repair half)
# ---------------------------------------------------------------------------
DATASETS_DIR = Path(paths.DATASETS_DIR)
BUG_REPORT_DIR = Path(paths.BUG_REPORT_DIR)
TESTCASE_DIR = Path(paths.TESTCASE_DIR)
BUG_CONFIG_DIR = Path(paths.BUG_CONFIG_DIR)
COMMIT_FILE = Path(paths.COMMIT_FILE)
# Crash keyword oracle, used by the mutant-reproduction stage to decide whether
# a runtime log still shows the original crash.
ORACLE_FILE = Path(paths.ORACLE_FILE)
# Localization-only inputs: default batch case list and the two ground-truth
# files used by the optional evaluation stage.
CASE_LIST_FILE = Path(paths.LOC_CASE_LIST_FILE)
ORACLE_FILE_LIST = Path(paths.LOC_ORACLE_FILE_LIST)
ORACLE_FUNC_LIST = Path(paths.LOC_ORACLE_FUNC_LIST)

# ---------------------------------------------------------------------------
# External trees / pools (only the optional mutant-reproduction stage needs
# these; the pure localization stages need just the kernel tree)
# ---------------------------------------------------------------------------
KERNEL_DIR = Path(paths.KERNEL_DIR_ORI)
CACHE_ROOT = Path(paths.CACHE_ROOT)
IMAGE_PATH = Path(paths.QEMU_IMAGE_PATHS[0]).parent
SYZKALLER_PATH = Path(paths.SYZKALLER_WORKER_DIRS[0])
# Paths **inside** the QEMU guest (not on the host).
GUEST_HOME_DIR = paths.GUEST_HOME_DIR
GUEST_REPRO_FILE = paths.GUEST_REPRO_FILE

# ---------------------------------------------------------------------------
# Outputs
# ---------------------------------------------------------------------------
OUTPUT_ROOT = Path(paths.LOCALIZATION_OUTPUT_DIR)

# Exact files the repair half reads (see paths.py).  ``--publish`` copies the
# two exported artifacts to these locations so that localization -> repair can
# be chained without any manual copying.
PUBLISHED_LOC_RESULT = Path(paths.LOC_RESULT_FILE)
PUBLISHED_ROOT_CAUSE = Path(paths.ROOT_CAUSE_JSONL)


def default_output_dir(prefix: str = "run") -> Path:
    """A fresh ``<OUTPUT_ROOT>/<prefix>-<UTC timestamp>`` directory."""
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    return OUTPUT_ROOT / f"{prefix}-{stamp}"
