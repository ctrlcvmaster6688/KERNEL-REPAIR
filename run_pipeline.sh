#!/usr/bin/env bash
# =============================================================================
# CoHiKer++ -- end-to-end runner
#
#   localization (mutant generation + mutant reproduction)
#        |
#        v
#   repair (top-K functions -> patch -> kernel build -> QEMU validation)
#
# The two halves share the QEMU image, the QEMU ssh key and the kernel build
# cache, but not the ssh port: localization defaults to 10022 while the repair
# validators forward 10021 + worker_id.
#
# Typical use
#   bash run_pipeline.sh --syzid 60c52ca98513a8760a91
#   bash run_pipeline.sh --syzid <syzid> --localization-stages all   # full localization first
#   bash run_pipeline.sh --skip-localization --syzid <syzid>         # repair only (shipped results)
#   bash run_pipeline.sh --case-list data/datasets/case_list_demo.txt
#   bash run_pipeline.sh --syzid <syzid> --dry-run                   # print commands, run nothing
#
# Configuration comes from <repo>/.env (see .env.example); nothing is hard-coded
# here. `--help` lists every flag.
# =============================================================================
set -uo pipefail

# Keep the package directory clean: neither this script nor its children write
# __pycache__ next to the sources.
export PYTHONDONTWRITEBYTECODE=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "${PYTHON:-}" ]]; then
    PYTHON="$(command -v python || command -v python3 || true)"
fi
[[ -n "$PYTHON" ]] || { echo "ERROR: no python/python3 on PATH; pass --python PATH" >&2; exit 2; }

SYZID=""
CASE_LIST=""
MODE="${COHIKER_PIPELINE_MODE:-all}"
LOC_STAGES="${COHIKER_PIPELINE_STAGES:-all}"
SKIP_LOC=0
SKIP_REPAIR=0
PUBLISH=0
DRY_RUN=0
OUT_DIR=""

usage() {
    cat <<'EOF'
CoHiKer++ -- end-to-end runner

  localization (mutant generation + mutant reproduction)
       |
       v
  repair (top-K functions -> patch -> kernel build -> QEMU validation)

Usage:
  bash run_pipeline.sh (--syzid ID | --case-list FILE) [options]

Modes (--mode):
  all            (default) localization from scratch, then repair:
                 mutation -> reproduce -> file -> func -> export -> rootcause
                 -> publish -> repair (patch + kernel build + QEMU validation)
  localization   only the localization half (it still publishes its two artifacts)
  repair         only the repair half, reusing the localization results that are
                 already in DATASETS_DIR (or were published by an earlier run)

Examples:
  bash run_pipeline.sh --syzid 60c52ca98513a8760a91                  # head to tail
  bash run_pipeline.sh --syzid <syzid> --mode localization           # stage 1 only
  bash run_pipeline.sh --syzid <syzid> --mode repair                 # stage 2 only
  bash run_pipeline.sh --syzid <syzid> --localization-stages localize  # skip contrastive
  bash run_pipeline.sh --case-list data/datasets/case_list_demo.txt
  bash run_pipeline.sh --syzid <syzid> --dry-run

Options:
  --syzid ID                 run one case
  --case-list FILE           run every syzid listed in FILE (one per line)
  --mode M                   all (default) | localization | repair
  --localization-stages S    stages/preset for localization/main.py:
                             all (default) | localize | contrastive | mut,repro,file,func,...
  --publish                  always publish the two localization artifacts onto the
                             files the repair half reads. Implied by export/rootcause,
                             and always on in any mode that runs those stages.
  --output-dir DIR           run directory (default <repo>/runs/pipeline-<timestamp>)
  --python PATH              interpreter to use (default: $PYTHON, else python, else python3)
  --dry-run                  print the commands without executing them
  -h, --help                 show this help

Notes:
  The two halves share the QEMU image, the ssh key and the kernel build cache,
  but not the ssh port: localization defaults to 10022, the repair validators
  forward 10021 + worker_id.
  Configuration comes from <repo>/.env (see .env.example); nothing is hard-coded.

Exit status: 0 = everything succeeded, 1 = preflight failed, 2 = usage error,
             3 = a stage or a case failed.
EOF
}

