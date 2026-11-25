"""Central definition of every filesystem path used by CoHiKer++.

This module is the single source of truth for paths.  Every other module
imports the constants from here; no other file in the repository hard-codes a
path.  When the data / kernel trees / worker pools are relocated, only the
values below (or the matching ``COHIKER_*`` environment variables) have to be
edited.

Resolution rules
----------------
1. Everything **shipped with the package** lives under ``<repo>/data`` and is
   addressed relatively, so the package keeps working after being copied or
   unzipped anywhere.
2. Everything **external** (kernel trees, QEMU pool, caches) has an absolute
   default plus a ``COHIKER_*`` environment override.  Output directories fall
   back to ``<repo>/runs/...`` when their absolute default does not exist, so a
   fresh machine does not need ``/data`` to be writable.
3. ``DATASETS_DIR`` prefers ``COHIKER_DATASETS_DIR``, then ``/data/datasets``,
   then the bundled mirror ``<repo>/data/datasets``.

Bundled knowledge bases (all relative to ``data/``)
---------------------------------------------------
``kernel_patch_final_rag_v3.db``   case-level semantic KB   (93 MB, SQLite)
``kernel_bm25_v3.idx``             BM25 index of the above (4.9 MB, pickle)
``golden_kernel_test_v3.db``       golden-patch KB          (273 MB, SQLite)
``final_kernel_master_vocab_v4.json``  vocabulary           (15 KB)
``my-languages.so``                tree-sitter C grammar    (0.7 MB)
"""

import os

# ---------------------------------------------------------------------------
# Environment overrides
# ---------------------------------------------------------------------------


def _env(name, default):
    """Return ``$name`` when set and non-empty, else ``default``."""
    value = os.environ.get(name)
    return value if value else default


