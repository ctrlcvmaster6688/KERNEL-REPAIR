# CoHiKer++ — Reproducible Code Package

CoHiKer++ is a two-half system for Linux kernel bug fixing:

* **localization** (`localization/`) — finds the Top-K suspicious functions/files
  for a syzbot crash report and writes one root-cause hypothesis per ranked
  function;
* **repair** (`agent/`, `core/`, `skill/`, `tool/`, `utils/`, `main.py`) — a
  LangGraph multi-agent fixer (Planner → parallel fix threads
  Generator → Reviewer → Validator → Judge) backed by a case-level semantic
  knowledge base, a golden-patch knowledge base, patch generation, kernel build
  with a per-commit cache, and syzkaller/QEMU reproduction.

The repair half consumes two artifacts produced by the localization half
(`file_func_result_v32.txt` and `rc_by_rank.jsonl`, see §5.4). A snapshot of
both ships inside `data/datasets/`, so the repair half can also run **without**
localizing again (§8).

This package contains the **runtime code, the knowledge bases and one snapshot
of the localization results**. All filesystem locations are declared once in
`paths.py`; everything that ships with the package is addressed relatively.

---

## Tested environment

**Everything in this package — the paper results, the code, the commands in this
README — was produced and verified on exactly one machine, described below.
No other operating system, CPU, GPU, Python or toolchain combination was tested;
treat any other environment as untested/unsupported.**

| Item | Value on the reference machine |
|---|---|
| OS / kernel | Ubuntu 24.04.3 LTS, Linux 5.15.0-139-generic, **x86_64** |
| CPU | 2× Intel Xeon Gold 6326 @ 2.90 GHz (16 cores/socket, 2 threads/core) → **64 logical CPUs** |
| RAM | **503 GiB** |
| GPU | **4× NVIDIA GeForce RTX 3090 (24 GiB each)**, driver 570.211.01, CUDA 12.8 |
| Disk | `/data` on a 29 TB volume (≈ 4.6 TB free at the time) |
| KVM | `/dev/kvm` present (QEMU guests run with `-enable-kvm`) |
| Python | **3.10.19** |
| Toolchain | git 2.43.0, gcc 11.5.0 (Ubuntu), GNU Make 4.3, rsync 3.2.7, QEMU 8.2.2, GNU cflow 1.7 |
| Go | **go1.25.5 linux/amd64** |

If your environment differs in any of the rows above, expect to adapt paths,
driver/CUDA versions or parallelism — the functionality may still work, but we
cannot claim it does.

---

## 0. ⚡ Quick start

The whole paper pipeline — localization from scratch (contrastive mutant
generation + mutant reproduction + file/function-level localization + export)
followed by repair (Top-K functions → patch → kernel build → QEMU validation) —
runs with **one command**. That command needs the Python dependencies, an LLM
endpoint, a kernel tree, QEMU images, a syzkaller checkout and the BGE-M3 weights
to be in place first, so the order is:

**§0.1 install & prepare once → §0.2 run one command → §0.3 read the results.**

Everything the pipeline needs to *read* ships in this package (knowledge bases,
210 crash reports / testcases / kernel configs, one snapshot of the localization
results), so `--mode repair` works out of the box for those 210 cases.

### 0.1 Install and prepare (one time)