die() { printf '\nERROR: %s\n' "$*" >&2; exit 2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --syzid)              [[ $# -ge 2 ]] || die "--syzid needs a value";              SYZID="$2"; shift 2 ;;
        --case-list)          [[ $# -ge 2 ]] || die "--case-list needs a value";          CASE_LIST="$2"; shift 2 ;;
        --mode)               [[ $# -ge 2 ]] || die "--mode needs a value";               MODE="$2"; shift 2 ;;
        --localization-stages) [[ $# -ge 2 ]] || die "--localization-stages needs a value"; LOC_STAGES="$2"; shift 2 ;;
        --output-dir)         [[ $# -ge 2 ]] || die "--output-dir needs a value";         OUT_DIR="$2"; shift 2 ;;
        --python)             [[ $# -ge 2 ]] || die "--python needs a value";             PYTHON="$2"; shift 2 ;;
        --publish)            PUBLISH=1; shift ;;
        --dry-run)            DRY_RUN=1; shift ;;
        -h|--help)            usage; exit 0 ;;
        *)                    die "unknown option: $1 (try --help)" ;;
    esac
done

case "$MODE" in
    all)                        SKIP_LOC=0; SKIP_REPAIR=0 ;;
    localization|loc)           MODE=localization; SKIP_LOC=0; SKIP_REPAIR=1 ;;
    repair|rep)                 MODE=repair;       SKIP_LOC=1; SKIP_REPAIR=0 ;;
    *)                          die "invalid --mode: $MODE (all | localization | repair)" ;;
esac

[[ -n "$SYZID" && -n "$CASE_LIST" ]] && die "--syzid and --case-list are mutually exclusive"
[[ -z "$SYZID" && -z "$CASE_LIST" ]] && die "need --syzid or --case-list (try --help)"

cd "$REPO_ROOT" || die "cannot cd to $REPO_ROOT"

# ---------------------------------------------------------------------------
# Run directory + logging (everything is teed into <run>/pipeline.log)
# ---------------------------------------------------------------------------
STAMP="$(date -u +%Y%m%d-%H%M%S)"
[[ -n "$OUT_DIR" ]] || OUT_DIR="$REPO_ROOT/runs/pipeline-$STAMP"
if [[ $DRY_RUN -eq 0 ]]; then
    mkdir -p "$OUT_DIR"
    exec > >(tee -a "$OUT_DIR/pipeline.log") 2>&1
fi

echo "==============================================================="
echo " CoHiKer++ pipeline"
echo "   repo        : $REPO_ROOT"
echo "   python      : $PYTHON"
echo "   mode        : $MODE"
echo "   cases       : ${SYZID:-$(sed -n '1p' "${CASE_LIST:-/dev/null}" 2>/dev/null)}${CASE_LIST:+ (from $CASE_LIST)}"
echo "   loc stages  : $LOC_STAGES"
echo "   run dir     : $OUT_DIR"
echo "   dry run     : $DRY_RUN"
echo "==============================================================="

run() {
    printf '\n+ %s\n' "$*"
    [[ $DRY_RUN -eq 1 ]] && return 0
    "$@"
}

# ---------------------------------------------------------------------------
# 1. Preflight -- uses the package's own paths/config so nothing is duplicated
# ---------------------------------------------------------------------------
echo
echo "--- preflight ---"
REPO_ROOT="$REPO_ROOT" SYZIDS_FOR_CHECK="${SYZID:-}" CASE_LIST_FOR_CHECK="${CASE_LIST:-}" \
LOC_STAGES_FOR_CHECK="$LOC_STAGES" MODE_FOR_CHECK="$MODE" \
"$PYTHON" - <<'PY'
import os
import subprocess
import sys

repo = os.environ["REPO_ROOT"]
sys.path.insert(0, repo)

fail, warn = [], []


def ok(msg):
    print(f"  [ok]   {msg}")


