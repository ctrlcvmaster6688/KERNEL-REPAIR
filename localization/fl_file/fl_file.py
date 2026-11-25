import os
import sys
import json
import time
import threading
import argparse
import re
import subprocess
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI, OpenAIError
import requests
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Optional

# The localization package root (one level up) provides the shared LLM client
# and the path constants; every absolute path comes from the repository paths.py.
sys.path.append(str(Path(os.path.abspath(__file__)).parent.parent))
from llm import LLMHelper  # noqa: E402

MAX_RETRIES = 3
MAX_INPUT_LENGTH = 61000
MAX_THREADS = 20
MAX_REQUESTS_PER_MINUTE = 40
REQUEST_INTERVAL = 60 / MAX_REQUESTS_PER_MINUTE

output_lock = threading.Lock()
rate_limit_lock = threading.Lock()
last_request_time = [0.0]

llm_helper = LLMHelper()

def wait_for_rate_limit():
    with rate_limit_lock:
        now = time.time()
        elapsed = now - last_request_time[0]
        if elapsed < REQUEST_INTERVAL:
            time.sleep(REQUEST_INTERVAL - elapsed)
        last_request_time[0] = time.time()

def read_file(file_path, max_length=None):
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            content = file.read()
            return content[:max_length] if max_length else content
    except FileNotFoundError:
        print(f"Error: File '{file_path}' not found.")
        return ""


def replace_extend_placeholders(content, bug_report, code_line):
    content = content.replace("{BUG_REPORT_HERE}", bug_report or "N/A")
    content = content.replace("{SYZ_HERE}", code_line or "N/A")
    return content

def replace_fl_placeholders(content,repro_content, mutate_content1, mutate_content2,mutate_content3,mutate_content4,report_content,filelist):
    content = content.replace("{TEST_PROGRAM_HERE}", repro_content or "N/A")
    content = content.replace("{MUTATE1_HERE}", mutate_content1 or "N/A")
    content = content.replace("{MUTATE2_HERE}", mutate_content2 or "N/A")
    content = content.replace("{MUTATE3_HERE}", mutate_content3 or "N/A")
    content = content.replace("{MUTATE4_HERE}", mutate_content4 or "N/A")
    content = content.replace("{FILE_LIST_HERE}", filelist or "N/A")
    content = content.replace("{BUG_REPORT_HERE}", report_content or "N/A")
    return  content

def process_syz_file(file_path, output_path=None):
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    processed_lines = []
    for line in lines:
        if line.startswith("syz_mount_image$"):
            line = re.sub(r"(syz_mount_image\$\w+).*", r"\1()", line)
        processed_lines.append(line)

    result = "".join(processed_lines)

    if output_path:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(result)
    return result


def extract_json_payload(raw_text: str) -> Optional[Dict[str, Any]]:
    if not raw_text:
        return None

    raw_text = raw_text.strip()
    try:
        payload = json.loads(raw_text)
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        pass

    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start == -1 or end == -1 or start >= end:
        return None

    try:
        payload = json.loads(raw_text[start:end + 1])
        return payload if isinstance(payload, dict) else None
    except json.JSONDecodeError:
        return None


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = []
        for item in value:
            part = normalize_text(item)
            if part:
                parts.append(part)
        return "\n".join(parts)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def ensure_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def extract_ranked_files_from_text(raw_text: str) -> list:
    if not raw_text:
        return []
    files = re.findall(r'"([^"]+\.(?:c|h|S))"', raw_text)
    return list(dict.fromkeys(files))


def build_fallback_file_payload(raw_text: str) -> Dict[str, Any]:
    files = extract_ranked_files_from_text(raw_text)
    return {
        "thought_process": {},
        "ranked_results": [
            {
                "rank": idx + 1,
                "file": file_path,
                "root_cause_hypothesis": "",
            }
            for idx, file_path in enumerate(files[:10])
        ],
        "standard_output": files[:10],
    }


def normalize_file_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    payload = ensure_dict(payload)
    thought_process = ensure_dict(payload.get("thought_process"))
    ranked_results = payload.get("ranked_results", [])
    standard_output = payload.get("standard_output", [])

    fallback_hypothesis = normalize_text(
        ensure_dict(thought_process.get("step_5")).get("details") or
        ensure_dict(thought_process.get("step_7")).get("details")
    )

    normalized_results = []
    seen = set()
    if isinstance(ranked_results, list):
        for item in ranked_results:
            if not isinstance(item, dict):
                continue
            file_path = normalize_text(item.get("file"))
            if not file_path or file_path in seen:
                continue
            normalized_results.append({
                "rank": len(normalized_results) + 1,
                "file": file_path,
                "root_cause_hypothesis": normalize_text(item.get("root_cause_hypothesis")) or fallback_hypothesis,
            })
            seen.add(file_path)
            if len(normalized_results) >= 10:
                break

    if isinstance(standard_output, list):
        for item in standard_output:
            file_path = normalize_text(item)
            if not file_path or file_path in seen:
                continue
            normalized_results.append({
                "rank": len(normalized_results) + 1,
                "file": file_path,
                "root_cause_hypothesis": fallback_hypothesis,
            })
            seen.add(file_path)
            if len(normalized_results) >= 10:
                break

    payload["ranked_results"] = normalized_results
    payload["standard_output"] = [item["file"] for item in normalized_results]
    return payload