0. **Knowledge bases — the two SQLite files must sit under `data/`**

   Both halves open them at `data/<name>` (§2.1), so the file names matter:

   | file | size | used for |
   |---|---|---|
   | `data/kernel_patch_final_rag_v3.db` | 93 MB | case-level semantic KB — `search_historical_fixes` |
   | `data/golden_kernel_test_v3.db` | 261 MB | golden-patch KB — `query_golden_fixes` (hard dependency) |

   * **Using the paper artifact zip (`repro_package.zip`)?** Nothing to do, both
     files are already inside.
   * **Cloned the git repository?** They are *not* tracked by git (one file is
     above GitHub's 100 MB limit). Download the release asset **once** and unpack
     it **at the repository root**:

     ```bash
     curl -L -o /tmp/knowledge_bases.zip \
       https://github.com/ctrlcvmaster6688/LLM-based-fault-localization-for-linux-kernel/releases/download/v1.0.0/knowledge_bases.zip
     unzip -o /tmp/knowledge_bases.zip -d .     # writes data/*.db
     ls -lh data/*.db                           # expect 93M and 261M
     ```

     The download is 151 MB; unpacked the two databases take ≈350 MB. They must
     end up next to `paths.py` (i.e. `<repo>/data/`); if you keep them elsewhere,
     point `GOLDEN_DB_PATH` at the golden DB and adjust `DB_PATH` in `paths.py`
     for the other one.

   Without them, `--mode repair` fails at the first KB query; the localization
   half alone only needs the embedding service (§6).

1. **Python dependencies** — one command

   ```bash
   pip install -r requirements.txt
   ```

   `requirements.txt` lists everything (§9 has the notes); on a CUDA host install
   the matching torch build first, e.g.
   `pip install torch --index-url https://download.pytorch.org/whl/cu124`.

2. **Linux kernel source — a full git checkout, not a tarball**

   ```bash
   git clone https://git.kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git /data/linux-ori
   #   mirror: git clone https://github.com/torvalds/linux.git /data/linux-ori
   ```

   The pipeline runs `git checkout -f <commit>` and `git log -S`, so the clone
   must keep its **full history** (no `--depth 1`) and must be writable
   (~5–8 GB). Check that the case commits resolve:

   ```bash
   while IFS=: read -r id commit; do
       git -C /data/linux-ori cat-file -e "$commit" 2>/dev/null || echo "missing $commit ($id)"
   done < data/datasets/case_commit.txt
   ```

   This tree stays pristine: the builds happen in the worker trees (§2.3), which
   the pipeline clears and re-syncs from here. **One clone is enough for any
   number of workers** — do not copy it per worker.

3. **syzkaller — one clone per worker (N copies)**

   ```bash
   for i in 0 1 2; do
       git clone https://github.com/google/syzkaller.git /data/syzkaller_worker_$i
   done
   ```

   **Each worker needs its own clone**: `core/syz_manager.py` runs
   `git checkout <commit>` and `make` inside that directory, so concurrent
   workers sharing one clone would corrupt each other. It needs a **Go toolchain
   ≥ 1.20** on the `PATH` of the shell that runs the pipeline. You do not have to
   build it by hand — the pipeline does it — but each clone ends up with its own
   `bin/linux_amd64/syz-execprog` + `syz-executor`.

4. **QEMU image + ssh key — one directory per worker (N directories)**

   ```bash
   cd /data/syzkaller_worker_0/tools
   ./create-image.sh -d bullseye        # apt-get install -y debootstrap; run as root
   #   -> bullseye.img + bullseye.id_rsa in the current directory
   #   (flags differ between syzkaller versions: ./create-image.sh --help)

   mkdir -p /data/qemu-worker-0
   mv bullseye.img bullseye.id_rsa /data/qemu-worker-0/

   # optional: physical copies for the other workers (reference layout)
   for i in 1 2; do
       mkdir -p /data/qemu-worker-$i
       cp /data/qemu-worker-0/bullseye.img /data/qemu-worker-0/bullseye.id_rsa /data/qemu-worker-$i/
   done
   ```

   The two file names are fixed (the QEMU command and the ssh/scp calls use
   exactly `bullseye.img` / `bullseye.id_rsa`). Copying is **not strictly
   required**: the image is a raw ext4 rootfs booted as `/dev/sda`, and both
   halves only read it (QEMU with `-snapshot`, localization copies it to `/tmp`),
   so the four `.env` entries may point at the same `bullseye.img` /
   `bullseye.id_rsa`. The reference machine keeps one physical copy per worker
   (2 GB each). Each worker gets its own ssh port (`10021 + i`) automatically.

5. **BGE-M3 weights + embedding service (1 copy)**

   ```bash
   pip install -U huggingface_hub
   export HF_ENDPOINT=https://hf-mirror.com                     # optional mirror
   huggingface-cli download BAAI/bge-m3 --local-dir /data/beg-m3

   python tools/embed_server/flask_embedding.py &               # port 6001
   curl -s http://127.0.0.1:6001/health                         # {"status":"ok",...}
   ```

   Keep it running while you use the pipeline. Without it `query_golden_fixes`
   returns `status=error` (hard dependency); `search_historical_fixes` only
   degrades. Details in §6.1.

**How many copies?** Let `N` be the number of parallel workers (3 on the
reference machine, `3` is also the built-in default):

| What | Copies | Why |
|---|---|---|
| kernel clone (`KERNEL_DIR_ORI`) | **1** | pristine source; each worker tree is rsynced from it |
| worker kernel trees (`KERNEL_WORKER_DIRS`) | **N directories** (may be empty) | created/filled by the pipeline on first use; ≈10 GB each once built |
| syzkaller (`SYZKALLER_WORKER_DIRS`) | **N** | every worker checks out and builds in its own clone |
| QEMU image + key (`QEMU_IMAGE_PATHS` / `QEMU_SSH_KEYS`) | **N entries**; the files may be shared | opened read-only (`-snapshot` / copy to `/tmp`); the reference layout uses N physical copies (2 GB each) |
| BGE-M3 weights (`COHIKER_EMBED_MODEL_PATH`) | **1** | one service process serves every worker |
| `.env` | **1** | all workers are described by the four lists in it |

6. **Fill `.env` — now that every path exists**

   ```bash
   cp .env.example .env
   ${EDITOR:-vi} .env
   ```

   Three-worker configuration (the reference layout; the four lists must all have
   the same number of comma-separated entries):

   ```ini
   LLM_API_KEY=sk-...
   COHIKER_KERNEL_DIR_ORI=/data/linux-ori
   COHIKER_KERNEL_WORKER_DIRS=/data/linux-worker-0,/data/linux-worker-1,/data/linux-worker-2
   COHIKER_SYZKALLER_WORKER_DIRS=/data/syzkaller_worker_0,/data/syzkaller_worker_1,/data/syzkaller_worker_2
   COHIKER_QEMU_IMAGE_PATHS=/data/qemu-worker-0/bullseye.img,/data/qemu-worker-1/bullseye.img,/data/qemu-worker-2/bullseye.img
   COHIKER_QEMU_SSH_KEYS=/data/qemu-worker-0/bullseye.id_rsa,/data/qemu-worker-1/bullseye.id_rsa,/data/qemu-worker-2/bullseye.id_rsa
   COHIKER_EMBED_MODEL_PATH=/data/beg-m3
   EMBED_API_URL=http://127.0.0.1:6001/embed
   COHIKER_NUM_CORES=20
   ```

   These values are exactly the built-in defaults, so **if you cloned/downloaded
   into the locations above (or copied the reference `/data/*` layout), you can
   delete the four worker lines** and keep only `LLM_API_KEY`,
   `COHIKER_KERNEL_DIR_ORI`, `COHIKER_EMBED_MODEL_PATH`, `EMBED_API_URL`.

   Running one case at a time? One worker is enough — make all four lists
   single-valued (`/data/linux-worker-0`, `/data/syzkaller_worker_0`,
   `/data/qemu-worker-0/bullseye.img`, `/data/qemu-worker-0/bullseye.id_rsa`);
   the pipeline never requires three.

   `/data/linux-worker-0` is scratch space for builds — never point it at the
   pristine clone. Check what your `.env` resolved to before running anything:

   ```bash
   python -c "from config import Config; print(Config.WORKER_POOL_CONFIG)"
   ```

### 0.2 Run it

```bash
bash run_pipeline.sh --syzid 60c52ca98513a8760a91
```

That is the head-to-tail run. It starts with a preflight (below) and stops with
an actionable message if something is still missing; add `--dry-run` to see the
exact commands without executing anything. Expect the first case of a batch to be
much slower — it compiles the kernel for that commit (§7.2).

Other entry points:

| Goal | Command |
|---|---|
| **head to tail** (default) | `bash run_pipeline.sh --syzid <syzid>` |
| localization half only (publishes its artifacts) | `bash run_pipeline.sh --syzid <syzid> --mode localization` |
| repair half only, reusing already-published results | `bash run_pipeline.sh --syzid <syzid> --mode repair` |
| full pipeline but skip the contrastive stage | `bash run_pipeline.sh --syzid <syzid> --localization-stages localize` |
| a batch of cases | `bash run_pipeline.sh --case-list data/datasets/case_list_demo.txt` |
| see what would run, execute nothing | `bash run_pipeline.sh --syzid <syzid> --dry-run` |

`--mode all` (default) runs `mutation → reproduce → file → func → export →
rootcause → evaluate`, publishes the two artifacts, then repairs every case. It
is the expensive path: the first commit of a batch also pays for a full kernel
build (§7.2). `--localization-stages` narrows the localization half (presets
`all` / `localize` / `contrastive`, or an explicit stage list), and
`--mode localization` + `--mode repair` split the two halves across two
invocations — the second one reads what the first one published.

The runner is a thin wrapper: it runs the preflight checks below, calls
`localization/main.py`, then calls `main.py --syzid` once per case. In
`--mode all` a failed localization aborts before repair instead of repairing
with stale results.

If your interpreter is not on `PATH` as `python`/`python3` (e.g. a conda env),
pass it explicitly: `bash run_pipeline.sh --syzid <syzid> --python /root/miniconda3/bin/python`
(the same interpreter can be forced with `PYTHON=/path/to/python`).

`run_pipeline.sh` may lose its executable bit when the zip is unpacked, so the
documented form is `bash run_pipeline.sh ...` (or `chmod +x run_pipeline.sh`
once). A dry run prints the exact commands and touches nothing.

**Preflight** (fails before any stage, with an actionable hint): it uses the
package's own `paths`/`config`, so it checks exactly what a run will use —
package importability + worker-pool consistency, `LLM_API_KEY`, embedding
`/health`, kernel tree is a git checkout, every QEMU image/key exists, and the
localization QEMU port does not collide with a validator port. In `--mode repair`
it additionally verifies that the case already has localization results; in
`--mode all` those are produced by the run itself.

### 0.3 Where the results are

The runner prints every location at the end of the run (in the `summary` block).
Concretely:

| What | Where |
|---|---|
| runner log (all stages, all stdout/stderr) | `runs/pipeline-<UTC>/pipeline.log` |
| localization run dir | `runs/pipeline-<UTC>/localization/` (§5.4) |
| Top-K localization output | `…/localization/file_func_result_v32.txt` → published to `paths.LOC_RESULT_FILE` |
| root-cause hypotheses | `…/localization/rc_by_rank.jsonl` → published to `paths.ROOT_CAUSE_JSONL` |
| file / function level intermediate results | `…/localization/{fl_file_result.txt,fl_method_result.json,candidate_functions.txt}` |
| **repair agent trace per case** (tools, LLM calls, build, QEMU, verdict) | `<LOG_DIR>/<syzid>_<YYYYmmdd_HHMMSS>.log` |
| **generated patches** | `<PATCH_ARCHIVE_DIR>/<syzid>/` (`<syzid>_fixed.patch`, `<syzid>_fixed_combined.patch`, per-attempt `.patch`/`.c` files) |
| knowledge-base retrieval logs | `<PATCH_ARCHIVE_DIR>/retrieval_logs/*.json` |
| warmed kernel snapshot (reused next time) | `<CACHE_ROOT>/<kernel_commit>/` (§7) |

`LOG_DIR` / `PATCH_ARCHIVE_DIR` / `CACHE_ROOT` are the `paths.py` values
(`COHIKER_LOG_DIR`, `COHIKER_PATCH_ARCHIVE_DIR`, `COHIKER_CACHE_ROOT`; defaults
`/data/logs_new_v32`, `/data/output_patches_v32/patches`, `/data/cache_linux`).

Quick checks after a run (default paths; adjust the last three lines if you
overrode `COHIKER_LOG_DIR` / `COHIKER_PATCH_ARCHIVE_DIR`):

```bash
latest=$(ls -dt runs/pipeline-* | head -1); echo "$latest"
tail -n 40 "$latest/pipeline.log"                       # what the runner did last
ls "$latest/localization"                               # localization artifacts
head -n 12 "$latest/localization/file_func_result_v32.txt"
ls -t /data/logs_new_v32/*.log | head -3                # newest repair logs
grep -E "FIXED|CRASHED|COMPILE_FAILED|VALIDATED" /data/logs_new_v32/<syzid>_*.log | tail
ls /data/output_patches_v32/patches/<syzid>/            # the patches themselves
```

### 0.4 Minimum setup summary

| You must provide | Where |
|---|---|
| Python dependencies | `pip install -r requirements.txt` (§9) |
| LLM key + endpoint | `.env`: `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` |
| kernel git checkout | `.env`: `COHIKER_KERNEL_DIR_ORI` |
| QEMU image + key per worker | `.env`: `COHIKER_QEMU_IMAGE_PATHS`, `COHIKER_QEMU_SSH_KEYS` |
| syzkaller clone | `.env`: `COHIKER_SYZKALLER_WORKER_DIRS` (Go toolchain installed) |
| BGE-M3 weights + running service | `.env`: `COHIKER_EMBED_MODEL_PATH`, `EMBED_API_URL` |
| *(optional)* per-commit build cache | `.env`: `COHIKER_CACHE_ROOT` (§7; first run per commit is a full kernel build — tens of minutes and ~30–40 GB RAM at the default 20 jobs) |
| *(optional)* dataset override | `.env`: `COHIKER_DATASETS_DIR`; empty = bundled mirror |

Everything else — the semantic KB, the golden-patch KB, BM25, the vocabulary,
the tree-sitter grammar, 210 crash reports/testcases/configs and one
localization-result snapshot — is inside the package and works from any
directory after unzipping.

**Two defaults that can bite.**

* `DATASETS_DIR` falls back to `/data/datasets` whenever that directory exists,
  which silently overrides the bundled mirror. If the host happens to have a
  `/data/datasets` with different contents, the run will use it without any
  warning — set `COHIKER_DATASETS_DIR` explicitly when in doubt.
* Both halves boot a QEMU guest and reach it over SSH. The repair half uses
  `10021 + worker_id`, so when localization and repair run at the same time,
  give localization a different port via
  `COHIKER_LOCALIZATION_QEMU_SSH_PORT` (default `10022`).

---

## 1. Directory structure

```
kernel_patcher_repro/
├── run_pipeline.sh              # ONE command: localization -> repair (§0.2)
├── main.py                      # repair entry point (CLI: --syzid / --batch)
├── config.py                    # runtime configuration, values taken from paths.py
├── paths.py                     # ALL paths used by the system (single source of truth)
├── .env.example                 # every environment variable the code honours
├── requirements.txt             # pip install -r requirements.txt (§9)
├── agent/                       # repair: LangGraph workflow
│   ├── fix_thread_graph.py      # graph definition, routing, kernel-cache warm-up
│   ├── parallel_nodes.py        # Planner / exploration planner / failure report
│   ├── fix_thread.py            # Generator → Reviewer → Validator → Judge loop
│   ├── state.py                 # state schema + attempt limits
│   ├── prompts.py               # prompt templates
│   ├── log_manager.py
│   └── __init__.py
├── core/
│   ├── Hybrid_patch_rag.py      # case-level semantic knowledge base (SQLite + BM25 + BGE-M3)
│   ├── struct_rag.py            # tree-sitter structural context extractor
│   ├── kbuild_manager.py        # kernel configure / incremental build / build cache
│   ├── patch_applier.py         # tree-sitter based patch application
│   ├── qemu_runner.py           # QEMU boot + syzkaller reproducer execution
│   ├── syz_manager.py           # syzkaller checkout / build
│   ├── validate_config.py       # ValidationConfig dataclass
│   ├── validate_main.py         # standalone validation driver
│   └── log_manager.py
├── skill/
│   ├── sandbox_validator.py     # single-worker sandbox validation
│   ├── parallel_validator.py    # multi-worker validation
│   ├── kernel_fix_expert.py     # golden-patch retrieval (intent embedding + SQL)
│   └── __init__.py
├── tool/
│   ├── function_calls.py        # ReAct tools (read_file, get_function_context, ...)
│   ├── kernel_doc.py            # kernel API documentation lookup
│   └── __init__.py
├── tools/
│   └── embed_server/
│       └── flask_embedding.py   # BGE-M3 embedding service (POST /embed, GET /health, §6)
├── utils/
│   ├── llm_interface.py         # OpenAI-compatible client + token statistics
│   ├── unified_log.py           # logging setup
│   ├── file_ops.py
│   └── patch_manager.py
├── localization/                # localization: single entry point, see §5
│   ├── main.py                  # ONE command runs every stage (single case or batch)
│   ├── settings.py              # selects/derives paths from ../paths.py
│   ├── llm.py                   # OpenAI-compatible client (same Config.LLM_* as repair)
│   ├── case_list.py             # case-list normalisation (syzid -> syzid/0/0/0/0)
│   ├── export.py                # fl_method result -> file_func_result_v32.txt
│   ├── root_cause.py            # NEW step: per-case root cause -> rc_by_rank.jsonl
│   ├── contrastive_reasoning/   # mutant generation + mutant reproduction (needs QEMU)
│   ├── fl_file/                 # file-level localization (LLM)
│   ├── fl_method/               # function filtering (RankNet) + function localization (LLM)
│   │   ├── fl_method.py
│   │   ├── func_fliter.py
│   │   ├── filter_model.pt      # ranking model consumed by func_fliter.py
│   │   ├── fliter_model.py      # training script that produced filter_model.pt
│   │   ├── get_cg_level.py      # call-graph level/degree features (cflow)
│   │   ├── get_complexcity.py   # cyclomatic complexity / Halstead effort
│   │   ├── get_duringtime.py    # function age feature (git log -S)
│   │   └── tools.py             # tree-sitter function extraction + features
│   ├── evaluation/              # file / function level metrics against the oracle files
│   └── prompt/                  # prompt templates used by the stages above
├── data/                        # knowledge bases + task inputs shipped with the package (§3)
│   ├── kernel_patch_final_rag_v3.db
│   ├── kernel_bm25_v3.idx
│   ├── golden_kernel_test_v3.db
│   ├── final_kernel_master_vocab_v4.json
│   ├── my-languages.so
│   └── datasets/                # per-case inputs + one snapshot of the localization results
└── README.md
```

Removed from the original working trees: `scripts/`, `analysis_results/`,
`__pycache__/`, logs, experiment outputs, notes and result JSON files.
The SWE-bench / SWE-agent / Agentless material that exists next to the original
code is intentionally not part of this package.

---

## 2. `paths.py` — path constants

`paths.py` is imported by every module; no other file contains an absolute
path. `REPO_ROOT` / `DATA_DIR` are resolved relative to `paths.py` itself, so
the package can be moved as a whole (copy a directory, unzip anywhere) without
editing anything.

### 2.1 Values that point inside this package (relative)

| Constant | Value | Meaning |
|---|---|---|
| `REPO_ROOT` | directory of `paths.py` | package root, computed at import time |
| `DATA_DIR` | `<REPO_ROOT>/data` | bundled knowledge bases |
| `DB_PATH` | `<DATA_DIR>/kernel_patch_final_rag_v3.db` | case-level semantic knowledge base (SQLite, 2 594 cases) |
| `BM25_PATH` | `<DATA_DIR>/kernel_bm25_v3.idx` | BM25 companion index of the above (pickle: `BM25Okapi` + `mapping`) |
| `LANG_SO_PATH` | `<DATA_DIR>/my-languages.so` | tree-sitter C grammar (used by **both** halves) |
| `VOCAB_PATH` | `<DATA_DIR>/final_kernel_master_vocab_v4.json` | vocabulary for golden-patch retrieval |
| `GOLDEN_DB_PATH` | `<DATA_DIR>/golden_kernel_test_v3.db` | golden-patch knowledge base (SQLite, 21 132 rows + intent embeddings) |
| `LOCALIZATION_DIR` | `<REPO_ROOT>/localization` | localization module home |
| `LOC_PROMPT_DIR` | `<LOCALIZATION_DIR>/prompt` | localization prompt templates |
| `LOC_FILTER_MODEL_PATH` | `<LOCALIZATION_DIR>/fl_method/filter_model.pt` | candidate-function ranking model |

### 2.2 External paths that must be provided when reproducing

| Constant | Default value | Meaning / what to provide |
|---|---|---|
| `DATASETS_DIR` | `$COHIKER_DATASETS_DIR` → `/data/datasets` → `<DATA_DIR>/datasets` | task dataset root (see §3) |
| `ROOT_CAUSE_JSONL` | `<DATASETS_DIR>/rc_by_rank.jsonl` | root-cause hypotheses, one JSON object per line — written by localization (§5.4) |
| `LOC_RESULT_FILE` | `<DATASETS_DIR>/file_func_result_v32.txt` | localization Top-K function/file results — written by localization (§5.4) |
| `COMMIT_FILE` | `<DATASETS_DIR>/case_commit.txt` | `syzid:kernel_commit` mapping |
| `SYZ_BRANCH_FILE` | `<DATASETS_DIR>/syz_branch.txt` | `syzid:syzkaller_commit` mapping |
| `ORACLE_FILE` | `<DATASETS_DIR>/oracle_content.txt` | `syzid:oracle_keyword` used to confirm a fix |
| `BUG_REPORT_DIR` | `<DATASETS_DIR>/bug_report` | `<syzid>.txt` crash reports |
| `TESTCASE_DIR` | `<DATASETS_DIR>/testcases` | `<syzid>.syz` syzkaller reproducers |
| `BUG_CONFIG_DIR` | `<DATASETS_DIR>/bug_config` | `<syzid>.txt` kernel `.config` files |
| `KERNEL_DIR_ORI` | `/data/linux-ori` (`$COHIKER_KERNEL_DIR_ORI`) | pristine kernel tree, git checkout (8.8 GB in the original setup) |
| `CACHE_ROOT` | `/data/cache_linux` (`$COHIKER_CACHE_ROOT`) | per-commit build snapshots, see §7 |
| `LOG_DIR` | `/data/logs_new_v32` (`$COHIKER_LOG_DIR`) | run logs; falls back to `<REPO_ROOT>/runs/logs_new_v32` when `/data` is absent |
| `PATCH_ARCHIVE_DIR` | `/data/output_patches_v32/patches` (`$COHIKER_PATCH_ARCHIVE_DIR`) | generated patches and batch results |
| `LOCALIZATION_OUTPUT_DIR` | `/data/localization_output` (`$COHIKER_LOCALIZATION_OUTPUT_DIR`) | one `run-<timestamp>/` directory per localization run |
| `LOC_CASE_LIST_FILE` | `<DATASETS_DIR>/case_list_all.txt` | default batch input of `localization/main.py` |
| `LOC_ORACLE_FILE_LIST` | `<DATASETS_DIR>/oracle_file_list.json` | file-level ground truth (optional evaluation) |
| `LOC_ORACLE_FUNC_LIST` | `<DATASETS_DIR>/oracle_func_list.txt` | function-level ground truth (optional evaluation) |

`DATASETS_DIR` resolution is what makes the package self-contained: point
`COHIKER_DATASETS_DIR` at your dataset, or let it fall back to the bundled
mirror when `/data/datasets` does not exist.

### 2.3 Validation worker pool

The validator needs one kernel tree, one syzkaller checkout and one QEMU image
per worker (three workers by default). Each list below is indexed by worker id
in `config.py`, and every one of them can be overridden with a comma-separated
environment variable — the four lists are **parallel** and must have the same
length, otherwise `paths.py` aborts at import time with an explicit error
(never a silent truncation). `config.Config.WORKER_POOL_CONFIG` is generated by
zipping them; the SSH port of worker *i* is `10021 + i`.

| Constant | Env override | Default value | Meaning |
|---|---|---|---|
| `KERNEL_WORKER_DIRS` | `COHIKER_KERNEL_WORKER_DIRS` | `/data/linux-worker-{0,1,2}` | kernel trees used by validators |
| `SYZKALLER_WORKER_DIRS` | `COHIKER_SYZKALLER_WORKER_DIRS` | `/data/syzkaller_worker_{0,1,2}` | syzkaller checkouts |
| `QEMU_IMAGE_PATHS` | `COHIKER_QEMU_IMAGE_PATHS` | `/data/qemu-worker-{0,1,2}/bullseye.img` | boot images |
| `QEMU_SSH_KEYS` | `COHIKER_QEMU_SSH_KEYS` | `/data/qemu-worker-{0,1,2}/bullseye.id_rsa` | SSH keys for the guests |
| `KERNEL_WORKER_DIR_PATTERN` / `KERNEL_MAX_WORKERS` | `COHIKER_KERNEL_WORKER_PATTERN` / `COHIKER_KERNEL_MAX_WORKERS` | `/data/linux-worker-{index}` / `10` | the paths a restored build cache may have been compiled at; `KbuildManager` rewrites those stale prefixes inside `*.cmd` files (§7.1) |
| `KERNEL_LEGACY_DIR`, `KERNEL_PATH_CANDIDATES` | — | `/data/linux` + the pattern above | full list of old build paths handled by the path surgery (§7) |
| `GUEST_HOME_DIR` / `GUEST_REPRO_FILE` | — | `/root`, `/root/repro.txt` | paths **inside** the QEMU guest |
| `DEMO_BUG_CONFIG_PATH` | — | `/data/config` | dummy value used by the `__main__` smoke block of `agent/fix_thread_graph.py` |

Example — a two-worker pool somewhere else:

```bash
export COHIKER_KERNEL_WORKER_DIRS=/tmp/w0,/tmp/w1
export COHIKER_SYZKALLER_WORKER_DIRS=/tmp/s0,/tmp/s1
export COHIKER_QEMU_IMAGE_PATHS=/tmp/q0/bullseye.img,/tmp/q1/bullseye.img
export COHIKER_QEMU_SSH_KEYS=/tmp/q0/bullseye.id_rsa,/tmp/q1/bullseye.id_rsa
python -c "from config import Config; print(Config.WORKER_POOL_CONFIG)"
```

### 2.4 Environment variables

`config.py` and `paths.py` read the following variables (see `.env.example`):

| Variable | Default | Used for |
|---|---|---|
| `LLM_API_KEY` | — (**required**) | LLM calls in both halves |
| `LLM_BASE_URL` | `config.Config.LLM_BASE_URL` | OpenAI-compatible endpoint |
| `LLM_MODEL` | `deepseek-v3.2` | model name |
| `EMBED_API_URL` | `http://127.0.0.1:6001/embed` | BGE-M3 embedding service, client side (§6) |
| `COHIKER_EMBED_MODEL_PATH` | — (required to start the server) | BGE-M3 weights directory (§6.1) |
| `COHIKER_EMBED_PORT` | `6001` | port the bundled embedding server listens on |
| `COHIKER_EMBED_DEVICE` | `auto` | `auto` → `cuda:1` (≥2 GPUs) / `cuda:0` (1) / `cpu` (none) |
| `COHIKER_EMBED_MAX_LENGTH` | `1024` | token truncation of the embedding server (keep it) |
| `COHIKER_DATASETS_DIR` | see §2.2 | task dataset root |
| `COHIKER_KERNEL_DIR_ORI` | `/data/linux-ori` | pristine kernel tree |
| `COHIKER_CACHE_ROOT` | `/data/cache_linux` | kernel build cache |
| `COHIKER_LOG_DIR` | `/data/logs_new_v32` | run logs |
| `COHIKER_PATCH_ARCHIVE_DIR` | `/data/output_patches_v32/patches` | generated patches |
| `COHIKER_LOCALIZATION_OUTPUT_DIR` | `/data/localization_output` | localization runs |
| `COHIKER_KERNEL_WORKER_DIRS`, `COHIKER_SYZKALLER_WORKER_DIRS`, `COHIKER_QEMU_IMAGE_PATHS`, `COHIKER_QEMU_SSH_KEYS` | see §2.3 | worker pool (four parallel lists) |
| `COHIKER_KERNEL_WORKER_PATTERN`, `COHIKER_KERNEL_MAX_WORKERS` | `/data/linux-worker-{index}`, `10` | *advanced*: where a restored build cache may have been compiled; used to rewrite stale paths in `*.cmd` files (§7.1). Leave at the default unless your cache comes from another directory |
| `COHIKER_NUM_CORES` | `20` | `make -j` parallelism (warm-up and validators); ~1.5–2 GB RAM per job, so 20 → ~30–40 GB peak, 8 → ~15 GB |
| `COHIKER_QEMU_MEM` | `4G` | RAM per QEMU validation guest |

The localization `reproduce` stage additionally reads
`COHIKER_LOCALIZATION_QEMU_SSH_PORT` (default `10022`) and
`COHIKER_LOCALIZATION_QEMU_MEM` (default `4G`) for its own guest; keep the port
different from the validators' `10021 + worker_id` unless both halves run one
after the other.

Load them with `cp .env.example .env` and edit — `paths.py` reads `.env` from
the package root automatically (no shell command needed); a variable that is
already exported in the environment wins over the file. `set -a; . ./.env;
set +a` also works if you prefer exporting.

---

## 3. `data/` — bundled knowledge bases and task inputs

| File | Size | Content |
|---|---|---|
| `kernel_patch_final_rag_v3.db` | 93 MB | case-level semantic KB, table `commits_rag`, 2 594 rows: report text/trace/meta + `commit_vec`, `desc_vec`, `trace_vec`, `meta_vec` |
| `kernel_bm25_v3.idx` | 4.9 MB | `{model: BM25Okapi, mapping: [2 594 ids]}` |
| `golden_kernel_test_v3.db` | 273 MB | golden-patch KB, table `golden_knowledge`, 21 132 rows: intent + `intent_vec` + patch |
| `final_kernel_master_vocab_v4.json` | 15 KB | vocabulary for keyword extraction |
| `my-languages.so` | 700 KB | tree-sitter C grammar (linux x86-64) |
| `datasets/bug_report/` | 3.1 MB | 211 crash reports |
| `datasets/testcases/` | 4.8 MB | 220 syzkaller reproducers |
| `datasets/bug_config/` | 48 MB | 211 per-case kernel `.config` files |
| `datasets/case_commit.txt`, `syz_branch.txt`, `oracle_content.txt` | 42 KB | per-case kernel commit / syzkaller commit / crash keyword |
| `datasets/case_list_all.txt`, `case_list_demo.txt` | 7 KB | case lists (210 cases / small demo) |
| `datasets/file_func_result_v32.txt` | 100 KB | **snapshot** of the localization Top-K output (210 cases) |
| `datasets/rc_by_rank.jsonl` | 910 KB | **snapshot** of the root-cause output (210 cases) |
| `datasets/oracle_file_list.json`, `oracle_func_list.txt` | 331 KB | localization ground truth, used only by the optional evaluation stage |

All knowledge bases are addressed relative to `data/` (§2.1) — no absolute
path, no rebuild step, unzip and run. The two that are actually read at runtime
are `kernel_patch_final_rag_v3.db` (semantic / historical fixes, used by
`search_historical_fixes`) and `golden_kernel_test_v3.db` (golden fixes, used by
`query_golden_fixes`).

> **Cloned the repository from GitHub?** These two files are published as the
> `knowledge_bases.zip` **release asset** instead of being tracked by git (one is
> above GitHub's 100 MB per-file limit). Download and unpack it at the repository
> root once — the exact commands and expected sizes are in **§0.1 step 0**. The
> artifact zip distributed with the paper already contains them.


`data/datasets/` is a partial mirror provided for convenience: it holds the
per-case **inputs** of the 210 reported cases plus one snapshot of the
localization results. If the full `/data/datasets` is available, leave
`DATASETS_DIR` at `/data/datasets`; otherwise use the mirror (§8).

---

## 4. Large external data (not in this package)

| Data | Size | Why it is not bundled / where to put it |
|---|---|---|
| `/data/linux-ori` | 8.8 GB | pristine kernel tree; clone the Linux repo matching the case commits and point `KERNEL_DIR_ORI` at it |
| `/data/cache_linux` | 1.6 TB in the original setup | per-commit build snapshots (§7); recreated on first run, or copied from the original machine |
| `/data/linux-worker-*`, `/data/syzkaller_worker_*`, `/data/qemu-worker-*` | several GB | validator pool: kernel trees, syzkaller builds (`bin/linux_amd64/syz-execprog`, `syz-executor`), Debian bullseye QEMU images |
| BGE-M3 weights (`/data/beg-m3`, 2.2 GB) | 2.2 GB | only needed by the embedding service (§6), which runs as a separate process |
| `/data/output_patches_v32` | 43 MB | previous run outputs; only `PATCH_ARCHIVE_DIR` is used as an output directory |

The kernel and validator pools require a working kernel build toolchain and a
KVM-capable host; validation is the only stage that needs them.

---

## 5. Localization half (`localization/`)

### 5.1 One command

```bash
export LLM_API_KEY=...            # same variables as the repair half
export EMBED_API_URL=http://127.0.0.1:6001/embed   # optional, §6

# single case
python localization/main.py --syzid 60c52ca98513a8760a91

# batch (default list: paths.LOC_CASE_LIST_FILE)
python localization/main.py --case-list <path/to/case_list.txt>

# every original stage, including the QEMU-based contrastive stage
python localization/main.py --syzid <syzid> --stages all

# chain into the repair half: copy the two artifacts onto the files it reads
python localization/main.py --case-list <list> --publish
```

`python localization/main.py --help` lists every flag. Nothing else has to be
run by hand: the entry point normalises the case list, runs the stages in
order, and exports the two result files.

### 5.2 Stages

| Stage | Script | Needs | Produces (inside the run directory) |
|---|---|---|---|
| `mutation` | `contrastive_reasoning/mutation_generate.py` | LLM | `contrastive_reasoning/mutation_result.txt`, `mutations/` |
| `reproduce` | `contrastive_reasoning/testcase_reproduce.py` | QEMU image, syzkaller, kernel tree, `bug_config/` | `contrastive_reasoning/reproduce.txt` (case list + selected mutant ids) |
| `file` | `fl_file/fl_file.py` | LLM, crash report, testcase | `output_file_extend.txt`, `fl_file_result.txt/.json` |
| `func` | `fl_method/func_fliter.py` then `fl_method/fl_method.py` | LLM, kernel tree at the case commit, `cflow`, git | `candidate_functions.txt`, `fl_method_result.txt/.json` |
| `export` | `export.py` | — | `file_func_result_v32.txt`, `file_func_result_v32.json` |
| `rootcause` | `root_cause.py` | LLM (only for gaps) | `rc_by_rank.jsonl` |
| `evaluate` | `evaluation/evaluation_file.py`, `evaluation/evaluation_method.py` | oracle files | `logs/06_*.log`, `logs/07_*.log` (Top-N / MRR / MAR / MFR) |

Presets: `localize` = `file,func,export,rootcause` (default),
`contrastive` = `mutation,reproduce`, `all` = everything.

Stages are executed as subprocesses from their own directory (like the
original `run_all.py` did), so the relative prompt paths keep working and each
stage keeps its own log under `logs/`.

`func` needs the kernel tree checked out at the case commit
(`case_commit.txt`); `func_fliter.py` runs `git checkout -f <commit>` itself.

### 5.3 Inputs

| Input | Source |
|---|---|
| crash report | `<BUG_REPORT_DIR>/<syzid>.txt` |
| reproducer | `<TESTCASE_DIR>/<syzid>.syz` |
| kernel commit | `<COMMIT_FILE>` (`func` stage) |
| kernel tree | `--work-dir` (default `KERNEL_DIR_ORI`) |
| case list | `--case-list` / `--syzid` (both `syzid` and `syzid/m1/m2/m3/m4` layouts accepted) |
| mutation hints | `--mutation-result`; if absent an empty index is used and the prompt placeholders become `N/A` |
| ground truth (optional) | `LOC_ORACLE_FILE_LIST`, `LOC_ORACLE_FUNC_LIST` |

### 5.4 Outputs

```
<LOCALIZATION_OUTPUT_DIR>/run-<UTC timestamp>/
├── case_list.txt                 # normalised input (syzid/0/0/0/0)
├── fl_file_result.txt/.json      # file-level Top-K
├── candidate_functions.txt       # RankNet-filtered candidate functions
├── fl_method_result.txt/.json    # function-level Top-K + root_cause_hypothesis
├── file_func_result_v32.txt      # <-- consumed by the repair half
├── file_func_result_v32.json     # same ranking, root-cause text kept
├── rc_by_rank.jsonl              # <-- consumed by the repair half
└── logs/
```

`file_func_result_v32.txt` — the block format the repair half parses
(`load_task_queue_from_loc_result`):

```
Response for case 60c52ca98513a8760a91
1. commit_creds:kernel/cred.c
2. io_issue_sqe:fs/io_uring.c
...
```

`rc_by_rank.jsonl` — one object per case (`load_task_queue_from_rootcause_jsonl`):

```json
{"case_id": "60c52ca98513a8760a91", "hypotheses": [
  {"rank": 1, "function_name": "commit_creds", "hypothesis": "..."}]}
```

`--publish` copies the two onto `paths.LOC_RESULT_FILE` and
`paths.ROOT_CAUSE_JSONL`, i.e. exactly the files `main.py` of the repair half
reads; without `--publish` the run stays self-contained in its own directory.

A snapshot of both files (210 cases, produced by the pipeline before this
package was assembled) ships in `data/datasets/` so that the repair half can be
exercised without running localization at all — see §8.

---

## 6. Embedding service — BGE-M3 (`EMBED_API_URL`)

Both knowledge-base retrievals are **dense**: the case-level semantic KB
(`core/Hybrid_patch_rag.py`) and the golden-patch KB
(`skill/kernel_fix_expert.py`) encode the *query* with an external embedding
service. The model is **BGE-M3** (`bge-m3`, 1024-d), served by a small Flask
process; it is **not** loaded in-process by this package.

### 6.1 Starting the service (`tools/embed_server/flask_embedding.py`)

This package ships the service itself. It is a plain Flask app with two routes
and no dependency on the rest of the package.

| Item | Value |
|---|---|
| script | `tools/embed_server/flask_embedding.py` |
| model | BAAI **bge-m3** (1024-d). **Weights are not shipped** (2.2 GB) |
| model directory | `COHIKER_EMBED_MODEL_PATH` / `--model-path` (required, no default) |
| model loading | `transformers.AutoModel` + `AutoTokenizer`, `trust_remote_code=True` |
| pooling | mean pooling over `last_hidden_state` with the attention mask, then L2 normalisation |
| truncation | `max_length=1024` (`COHIKER_EMBED_MAX_LENGTH`) |
| device | `COHIKER_EMBED_DEVICE` / `--device`; default `auto` → `cuda:1` with ≥2 GPUs, `cuda:0` with one, `cpu` with none |
| port | `COHIKER_EMBED_PORT` / `--port`, default `6001`; routes `POST /embed`, `GET /health` |
| requires | `flask`, `torch`, `transformers` (imported lazily, so `--help` works without them) |

```bash
# 1. fetch the weights once (not part of the package)
huggingface-cli download BAAI/bge-m3 --local-dir /data/beg-m3

# 2. start the service
export COHIKER_EMBED_MODEL_PATH=/data/beg-m3   # required
export COHIKER_EMBED_PORT=6001                 # optional (default 6001)
export COHIKER_EMBED_DEVICE=auto               # optional
python tools/embed_server/flask_embedding.py

# 3. liveness probe
curl -s http://127.0.0.1:6001/health
# {"status":"ok","model":"bge-m3","dim":1024,"device":"cuda:1"}
```

`--help` lists every flag; `--model-path/--port/--device/--max-length` override
the environment variables. The pooling, normalisation and `max_length` must stay
as they are: the 21 132 `intent_vec` blobs in `data/golden_kernel_test_v3.db`
(and the `*_vec` columns of the semantic KB) were pre-computed with exactly this
computation, so changing it would make query and stored vectors incompatible.

### 6.2 Protocol

```
POST <EMBED_API_URL>
{"content": "<text, truncated to 8000 chars by the clients>"}

200 {"embedding": [float, ... 1024 ...], "model": "bge-m3", "dim": 1024}
400 {"error": "Content is empty"}

GET /health
200 {"status": "ok", "model": "bge-m3", "dim": 1024, "device": "cuda:1"}
```

```bash
curl -s -X POST http://127.0.0.1:6001/embed \
     -H 'Content-Type: application/json' \
     -d '{"content":"kernel BUG at mm/slub.c:5024"}' | head -c 200
```

### 6.3 Clients and URL forms

| Caller | Call site | Timeout | URL handling |
|---|---|---|---|
| semantic KB query encoder | `core/Hybrid_patch_rag.py:210` | 20 s, 2 tries | uses `EMBED_API_URL` verbatim |
| golden-patch KB | `skill/kernel_fix_expert.py:245` (`EmbeddingClient`) | 10 s | uses `EMBED_API_URL` verbatim |

So both `http://127.0.0.1:6001/embed` and an OpenAI-style
`http://<host>:6001/v1/embeddings` are accepted. All clients set
`session.trust_env = False`, i.e. **proxy environment variables are ignored** —
the URL must be reachable directly. A remote service is addressed by replacing
the host: `http://<embedding-host>:6001/embed`.

Consequently an embedding service that is only reachable **through** an HTTP
proxy cannot be used: run it on the machine that executes the pipeline, or on
one that is directly reachable from there.

### 6.4 Failure behaviour (important)

If the service is down or unreachable, the clients return `None` instead of
raising, but the two consumers react **differently**:

| Consumer | Reaction to a missing embedding |
|---|---|
| `core/Hybrid_patch_rag.py` (`search_historical_fixes`) | degrades: the dense term is skipped and the hybrid score falls back to its sparse part, the run continues |
| `skill/kernel_fix_expert.py` (`query_golden_fixes`) | **hard failure**: `run()` returns `{"status": "error", "message": "Failed to generate embeddings", "results": []}` because both the intent vector and the code vector are mandatory (`kernel_fix_expert.py:688`) |

Check the log for `Embedding API` lines before trusting a result: a silent
`status: "error"` from `query_golden_fixes` means BGE-M3 was not running.

---

## 7. Kernel build cache (`CACHE_ROOT`)

Compiling a kernel per candidate patch is the dominant cost of validation, so
the system keeps one **full build snapshot per kernel commit** and reuses it
across cases, workers and runs.

### 7.1 Layout and lifecycle

```
<CACHE_ROOT>/<kernel_commit>/          # full rsync snapshot of a *built* kernel tree
├── vmlinux  bzImage  arch/  drivers/  ...  (≈ 9.8 GB populated)
└── .config, include/config/auto.conf.cmd, ...   (absolute build paths recorded inside)
```

* `KbuildManager.set_cache_path(commit)` → `cache_dir = <CACHE_ROOT>/<commit>`
  (created if missing).
* `load_full_snapshot()`:
  * snapshot present and non-empty → clear the worker tree, `rsync -aH --delete`
    cache → worker tree, then `_perform_path_surgery()` rewrites stale absolute
    paths inside `*.cmd` / `.config` (`sed s|<old>|<new>|g` over
    `paths.KERNEL_PATH_CANDIDATES`) so the tree is usable from the new location;
  * snapshot missing/empty (**first run**) → clear the worker tree,
    `rsync -aH --delete` `KERNEL_DIR_ORI` → worker tree, apply the per-case
    `bug_config` (`.config` + `make olddefconfig`), return.

  The candidate list used by the path surgery is
  `KERNEL_LEGACY_DIR` + `COHIKER_KERNEL_WORKER_PATTERN` (default
  `/data/linux-worker-{index}`) expanded over `COHIKER_KERNEL_MAX_WORKERS`
  (default `10`) — i.e. `/data/linux`, `/data/linux-worker-0` …
  `/data/linux-worker-9`. It is simply *the set of directories a cache may have
  been built in*. If your cache was built somewhere else, extend that pattern;
  otherwise the rewrite finds nothing and `make` rebuilds the affected objects
  from scratch.
* `compile_kernel()` → `make -j<COHIKER_NUM_CORES> bzImage` (incremental once a
  snapshot exists); `targeted_make(file)` → `make <file>.o` as a fast syntax /
  linkage check before the full build.
* `save_full_snapshot()` → `rsync -aH --delete` worker tree → `cache_dir`.

Explicit **warm-up**: `agent/fix_thread_graph.py::_warmup_kernel_cache()` is
called once before the parallel fix threads start. It is skipped when
`<CACHE_ROOT>/<commit>/vmlinux` already exists; otherwise it clears worker-0,
copies `KERNEL_DIR_ORI` there, `git checkout -f <commit>`, applies the bug
config, runs `make -j<NUM_CORES> bzImage` and saves the snapshot. This is the
supported way to populate the cache ahead of a batch run.

### 7.2 Cost: first run vs. cache hit

| Situation | What happens | Cost |
|---|---|---|
| first run for a commit (no snapshot) | full kernel build of `bzImage` from `KERNEL_DIR_ORI` + bug config | **long**: tens of minutes on a many-core host, up to hours on a small one; plan the first case of a batch as a warm-up |
| snapshot hit | `rsync` of ≈ 10 GB into the worker tree + path surgery + incremental `make bzImage` | minutes (I/O bound) |
| repeat validation of the same commit | same as above; only the touched object files rebuild | fastest |

**Memory**: a kernel build wants roughly 1.5–2 GB RAM per `make` job, so the
default `COHIKER_NUM_CORES=20` peaks around 30–40 GB; keep it well below the
core count on a small box (`COHIKER_NUM_CORES=8` → ~15 GB). Each QEMU guest
adds `COHIKER_QEMU_MEM` (default `4G`) per worker. Disk: each populated snapshot
is ≈ 10 GB (1.6 TB / 181 commits in the original machine).

Dependencies of the cache path (all verified present on the original host):
`rsync` (`-aH`, `--delete`), `make`, `gcc`, `git`, `sed`, `xargs`, `find`, and
write access to both the worker tree and `CACHE_ROOT`.

### 7.3 Known limitations of the reuse logic

* `load_full_snapshot()` returns `True` for the **first-run** branch too (it
  only copied the pristine tree and applied the config), so the
  `save_full_snapshot()` calls at the end of the miss branches in
  `skill/sandbox_validator.py` and `core/validate_main.py` are unreachable.
  Only `_warmup_kernel_cache()` actually persists a snapshot — treat the
  warm-up as mandatory after a cold start, otherwise every case rebuilds.
* The cache key is the commit string exactly as it appears in
  `case_commit.txt`; the original `CACHE_ROOT` contains both abbreviated and
  full-SHA directories for the same commit (e.g. `02d5e016800d/` and
  `02d5e016800d08…/`), which means a change in the identifier causes a miss and
  a fresh multi-GB build.
* Path surgery only knows `paths.KERNEL_PATH_CANDIDATES`; a snapshot built at a
  path outside that list cannot be relocated and silently degrades to a full
  rebuild (`make` sees stale dependencies).
* `_clear_workspace()` and `rsync --delete` are destructive: the worker
  directory (`/data/linux-worker-N`) is emptied, `.git` included. Never point
  `KERNEL_WORKER_DIRS` at the pristine tree.
* Empty 4 KB cache directories exist (an aborted build); they are treated as a
  miss because the loader checks for a non-empty directory, and the warm-up
  additionally requires `vmlinux`.

---

## 8. Repair without localizing again

`data/datasets/` already contains one snapshot of the localization output, so a
repair run needs neither the LLM localization stages nor the localization kernel
work. This is `--mode repair` of the one-command runner (§0.2):

```bash
bash run_pipeline.sh --mode repair --syzid 60c52ca98513a8760a91
# equivalent, without the preflight/log wrapper:
#   export LLM_API_KEY=...
#   python main.py --syzid 60c52ca98513a8760a91
```

`main.py` reads `<DATASETS_DIR>/file_func_result_v32.txt` and
`<DATASETS_DIR>/rc_by_rank.jsonl` (§5.4) instead of running localization. The
crash report, testcase, `.config`, kernel commit and oracle keyword for all 210
cases are in the same directory. `DATASETS_DIR` resolves to
`$COHIKER_DATASETS_DIR` → `/data/datasets` → the bundled mirror, in that order,
so set `COHIKER_DATASETS_DIR=$PWD/data/datasets` if `/data/datasets` exists on
your machine but holds different data.

To refresh the snapshot with your own localization run:

```bash
python localization/main.py --case-list data/datasets/case_list_all.txt --publish
```

The snapshot is a record of an earlier pipeline run (210 cases, file date
2026-04-20); it is not regenerated automatically.

---

## 9. Dependencies

```bash
pip install -r requirements.txt
```

`requirements.txt` covers the whole package: `openai`, `requests`, `numpy`,
`torch`, `tqdm`, `rank_bm25` (the BM25 half of the semantic KB),
`langchain-core` + `langgraph` (the repair graph), `tree-sitter` **pinned to
0.21.3** (0.22 removed the `Language(path, name)` constructor both halves use),
and `flask` + `transformers` for the bundled BGE-M3 service (§6). The file also
lists the exact versions the pipeline was verified with, and the two optional
dependencies (`matplotlib`, `scikit-learn`) needed only to retrain
`localization/fl_method/filter_model.pt`.

On a CUDA host install the matching torch build instead of the default wheel,
e.g. `pip install torch --index-url https://download.pytorch.org/whl/cu124`.

System tools: `git`, `rsync`, `make`, `gcc` and a kernel toolchain (validation),
`cflow` (function features), `qemu-system-x86_64` + syzkaller (reproduction),
and a Go toolchain if syzkaller has to be (re)built. The BGE-M3 service runs as a
separate process and needs the model weights on disk.

---

## 10. License and citation

* **Code** (`agent/`, `core/`, `skill/`, `tool/`, `utils/`, `localization/`,
  `tools/`, scripts, configuration): MIT — see [`LICENSE`](LICENSE).
* **Data** (`data/`): crash reports and reproducers come from
  [syzbot](https://syzkaller.appspot.com); patches, commit messages and the
  derived knowledge bases contain material from the Linux kernel (GPL-2.0).
  Reuse them under their original terms.
* Citation metadata is in [`CITATION.cff`](CITATION.cff); GitHub shows a
  "Cite this repository" button for it.

The two SQLite knowledge bases are distributed as the `knowledge_bases.zip`
release asset (see §3), not through git.