def bad(msg, hint=""):
    fail.append(msg + (f"\n         hint: {hint}" if hint else ""))
    print(f"  [fail] {msg}")


def soft(msg, hint=""):
    warn.append(msg + (f"\n         hint: {hint}" if hint else ""))
    print(f"  [warn] {msg}")


try:
    import paths
    import config
except Exception as exc:  # noqa: BLE001
    print(f"  [fail] cannot import the package: {type(exc).__name__}: {exc}")
    sys.exit(1)

ok(f"package imports (worker pool: {len(config.Config.WORKER_POOL_CONFIG)} worker(s))")

# LLM credentials (both halves need them)
if config.Config.LLM_API_KEY:
    ok(f"LLM configured ({config.Config.LLM_MODEL} @ {config.Config.LLM_BASE_URL})")
else:
    bad("LLM_API_KEY is not set", "put LLM_API_KEY in <repo>/.env (see .env.example)")

# Embedding service (only needed for golden-fix retrieval; degrades otherwise)
import json
import urllib.request

_embed_url = config.Config.EMBED_API_URL.replace("/v1/embeddings", "/embed")
_base_url = _embed_url
if _base_url.rstrip("/").endswith("/embed"):
    _base_url = _base_url.rstrip("/")[: -len("/embed")]
health_url = _base_url.rstrip("/") + "/health"


def _probe_embed():
    """Prefer GET /health; fall back to a tiny POST /embed (older services have no /health)."""
    try:
        with urllib.request.urlopen(health_url, timeout=4) as resp:
            return f"/health -> {resp.status}"
    except Exception:  # noqa: BLE001 - not fatal, try the real endpoint
        pass
    body = json.dumps({"content": "x"}).encode()
    req = urllib.request.Request(_embed_url, data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read().decode())
        return f"/embed -> {resp.status}, dim={len(payload.get('embedding') or [])}"


try:
    print(f"  [ok]   embedding service {_embed_url} ({_probe_embed()})")
except Exception as exc:  # noqa: BLE001
    soft(f"embedding service not reachable at {_embed_url} ({type(exc).__name__}: {exc})",
         "start it: python tools/embed_server/flask_embedding.py  (query_golden_fixes "
         "returns status=error without it; search_historical_fixes only degrades)")

# Kernel tree (needed by localization func stage and by the builders)
kernel_tree = paths.KERNEL_DIR_ORI
if os.path.isdir(os.path.join(kernel_tree, ".git")):
    ok(f"kernel tree (git): {kernel_tree}")
elif os.path.isdir(kernel_tree):
    bad(f"kernel tree is not a git checkout: {kernel_tree}",
        "git checkout -f <commit> and git log -S are required")
else:
    bad(f"kernel tree missing: {kernel_tree}", "set COHIKER_KERNEL_DIR_ORI in .env")

# QEMU image + key (shared by the localization reproduce stage and the validators)
img_paths, keys = paths.QEMU_IMAGE_PATHS, paths.QEMU_SSH_KEYS
missing = [p for p in img_paths + keys if not os.path.exists(p)]
if not missing:
    ok(f"QEMU images/keys: {len(img_paths)} worker(s)")
else:
    bad(f"missing QEMU image/key files: {missing[:4]}",
        "build/copy the bullseye image (syzbot create-image.sh) or set "
        "COHIKER_QEMU_IMAGE_PATHS / COHIKER_QEMU_SSH_KEYS")

# syzkaller binaries: built on demand by core/syz_manager.py, which needs `go`
missing_syz = [d for d in paths.SYZKALLER_WORKER_DIRS
               if not os.path.exists(os.path.join(d, "bin/linux_amd64/syz-executor"))]
if not missing_syz:
    ok(f"syzkaller binaries present ({len(paths.SYZKALLER_WORKER_DIRS)} worker(s))")
else:
    import shutil

    if shutil.which("go"):
        soft(f"syzkaller not built yet in {len(missing_syz)} worker dir(s); "
             f"syz_manager will run `make` (go: {shutil.which('go')})")
    else:
        bad(f"syzkaller binaries missing in {missing_syz[:2]} and 'go' is not on PATH",
            "export PATH=$PATH:/usr/local/go/bin (Go is often only exported from ~/.zshrc), "
            "or point COHIKER_SYZKALLER_WORKER_DIRS at a pre-built checkout")