def build_case_json_record(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "top_10": payload.get("ranked_results", []),
        "thought_process": payload.get("thought_process", {}),
    }


def find_case_file(folder, syzid, extensions):
    folder_path = Path(folder)
    for ext in extensions:
        candidate = folder_path / f"{syzid}{ext}"
        if candidate.exists():
            return str(candidate)
    return str(folder_path / f"{syzid}{extensions[0]}")


def process_extend_entry(entry, report_folder, testcase_folder, base_prompt, standard_output, output_file):
    syzid = entry[0]
    report_path = find_case_file(report_folder, syzid, [".txt", ".log"])
    syz_path = find_case_file(testcase_folder, syzid, [".syz"])
    report_content = read_file(report_path)
    syz_content = process_syz_file(syz_path)

    prompt = replace_extend_placeholders(base_prompt, report_content, syz_content)
    retries = 0
    while retries < MAX_RETRIES:
        try:
            wait_for_rate_limit()
            p = [
                {'role': 'user', 'content': prompt},
                {'role': 'assistant', 'content': standard_output}
            ]
            response, time_cost = llm_helper.query(p, number_of_result=1)

            result = response.choices[0].message.content if response.choices else ""
            with output_lock:
                with open(output_file, "a", encoding="utf-8") as output:
                    output.write(f"response for {syzid}:\n{result}\n")
            return
        except (requests.exceptions.Timeout, OpenAIError) as e:
            retries += 1
            print(f"Warning: {syzid} API request fail  (try{retries}/{MAX_RETRIES}), error : {e}")
            time.sleep(2 ** retries)
        except Exception as e:
            print(f"Error: during processing {syzid} : {e}")
            return

def parse_mutations(file_path):
    with open(file_path, 'r') as file:
        lines = file.readlines()

    mutations_map = defaultdict(list)
    current_case_id = None

    for i, line in enumerate(lines):
        if line.startswith("Response for"):
            current_case_id = line.split()[2]
            continue

        if "// Original:" in line and current_case_id:
            original_line = line.split("// Original:")[1].strip()
            mutated_line = lines[i + 1].strip()
            mutations_map[current_case_id].append((original_line, mutated_line))

    return dict(mutations_map)

def extract_mutation(mutations_map, case_id, mutate_id):
    if case_id == 0 or mutate_id == '0':
        return None
    if case_id in mutations_map:
        mutations = mutations_map[case_id]
        if 1 <= int(mutate_id) <= len(mutations):
            original_line, mutated_line = mutations[int(mutate_id) - 1]
            return f"{original_line} -> {mutated_line}"
    return None

def extract_files_by_regex(filepath, target_id):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()

    pattern = rf'response for {re.escape(target_id)}:.*?(?=(^response for |\Z))'
    match = re.search(pattern, content, flags=re.DOTALL | re.MULTILINE | re.IGNORECASE)

    if not match:
        raise ValueError(f"not found ID: {target_id}")

    block = match.group(0)
    file_pattern = r'"([^"]+\.(?:c|h|S))"'
    files = re.findall(file_pattern, block)

    return sorted(set(files))

