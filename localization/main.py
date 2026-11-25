"""Single entry point for the localization half of CoHiKer++.

One command runs every stage in order, for one case or for a batch::

    python localization/main.py --syzid 60c52ca98513a8760a91
    python localization/main.py --case-list <path/to/case_list.txt>

Stages (``--stages`` accepts names, comma lists or presets):

    mutation     LLM mutant generation                    (needs the LLM only)
    reproduce    mutant reproduction in QEMU + syzkaller   (needs the QEMU pool)
    file         file-level localization                  (LLM)
    func         candidate-function filtering (RankNet) + function localization (LLM)
    export       -> file_func_result_v32.txt (Top-K, the repair half's input)
    rootcause    -> rc_by_rank.jsonl (per-case root cause, the repair half's input)
    evaluate     file/function metrics against the oracle files (optional)

Presets: ``localize`` = file,func,export,rootcause (default),
``all`` = mutation,reproduce,file,func,export,rootcause,evaluate,
``contrastive`` = mutation,reproduce.

Notes
-----
* Every path comes from ``paths.py`` through ``localization/settings.py``; the
  command line only ever overrides those defaults.
* ``--publish`` copies the two exported artifacts to the exact files the repair
  half reads (``paths.LOC_RESULT_FILE`` / ``paths.ROOT_CAUSE_JSONL``), which is
  what chains localization -> repair without manual copying.
* Stage scripts are executed as subprocesses from their own directory, exactly
  like the original ``run_all.py`` did, so their relative prompt/template paths
  keep working and each stage keeps its own stdout log.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

_LOCALIZATION_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _LOCALIZATION_DIR.parent
for _path in (str(_REPO_ROOT), str(_LOCALIZATION_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import settings  # noqa: E402

STAGE_ORDER = ["mutation", "reproduce", "file", "func", "export", "rootcause", "evaluate"]
PRESETS: Dict[str, List[str]] = {
    "localize": ["file", "func", "export", "rootcause"],
    "contrastive": ["mutation", "reproduce"],
    "all": list(STAGE_ORDER),
}


def resolve_stages(spec: str) -> List[str]:
    """Map a ``--stages`` value (names, comma list or preset) to ordered stages."""
    wanted: List[str] = []
    for token in spec.replace(",", " ").split():
        token = token.strip().lower()
        if not token:
            continue
        if token in PRESETS:
            wanted.extend(PRESETS[token])
        elif token in STAGE_ORDER:
            wanted.append(token)
        else:
            raise SystemExit(
                f"unknown stage '{token}'; choose from "
                f"{', '.join(STAGE_ORDER)} or a preset ({', '.join(PRESETS)})"
            )
    unknown = [stage for stage in wanted if stage not in STAGE_ORDER]
    if unknown:
        raise SystemExit(f"unknown stages: {unknown}")
    return [stage for stage in STAGE_ORDER if stage in set(wanted)]


def run_step(name: str, cmd: List[str], cwd: Path, log_path: Path) -> str:
    """Run one stage script (subprocess, own log file), mirroring run_all.py."""
    print(f"\n[stage] {name}")
    print(f"[stage] cwd: {cwd}")
    print(f"[stage] cmd: {' '.join(cmd)}")

    log_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(cmd, cwd=str(cwd), text=True, capture_output=True)

    combined = (result.stdout or "") + (
        ("\n[stderr]\n" + result.stderr) if result.stderr else ""
    )
    log_path.write_text(combined, encoding="utf-8")
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
    if result.returncode != 0:
        raise SystemExit(f"[stage] {name} failed (exit {result.returncode}); log: {log_path}")
    return combined


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="localization/main.py",
        description="Run the CoHiKer++ fault-localization pipeline in one command "
                    "(single case or batch) and export the Top-K ranking plus the "
                    "per-case root-cause hypotheses consumed by the repair half.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python localization/main.py --syzid 60c52ca98513a8760a91\n"
            "  python localization/main.py --case-list <path/to/case_list.txt> --publish\n"
            "  python localization/main.py --syzid <syzid> --stages all\n"
            "  python localization/main.py --syzid <syzid> --stages export,rootcause "
            "--method-json <run>/fl_method_result.json --no-llm\n"
        ),
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--syzid", help="Run a single case (writes a one-line case list).")
    source.add_argument("--case-list", help="Batch case list; one syzid (optionally "
                                           "syzid/m1/m2/m3/m4) per line.")
    parser.add_argument("--stages", default="localize",
                        help="Stages or preset to run (default: localize).")
    parser.add_argument("--output-dir", default=None,
                        help="Run directory (default: <LOCALIZATION_OUTPUT_DIR>/run-<timestamp>).")

    group = parser.add_argument_group("inputs")
    group.add_argument("--report-dir", default=str(settings.BUG_REPORT_DIR))
    group.add_argument("--testcase-dir", default=str(settings.TESTCASE_DIR))
    group.add_argument("--work-dir", default=str(settings.KERNEL_DIR),
                       help="Kernel source tree (git checkout at the case commit).")
    group.add_argument("--commit-map", default=str(settings.COMMIT_FILE))
    group.add_argument("--bug-config-dir", default=str(settings.BUG_CONFIG_DIR))
    group.add_argument("--oracle-content", default=str(settings.ORACLE_FILE))
    group.add_argument("--image-path", default=str(settings.IMAGE_PATH),
                       help="Directory holding bullseye.img / bullseye.id_rsa.")
    group.add_argument("--syzkaller-path", default=str(settings.SYZKALLER_PATH))
    group.add_argument("--cache-linux-dir", default=str(settings.CACHE_ROOT))
    group.add_argument("--file-oracle", default=str(settings.ORACLE_FILE_LIST))
    group.add_argument("--func-oracle", default=str(settings.ORACLE_FUNC_LIST))
    group.add_argument("--mutation-result", default=None,
                       help="Existing mutation-result file (contrastive stage output). "
                            "If omitted, an empty one is created: prompts then get N/A.")

    group = parser.add_argument_group("outputs")
    group.add_argument("--publish", action="store_true",
                       help="Also copy the two exported artifacts onto the files the "
                            "repair half reads (paths.LOC_RESULT_FILE / "
                            "paths.ROOT_CAUSE_JSONL).")
    group.add_argument("--no-root-cause-llm", action="store_true",
                       help="Never call the LLM in the root-cause stage; only reuse "
                            "hypotheses already produced by the function-level stage.")
    return parser


def prepare_case_list(args, out_dir: Path) -> Path:
    """Normalize the case list into the five-field layout the stages expect."""
    import case_list

    target = out_dir / "case_list.txt"
    if args.syzid:
        source = out_dir / "case_list.input"
        source.write_text(args.syzid.strip() + "\n", encoding="utf-8")
    else:
        source = Path(args.case_list) if args.case_list else settings.CASE_LIST_FILE
        if not source.exists():
            raise SystemExit(f"case list not found: {source}")
    case_ids, without_hints = case_list.normalize_case_list(source, target)
    if not case_ids:
        raise SystemExit(f"no case id found in {source}")
    if without_hints:
        print(f"[case] {without_hints}/{len(case_ids)} case(s) have no mutation ids; "
              f"the mutation placeholders of the prompts will be 'N/A'.")
    return target


def stage_mutation(args, out_dir: Path, case_list_path: Path) -> Path:
    contrastive = out_dir / "contrastive_reasoning"
    output = contrastive / "mutation_result.txt"
    run_step(
        "mutation",
        [sys.executable, "mutation_generate.py",
         "--case_list", str(case_list_path),
         "--report_dir", args.report_dir,
         "--testcase_dir", args.testcase_dir,
         "--output_file", str(output),
         "--mutation_case_dir", str(contrastive / "mutations"),
         "--mutation_detail_file", str(contrastive / "mutation_details.txt"),
         "--analysis_output", str(contrastive / "mutation_analysis.json")],
        cwd=_LOCALIZATION_DIR / "contrastive_reasoning",
        log_path=out_dir / "logs" / "01_mutation_generate.log",
    )
    return output


def stage_reproduce(args, out_dir: Path, case_list_path: Path, mutation_file: Path) -> Path:
    contrastive = out_dir / "contrastive_reasoning"
    output = contrastive / "reproduce.txt"
    run_step(
        "reproduce",
        [sys.executable, "testcase_reproduce.py",
         "--case_list", str(case_list_path),
         "--mutation_case_dir", str(contrastive / "mutations"),
         "--image_path", args.image_path,
         "--output_file", str(output),
         "--linux_dir", args.work_dir,
         "--bug_config_dir", args.bug_config_dir,
         "--log_file", str(out_dir / "logs" / "02_testcase_reproduce.log"),
         "--cache_linux_dir", args.cache_linux_dir,
         "--syzkaller_path", args.syzkaller_path,
         "--analysis_output", str(contrastive / "reproduce_analysis.json"),
         "--commit_map", args.commit_map,
         "--oracle_content", args.oracle_content],
        cwd=_LOCALIZATION_DIR / "contrastive_reasoning",
        log_path=out_dir / "logs" / "02_testcase_reproduce.stdout.log",
    )
    return output


def stage_file(args, out_dir: Path, case_list_path: Path, mutation_file: Path) -> None:
    run_step(
        "file",
        [sys.executable, "fl_file.py",
         "--case_list", str(case_list_path),
         "--report_dir", args.report_dir,
         "--testcase_dir", args.testcase_dir,
         "--output_extend", str(out_dir / "output_file_extend.txt"),
         "--mutation_output", str(mutation_file),
         "--localize_output", str(out_dir / "fl_file_result.txt"),
         "--json_output", str(out_dir / "fl_file_result.json")],
        cwd=_LOCALIZATION_DIR / "fl_file",
        log_path=out_dir / "logs" / "03_fl_file.log",
    )


def stage_func(args, out_dir: Path, case_list_path: Path, mutation_file: Path) -> None:
    candidates = out_dir / "candidate_functions.txt"
    run_step(
        "func:candidates",
        [sys.executable, "func_fliter.py",
         "--case_list", str(case_list_path),
         "--report_dir", args.report_dir,
         "--testcase_dir", args.testcase_dir,
         "--mutation_output", str(mutation_file),
         "--work_dir", args.work_dir,
         "--result_file", str(out_dir / "fl_file_result.txt"),
         "--output_dir", str(candidates),
         "--commit_map", args.commit_map],
        cwd=_LOCALIZATION_DIR / "fl_method",
        log_path=out_dir / "logs" / "04_func_filter.log",
    )
    run_step(
        "func:localize",
        [sys.executable, "fl_method.py",
         "--case_list", str(case_list_path),
         "--report_dir", args.report_dir,
         "--testcase_dir", args.testcase_dir,
         "--func_list", str(candidates),
         "--mutation_output", str(mutation_file),
         "--output_dir", str(out_dir / "fl_method_result.txt"),
         "--json_output", str(out_dir / "fl_method_result.json"),
         "--function_summary_path", str(out_dir / "function_summary.json")],
        cwd=_LOCALIZATION_DIR / "fl_method",
        log_path=out_dir / "logs" / "05_fl_method.log",
    )


def stage_export(args, out_dir: Path, case_ids: List[str]) -> Dict[str, Path]:
    import export

    records = export.load_records(
        method_json=out_dir / "fl_method_result.json",
        method_txt=out_dir / "fl_method_result.txt",
    )
    loc_path = out_dir / "file_func_result_v32.txt"
    json_path = out_dir / "file_func_result_v32.json"
    cases = export.write_loc_result(records, loc_path, order=case_ids)
    export.write_json(records, json_path, order=case_ids)
    print(f"[export] {cases} case(s) -> {loc_path.name} (+ matching .json)")
    return {"loc_result": loc_path, "loc_result_json": json_path}


def stage_rootcause(args, out_dir: Path, case_ids: List[str]) -> Dict[str, Path]:
    import export
    import root_cause

    records = export.load_records(
        method_json=out_dir / "fl_method_result.json",
        method_txt=out_dir / "fl_method_result.txt",
    )
    built, stats = root_cause.build_records(
        records,
        report_dir=Path(args.report_dir),
        use_llm=not args.no_root_cause_llm,
        order=case_ids,
    )
    jsonl_path = out_dir / "rc_by_rank.jsonl"
    cases = root_cause.write_jsonl(built, jsonl_path, order=case_ids)
    print(f"[rootcause] cases={cases} reused={stats['reused']} "
          f"generated={stats['generated']} empty={stats['empty']}")
    return {"root_cause": jsonl_path}


def stage_evaluate(args, out_dir: Path, case_list_path: Path) -> None:
    file_oracle = Path(args.file_oracle)
    func_oracle = Path(args.func_oracle)
    if not file_oracle.exists() or not func_oracle.exists():
        print(f"[evaluate] skipped: oracle files missing ({file_oracle}, {func_oracle})")
        return
    run_step(
        "evaluate:file",
        [sys.executable, "evaluation_file.py",
         "--case_list", str(case_list_path),
         "--result_file", str(out_dir / "fl_file_result.txt"),
         "--oracle_file", str(file_oracle)],
        cwd=_LOCALIZATION_DIR / "evaluation",
        log_path=out_dir / "logs" / "06_evaluation_file.log",
    )
    run_step(
        "evaluate:function",
        [sys.executable, "evaluation_method.py",
         "--case_list", str(case_list_path),
         "--result_file", str(out_dir / "fl_method_result.txt"),
         "--oracle_file", str(func_oracle)],
        cwd=_LOCALIZATION_DIR / "evaluation",
        log_path=out_dir / "logs" / "07_evaluation_method.log",
    )


def preflight_llm(stages: List[str]) -> None:
    """Fail fast when an LLM-based stage is requested without credentials."""
    llm_stages = {"mutation", "file", "func"} & set(stages)
    if not llm_stages:
        return
    import llm

    try:
        llm.get_client()
    except Exception as exc:  # noqa: BLE001 - re-raised with stage context
        raise SystemExit(
            f"stage(s) {', '.join(sorted(llm_stages))} need an LLM: {exc}"
        )


def main(argv: Optional[List[str]] = None) -> int:
    import case_list as case_list_module

    args = build_parser().parse_args(argv)
    stages = resolve_stages(args.stages)
    preflight_llm(stages)
    out_dir = Path(args.output_dir) if args.output_dir else settings.default_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[run] started {datetime.now().isoformat(timespec='seconds')}")
    print(f"[run] output: {out_dir}")
    print(f"[run] stages: {', '.join(stages)}")

    case_list_path = prepare_case_list(args, out_dir)
    case_ids = case_list_module.read_case_ids(case_list_path)
    print(f"[run] cases: {len(case_ids)}")

    mutation_file = Path(args.mutation_result) if args.mutation_result else None
    if mutation_file is not None and not mutation_file.exists():
        raise SystemExit(f"mutation-result file not found: {mutation_file}")
    if mutation_file is None and not ({"mutation", "reproduce"} & set(stages)):
        mutation_file = case_list_module.empty_mutation_file(
            out_dir / "mutation_result.empty.txt"
        )
        print(f"[run] no mutation-result file: using an empty one ({mutation_file.name}); "
              f"prompts receive 'N/A' for the mutation placeholders.")

    produced: Dict[str, Path] = {}
    for stage in stages:
        if stage == "mutation":
            mutation_file = stage_mutation(args, out_dir, case_list_path)
            print(f"[run] mutation result: {mutation_file}")
        elif stage == "reproduce":
            if mutation_file is None:
                raise SystemExit("stage 'reproduce' needs a mutation-result file "
                                 "(--mutation-result) or the 'mutation' stage")
            reproduce_out = stage_reproduce(args, out_dir, case_list_path, mutation_file)
            # the reproduction stage selects the mutants used by the later stages
            case_list_path = reproduce_out
            case_ids = case_list_module.read_case_ids(case_list_path)
            print(f"[run] case list replaced by reproduce output ({len(case_ids)} case(s))")
        elif stage == "file":
            if mutation_file is None:
                mutation_file = case_list_module.empty_mutation_file(
                    out_dir / "mutation_result.empty.txt"
                )
            stage_file(args, out_dir, case_list_path, mutation_file)
        elif stage == "func":
            if mutation_file is None:
                mutation_file = case_list_module.empty_mutation_file(
                    out_dir / "mutation_result.empty.txt"
                )
            stage_func(args, out_dir, case_list_path, mutation_file)
        elif stage == "export":
            produced.update(stage_export(args, out_dir, case_ids))
        elif stage == "rootcause":
            produced.update(stage_rootcause(args, out_dir, case_ids))
        elif stage == "evaluate":
            stage_evaluate(args, out_dir, case_list_path)

    if args.publish:
        mapping = {
            "loc_result": settings.PUBLISHED_LOC_RESULT,
            "root_cause": settings.PUBLISHED_ROOT_CAUSE,
        }
        for key, target in mapping.items():
            source = produced.get(key)
            if not source or not source.exists():
                print(f"[publish] skipped {key}: not produced in this run")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            print(f"[publish] {source.name} -> {target}")

    print(f"\n[run] finished {datetime.now().isoformat(timespec='seconds')}")
    for key, path in produced.items():
        print(f"[run] {key}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