# localize port must not collide with a validator port
loc_port = int(os.environ.get("COHIKER_LOCALIZATION_QEMU_SSH_PORT", "10022"))
worker_ports = [w["ssh_port"] for w in config.Config.WORKER_POOL_CONFIG]
if loc_port in worker_ports:
    soft(f"localization QEMU port {loc_port} equals a validator port ({worker_ports})",
         "fine for sequential runs, but concurrent QEMU guests will collide on hostfwd; "
         "set COHIKER_LOCALIZATION_QEMU_SSH_PORT to a free port")
else:
    ok(f"QEMU ports: localization {loc_port}, validators {worker_ports}")

# Repair needs localization results for the case(s)
syzids = []
if os.environ.get("SYZIDS_FOR_CHECK"):
    syzids = [os.environ["SYZIDS_FOR_CHECK"]]
elif os.environ.get("CASE_LIST_FOR_CHECK"):
    try:
        with open(os.environ["CASE_LIST_FOR_CHECK"], encoding="utf-8") as fh:
            syzids = [ln.strip().split("/")[0] for ln in fh if ln.strip() and not ln.startswith("#")]
    except OSError as exc:
        bad(f"cannot read --case-list: {exc}")

mode = os.environ.get("MODE_FOR_CHECK", "all")
if mode in ("repair", "rep") and syzids:
    loc_text = ""
    rc_text = ""
    try:
        with open(paths.LOC_RESULT_FILE, encoding="utf-8", errors="replace") as fh:
            loc_text = fh.read()
    except OSError:
        pass
    try:
        with open(paths.ROOT_CAUSE_JSONL, encoding="utf-8", errors="replace") as fh:
            rc_text = fh.read()
    except OSError:
        pass
    without = [c for c in syzids
               if f"Response for case {c}" not in loc_text or f'"{c}"' not in rc_text]
    if not without:
        ok(f"localization results present for {len(syzids)} case(s) in {paths.DATASETS_DIR}")
    elif len(without) == len(syzids):
        bad(f"no localization results for {len(syzids)} case(s) under {paths.DATASETS_DIR}",
            "run --mode localization first (it publishes the two artifacts), or point "
            "COHIKER_DATASETS_DIR at a directory that has file_func_result_v32.txt + rc_by_rank.jsonl")
    else:
        soft(f"no localization results for {len(without)}/{len(syzids)} case(s): {without[:3]}",
             "those cases will be repaired with an empty task queue")
elif syzids:
    ok("localization will produce the repair inputs in this run (mode = all/localization)")

if fail:
    print("\npreflight FAILED:")
    for item in fail:
        print(f"  - {item}")
    sys.exit(1)
if warn:
    print("\npreflight passed with warnings (the run continues)")
else:
    print("\npreflight passed")
PY
PREFLIGHT=$?
PREFLIGHT_FAILED=0
if [[ $PREFLIGHT -ne 0 ]]; then
    PREFLIGHT_FAILED=1
    if [[ $DRY_RUN -eq 0 ]]; then
        echo
        echo "Aborting before any stage runs. Fix the items above (or use --dry-run to inspect the commands)."
        exit 1
    fi
    echo "(dry run: continuing despite preflight failure)"
fi