def _env_list(name, default):
    """Comma-separated ``$name`` -> list of non-empty items; unset -> ``default``.

    Used for the worker pool, whose four lists are parallel and therefore have to
    be overridden together (``_validate_worker_pool`` checks that).
    """
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def _load_dotenv(path):
    """Load ``KEY=VALUE`` lines from ``path`` into ``os.environ``.

    The package is configured entirely through environment variables; reading a
    single ``<repo>/.env`` here is what makes "copy ``.env.example``, fill it in,
    run" enough in practice.  Already-exported variables always win, so a shell
    override still behaves as expected.  No third-party dependency.
    """
    if not os.path.isfile(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if value[:1] in ("'", '"'):
                    # quoted value: take the inside verbatim, ignore anything after
                    quote = value[0]
                    end = value.find(quote, 1)
                    value = value[1:end] if end != -1 else value[1:]
                else:
                    # drop a trailing "  # comment"; a '#' glued to the value is
                    # kept, so paths/URLs containing '#' still work
                    for idx, char in enumerate(value):
                        if char == "#" and (idx == 0 or value[idx - 1] in " \t"):
                            value = value[:idx].rstrip()
                            break
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass


def _output_dir(env_name, default, fallback_name):
    """env override -> absolute default (when writable parent exists) -> <repo>/runs/..."""
    value = os.environ.get(env_name)
    if value:
        return value
    if os.path.isdir(os.path.dirname(default)):
        return default
    return os.path.join(REPO_ROOT, "runs", fallback_name)


# ---------------------------------------------------------------------------
# Repository layout
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(REPO_ROOT, "data")

# One file is enough: copy ``.env.example`` to ``.env``, fill it in, run.  This
# must happen before any constant below reads the environment.  Variables that
# are already exported take precedence over the file.
_load_dotenv(os.path.join(REPO_ROOT, ".env"))

# ---------------------------------------------------------------------------
# Knowledge bases bundled with this package (see data/)
# ---------------------------------------------------------------------------
# Case-level semantic knowledge base (SQLite) and its BM25 companion index.
DB_PATH = os.path.join(DATA_DIR, "kernel_patch_final_rag_v3.db")
BM25_PATH = os.path.join(DATA_DIR, "kernel_bm25_v3.idx")
# Tree-sitter C grammar used to parse kernel sources (repair and localization).
LANG_SO_PATH = os.path.join(DATA_DIR, "my-languages.so")
# Vocabulary used by the golden-patch retriever.
VOCAB_PATH = os.path.join(DATA_DIR, "final_kernel_master_vocab_v4.json")
# Golden-patch knowledge base: 21 132 (commit, intent, patch) rows with their
# intent embeddings.  Read by skill/kernel_fix_expert.py.
GOLDEN_DB_PATH = os.path.join(DATA_DIR, "golden_kernel_test_v3.db")

# ---------------------------------------------------------------------------
# Task datasets
# ---------------------------------------------------------------------------
# Precedence: COHIKER_DATASETS_DIR -> /data/datasets -> bundled mirror.
# The bundled mirror holds the per-case inputs and one snapshot of the
# localization results, which is what makes the "repair without re-localizing"
# quick start possible (see README).
DATASETS_DIR = _env("COHIKER_DATASETS_DIR", "")
if not DATASETS_DIR:
    _external_datasets = "/data/datasets"
    DATASETS_DIR = _external_datasets if os.path.isdir(_external_datasets) else os.path.join(DATA_DIR, "datasets")

# Root-cause hypotheses, one JSON object per line.  Written by
# localization/root_cause.py; a snapshot is shipped in the bundled mirror.
ROOT_CAUSE_JSONL = os.path.join(DATASETS_DIR, "rc_by_rank.jsonl")
# Top-K localization results.  Written by localization/export.py; a snapshot is
# shipped in the bundled mirror.
LOC_RESULT_FILE = os.path.join(DATASETS_DIR, "file_func_result_v32.txt")
COMMIT_FILE = os.path.join(DATASETS_DIR, "case_commit.txt")
SYZ_BRANCH_FILE = os.path.join(DATASETS_DIR, "syz_branch.txt")
ORACLE_FILE = os.path.join(DATASETS_DIR, "oracle_content.txt")
BUG_REPORT_DIR = os.path.join(DATASETS_DIR, "bug_report")
TESTCASE_DIR = os.path.join(DATASETS_DIR, "testcases")
BUG_CONFIG_DIR = os.path.join(DATASETS_DIR, "bug_config")

# ---------------------------------------------------------------------------
# Kernel source trees and build cache
# ---------------------------------------------------------------------------
KERNEL_DIR_ORI = _env("COHIKER_KERNEL_DIR_ORI", "/data/linux-ori")
CACHE_ROOT = _env("COHIKER_CACHE_ROOT", "/data/cache_linux")

# Legacy build paths recorded inside .cmd files.  KbuildManager rewrites these
# stale absolute paths after a build snapshot has been relocated, so this list
# must cover every location a worker tree may have been built at.
KERNEL_LEGACY_DIR = "/data/linux"
KERNEL_WORKER_DIR_PATTERN = _env("COHIKER_KERNEL_WORKER_PATTERN", "/data/linux-worker-{index}")
KERNEL_MAX_WORKERS = int(_env("COHIKER_KERNEL_MAX_WORKERS", "10"))
KERNEL_PATH_CANDIDATES = [KERNEL_LEGACY_DIR] + [
    KERNEL_WORKER_DIR_PATTERN.format(index=i) for i in range(KERNEL_MAX_WORKERS)
]

# ---------------------------------------------------------------------------
# Validation worker pool (kernel tree, syzkaller checkout, QEMU image per worker)
# ---------------------------------------------------------------------------
# One entry per worker; the four lists are parallel (index i belongs to the same
# worker) and are read from comma-separated environment variables.  Defaults keep
# the original three-worker layout.
_DEFAULT_WORKER_COUNT = 3
KERNEL_WORKER_DIRS = _env_list(
    "COHIKER_KERNEL_WORKER_DIRS",
    ["/data/linux-worker-%d" % i for i in range(_DEFAULT_WORKER_COUNT)],
)
SYZKALLER_WORKER_DIRS = _env_list(
    "COHIKER_SYZKALLER_WORKER_DIRS",
    ["/data/syzkaller_worker_%d" % i for i in range(_DEFAULT_WORKER_COUNT)],
)
QEMU_IMAGE_PATHS = _env_list(
    "COHIKER_QEMU_IMAGE_PATHS",
    ["/data/qemu-worker-%d/bullseye.img" % i for i in range(_DEFAULT_WORKER_COUNT)],
)
QEMU_SSH_KEYS = _env_list(
    "COHIKER_QEMU_SSH_KEYS",
    ["/data/qemu-worker-%d/bullseye.id_rsa" % i for i in range(_DEFAULT_WORKER_COUNT)],
)


def _validate_worker_pool():
    """Fail loudly on an inconsistent worker pool instead of truncating it.

    Runs at import time: ``paths`` is imported by ``config`` and by every entry
    point, so a misconfigured pool stops the program before any work starts.
    """
    pool = {
        "COHIKER_KERNEL_WORKER_DIRS": KERNEL_WORKER_DIRS,
        "COHIKER_SYZKALLER_WORKER_DIRS": SYZKALLER_WORKER_DIRS,
        "COHIKER_QEMU_IMAGE_PATHS": QEMU_IMAGE_PATHS,
        "COHIKER_QEMU_SSH_KEYS": QEMU_SSH_KEYS,
    }
    empty = [name for name, values in pool.items() if not values]
    if empty:
        raise ValueError(
            "worker pool is empty for: " + ", ".join(empty)
            + ". Each variable must list at least one absolute path."
        )
    lengths = {name: len(values) for name, values in pool.items()}
    if len(set(lengths.values())) != 1:
        detail = ", ".join(f"{name}={length}" for name, length in lengths.items())
        raise ValueError(
            "worker pool lists must all have the same length (index i is one "
            f"worker, index -1 is not padded); got: {detail}. "
            "Set all four COHIKER_* worker variables to the same number of "
            "comma-separated entries."
        )


_validate_worker_pool()

# Paths inside the QEMU guest (not on the host).
GUEST_HOME_DIR = "/root"
GUEST_REPRO_FILE = "/root/repro.txt"

# ---------------------------------------------------------------------------
# Outputs and logs
# ---------------------------------------------------------------------------
LOG_DIR = _output_dir("COHIKER_LOG_DIR", "/data/logs_new_v32", "logs_new_v32")
PATCH_ARCHIVE_DIR = _output_dir(
    "COHIKER_PATCH_ARCHIVE_DIR", "/data/output_patches_v32/patches", "output_patches_v32/patches"
)

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
# Value used by the __main__ smoke block of agent/fix_thread_graph.py.
DEMO_BUG_CONFIG_PATH = "/data/config"

# ---------------------------------------------------------------------------
# Fault-localization half (localization/)
# ---------------------------------------------------------------------------
# Module home and the prompt templates used by its stages.
LOCALIZATION_DIR = os.path.join(REPO_ROOT, "localization")
LOC_PROMPT_DIR = os.path.join(LOCALIZATION_DIR, "prompt")
# Candidate-function ranking model (MultiTaskRankNet) that pre-filters the
# functions of every localized file; trained by localization/fl_method/fliter_model.py.
LOC_FILTER_MODEL_PATH = os.path.join(LOCALIZATION_DIR, "fl_method", "filter_model.pt")
# Root of all localization run directories; each run gets its own
# ``run-<UTC timestamp>`` sub-directory (logs, intermediate and final results).
LOCALIZATION_OUTPUT_DIR = _output_dir(
    "COHIKER_LOCALIZATION_OUTPUT_DIR", "/data/localization_output", "localization_runs"
)
# Localization-only task inputs (the repair half does not read these).
# Default batch case list, one syzid per line.
LOC_CASE_LIST_FILE = os.path.join(DATASETS_DIR, "case_list_all.txt")
# Ground truth for the optional evaluation stage.
LOC_ORACLE_FILE_LIST = os.path.join(DATASETS_DIR, "oracle_file_list.json")
LOC_ORACLE_FUNC_LIST = os.path.join(DATASETS_DIR, "oracle_func_list.txt")

# The localization half writes these two artifacts and the repair half reads
# them (LOC_RESULT_FILE / ROOT_CAUSE_JSONL above); ``localization/main.py
# --publish`` copies one onto the other.
LOCALIZATION_LOC_RESULT_NAME = "file_func_result_v32.txt"
LOCALIZATION_ROOT_CAUSE_NAME = "rc_by_rank.jsonl"