def process_fl_entry(entry, report_folder, testcase_folder, base_prompt, standard_output, output_file,
                     mutate_result_dir, extend_output, json_results=None):
        syzid = entry[0]
        mutate_num1=entry[1]
        mutate_num2=entry[2]
        mutate_num3=entry[3]
        mutate_num4=entry[4]
        mutate_map=parse_mutations(mutate_result_dir)
        report_path = find_case_file(report_folder, syzid, [".txt", ".log"])
        syz_path = find_case_file(testcase_folder, syzid, [".syz"])
        report_content = read_file(report_path)
        syz_content = process_syz_file(syz_path)
        mutate_content1= extract_mutation(mutate_map,syzid, mutate_num1)
        mutate_content2= extract_mutation(mutate_map,syzid, mutate_num2)
        mutate_content3= extract_mutation(mutate_map,syzid, mutate_num3)
        mutate_content4= extract_mutation(mutate_map,syzid, mutate_num4)
        filelist=extract_files_by_regex(extend_output,syzid)
        filelist= '\n'.join(filelist)
        prompt = replace_fl_placeholders(base_prompt, syz_content,mutate_content1, mutate_content2, mutate_content3, mutate_content4, report_content, filelist)
        retries = 0
        while retries < MAX_RETRIES:
            try:
                wait_for_rate_limit()

                p = [
                {'role': 'user', 'content': prompt},
                {'role': 'assistant', 'content': standard_output}
                ]
                response, time_cost = llm_helper.query(p, number_of_result=1)
                result = response.choices[0].message.content if response.choices else ""
                payload = extract_json_payload(result)
                if not isinstance(payload, dict):
                    payload = build_fallback_file_payload(result)
                payload = normalize_file_payload(payload)
                serialized = json.dumps(payload, ensure_ascii=False, indent=2)
                with output_lock:
                    with open(output_file, "a", encoding="utf-8") as output:
                        output.write(f"response for {syzid}:\n{serialized}\n")
                    if json_results is not None:
                        json_results[syzid] = build_case_json_record(payload)
                return
            except (requests.exceptions.Timeout, OpenAIError) as e:
                retries += 1
                print(f"Warning: {syzid} API request fail  (try{retries}/{MAX_RETRIES}), error : {e}")
                time.sleep(2 ** retries)
            except Exception as e:
                print(f"Error: during in fl processing {syzid} : {e}")
                return

def process_case(entry, args, base_prompt_extend, standard_output_extend, base_prompt_fl, standard_output_fl, json_results):
    """Merges the two processing stages for a single case and prints logs."""
    syzid = entry[0]
    # Calculate the number of valid mutations
    num_mutants = sum(1 for m in entry[1:] if m != '0')
    print(f"Processing {syzid} with {num_mutants} mutants")

    # Stage 1: Extend Scope
    try:
        process_extend_entry(
            entry, args.report_dir, args.testcase_dir,
            base_prompt_extend, standard_output_extend, args.output_extend
        )
    except Exception as e:
        print(f"Error during extend processing for {syzid}: {e}")
        return  # Stop processing this case if the first stage fails

    # Stage 2: File Localization
    try:
        process_fl_entry(
            entry, args.report_dir, args.testcase_dir,
            base_prompt_fl, standard_output_fl, args.localize_output,
            args.mutation_output, args.output_extend, json_results
        )
    except Exception as e:
        print(f"Error during fl processing for {syzid}: {e}")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--case_list', required=True, help='Path to case list')
    parser.add_argument('--report_dir', required=True, help='Directory containing reports')
    parser.add_argument('--testcase_dir', required=True, help='Directory containing test cases')
    # input
    parser.add_argument('--mutation_output', required=True, help='Path to parse mutation result')
    # output
    parser.add_argument('--output_extend', required=True, help='Path to save file list extend output')
    parser.add_argument('--localize_output', required=True, help='Path to save localize result')
    parser.add_argument('--json_output', required=False, help='Path to save structured file-level json output')
    args = parser.parse_args()

    # Clear output files before starting to prevent appending to old results
    open(args.output_extend, 'w').close()
    open(args.localize_output, 'w').close()

    with open(args.case_list, 'r', encoding="utf-8") as f:
        entries = [line.strip().split('/') for line in f]

    print(f"Start to localize {len(entries)} cases...")

    base_prompt_extend = read_file("../prompt/extend_scope_prompt.txt", MAX_INPUT_LENGTH)
    standard_output_extend = read_file("../prompt/extend_scope_standard_output.txt", MAX_INPUT_LENGTH)
    base_prompt_fl = read_file("../prompt/fl_file_prompt.txt", MAX_INPUT_LENGTH)
    standard_output_fl = read_file("../prompt/fl_file_standard_output.txt", MAX_INPUT_LENGTH)
    case_json_results = {}

    with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
        futures = [
            executor.submit(
                process_case,
                entry,
                args,
                base_prompt_extend,
                standard_output_extend,
                base_prompt_fl,
                standard_output_fl,
                case_json_results,
            ) for entry in entries
        ]

        # Use tqdm to display a progress bar for overall progress
        for future in tqdm(as_completed(futures), total=len(entries), desc="Overall Progress"):
            try:
                future.result()  # Get the result to trigger any exceptions
            except Exception:
                # Exceptions are already printed in the worker function, no need to handle here
                pass

    print(f"\nFinished.")
    print(f"Outputs are stored in {args.localize_output}")
    if args.json_output:
        json_output_path = Path(args.json_output)
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(json.dumps(case_json_results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Structured JSON is stored in {json_output_path}")


if __name__ == "__main__":
    main()