# ---------------------------------------------------------------------------
# 2. Localization
# ---------------------------------------------------------------------------
LOC_RC=0
if [[ $SKIP_LOC -eq 0 ]]; then
    [[ -n "$CASE_LIST" ]] || CASE_LIST="$OUT_DIR/case_list.txt"
    if [[ -n "$SYZID" && $DRY_RUN -eq 0 ]]; then
        printf '%s\n' "$SYZID" > "$OUT_DIR/case_list.txt"
        CASE_LIST="$OUT_DIR/case_list.txt"
    fi
    # publish whenever the requested stages produce the two artifacts
    case "$LOC_STAGES" in
        *export*|*rootcause*|all|localize) PUBLISH=1 ;;
    esac
    LOC_ARGS=(localization/main.py --stages "$LOC_STAGES" --output-dir "$OUT_DIR/localization")
    [[ -n "$SYZID" ]] && LOC_ARGS+=(--syzid "$SYZID") || LOC_ARGS+=(--case-list "$CASE_LIST")
    [[ $PUBLISH -eq 1 ]] && LOC_ARGS+=(--publish)

    echo
    echo "--- localization ($LOC_STAGES) ---"
    run "$PYTHON" "${LOC_ARGS[@]}"
    LOC_RC=$?
    [[ $LOC_RC -ne 0 ]] && echo "localization failed (exit $LOC_RC)"
fi

# ---------------------------------------------------------------------------
# 3. Repair (patch generation + kernel build + QEMU validation per case)
# ---------------------------------------------------------------------------
REPAIR_FAILED=0
REPAIR_TOTAL=0
if [[ $SKIP_REPAIR -eq 0 && $LOC_RC -ne 0 ]]; then
    # mode=all: repairing with stale/absent localization output would silently
    # work on the wrong functions, so stop here instead.
    echo
    echo "--- repair skipped: localization failed (exit $LOC_RC) ---"
    SKIP_REPAIR=1
fi
if [[ $SKIP_REPAIR -eq 0 ]]; then
    echo
    echo "--- repair ($MODE) ---"
    CASES=()
    if [[ -n "$SYZID" ]]; then
        CASES=("$SYZID")
    else
        while IFS= read -r line; do
            line="${line%%#*}"
            line="$(echo "$line" | tr -d '[:space:]')"
            [[ -z "$line" ]] && continue
            CASES+=("${line%%/*}")
        done < "$CASE_LIST"
    fi
    echo "cases: ${#CASES[@]}"
    for case_id in "${CASES[@]}"; do
        REPAIR_TOTAL=$((REPAIR_TOTAL + 1))
        echo
        echo ">>> repair $case_id"
        run "$PYTHON" main.py --syzid "$case_id"
        rc=$?
        if [[ $rc -ne 0 ]]; then
            REPAIR_FAILED=$((REPAIR_FAILED + 1))
            echo ">>> repair $case_id FAILED (exit $rc)"
        else
            echo ">>> repair $case_id done"
        fi
    done
fi

# ---------------------------------------------------------------------------
# 4. Summary
# ---------------------------------------------------------------------------
echo
echo "==============================================================="
echo " summary"
echo "   mode               : $MODE"
echo "   run dir            : $OUT_DIR"
if [[ $PREFLIGHT_FAILED -eq 0 ]]; then
    echo "   preflight          : passed"
else
    echo "   preflight          : FAILED (see messages above)"
fi
if [[ $SKIP_LOC -eq 0 ]]; then
    echo "   localization       : exit $LOC_RC (stages: $LOC_STAGES)"
else
    echo "   localization       : skipped"
fi
if [[ $SKIP_REPAIR -eq 1 ]]; then
    echo "   repair             : skipped"
else
    echo "   repair             : $((REPAIR_TOTAL - REPAIR_FAILED))/$REPAIR_TOTAL case(s) ok"
fi
"$PYTHON" - <<'PY' 2>/dev/null || true
import sys
sys.path.insert(0, ".")
import paths
print(f"   localization out   : {paths.LOCALIZATION_OUTPUT_DIR}")
print(f"   repair logs        : {paths.LOG_DIR}")
print(f"   patches/results    : {paths.PATCH_ARCHIVE_DIR}")
print(f"   localization files : {paths.LOC_RESULT_FILE}")
print(f"                        {paths.ROOT_CAUSE_JSONL}")
PY
echo "==============================================================="

if [[ $PREFLIGHT_FAILED -ne 0 ]]; then
    exit 1
fi
if [[ $LOC_RC -ne 0 || $REPAIR_FAILED -ne 0 ]]; then
    exit 3
fi
exit 0
