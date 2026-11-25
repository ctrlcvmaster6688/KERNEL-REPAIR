import os
import json
import time
import re
import requests
import argparse
import threading
from threading import Lock
from tqdm import tqdm
from openai import OpenAI, OpenAIError
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
import sys
from pathlib import Path
from typing import Dict, List
from datetime import datetime

# The localization package root (one level up) provides the shared LLM client;
# every absolute path comes from the repository paths.py.
sys.path.append(str(Path(os.path.abspath(__file__)).parent.parent))
from llm import LLMHelper  # noqa: E402

output_lock = threading.Lock()
# === Path Configuration ===
default_function_summary_path = "../../results/function_summary.json"
standard_output1_path = "../prompt/func_fl_standard_1.txt"
standard_output2_path = "../prompt/func_fl_standard_2.txt"

llm_helper = LLMHelper()


def get_functions_by_case_id(case_id, file_path):
    funcs_list = []

    with open(file_path, 'r') as f:
        lines = f.readlines()
    current_case_id = None
    for line in lines:
        line = line.strip()
        # Check if it's a case_id line
        if line.startswith("func for case"):
            if current_case_id == case_id and funcs_list:
                break  # Found the target case_id, stop reading
            current_case_id = line.split(" ")[3]  # Extract case_id
            funcs_list = []  # Reset function list
        elif line and current_case_id == case_id:
            funcs_list.append(line)
    return funcs_list

def get_candidate_function_entries(case_id, file_path):
    entries = []
    for line in get_functions_by_case_id(case_id, file_path):
        if ":" not in line:
            continue
        file_name, signature = line.split(":", 1)
        function_name = signature.split("(", 1)[0].strip()
        entries.append({
            "file": file_name.strip(),
            "signature": signature.strip(),
            "function": function_name,
        })
    return entries

# === File Reading Function ===
def read_file(file_path, max_length=None):
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            content = file.read()
            return content[:max_length] if max_length else content
    except FileNotFoundError:
        print(f"Error: File '{file_path}' not found.")
        return ""

def replace_fl_placeholders(content,repro_content, mutate_content1, mutate_content2,mutate_content3,mutate_content4,report_content,filelist):
    """Replace placeholders"""
    content = content.replace("{TEST_PROGRAM_HERE}", repro_content or "N/A")
    content = content.replace("{MUTATE1_HERE}", mutate_content1 or "N/A")
    content = content.replace("{MUTATE2_HERE}", mutate_content2 or "N/A")
    content = content.replace("{MUTATE3_HERE}", mutate_content3 or "N/A")
    content = content.replace("{MUTATE4_HERE}", mutate_content4 or "N/A")
    content = content.replace("{FUNCTION_LIST_HERE}", filelist or "N/A")
    content = content.replace("{BUG_REPORT_HERE}", report_content or "N/A")
    return content

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

def parse_mutations(file_path):
    with open(file_path, 'r') as file:
        lines = file.readlines()

    # Dictionary to store structure: {case_id: [(original_line, mutated_line), ...]}
    mutations_map = defaultdict(list)
    current_case_id = None
    for i, line in enumerate(lines):
        # Check if it's the start line of a new case id
        if line.startswith("Response for"):
            current_case_id = line.split()[2]  # Get case ID
            continue
        # Check for the marker of an original mutated code line
        if "// Original:" in line and current_case_id:
            original_line = line.split("// Original:")[1].strip()
            mutated_line = lines[i + 1].strip()  # The next line is the mutated code
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

# === Model Calling Function ===
def call_model(messages, retries=3):
    result = ""
    for retry in range(retries):
        try:
            response, time_cost = llm_helper.query(messages, number_of_result=1)
            result=response.choices[0].message.content if response.choices else ""
            return result
        except (requests.exceptions.Timeout, OpenAIError) as e:
            print(f"Retry {retry + 1}/{retries} failed: {e}")
            time.sleep(2 ** retry)
    return None

def extract_json_payload(raw_text):
    if not raw_text:
        return None

    raw_text = raw_text.strip()
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        pass

    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start == -1 or end == -1 or start >= end:
        return None

    try:
        return json.loads(raw_text[start:end + 1])
    except json.JSONDecodeError:
        return None


def ensure_dict(value):
    return value if isinstance(value, dict) else {}


def extract_function_names_from_text(raw_text, candidate_entries):
    if not raw_text:
        return []

    candidate_names = []
    seen_candidates = set()
    for entry in candidate_entries:
        name = str(entry.get("function", "")).strip()
        if name and name not in seen_candidates:
            candidate_names.append(name)
            seen_candidates.add(name)

    matched = []
    for name in candidate_names:
        pattern = rf'(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])'
        if re.search(pattern, raw_text):
            matched.append(name)
        if len(matched) >= 10:
            break
    return matched


def build_fallback_payload(raw_text, candidate_entries):
    function_to_files = build_function_to_files(candidate_entries)
    function_names = extract_function_names_from_text(raw_text, candidate_entries)
    return {
        "thought_process": {},
        "ranked_results": [
            {
                "rank": idx + 1,
                "function": function_name,
                "file": function_to_files.get(function_name, [""])[0],
                "root_cause_hypothesis": "",
            }
            for idx, function_name in enumerate(function_names[:10])
        ],
        "standard_output": function_names[:10],
    }

def build_function_to_files(candidate_entries):
    function_to_files = defaultdict(list)
    for entry in candidate_entries:
        if entry["file"] not in function_to_files[entry["function"]]:
            function_to_files[entry["function"]].append(entry["file"])
    return function_to_files

def normalize_payload(payload, candidate_entries):
    payload = ensure_dict(payload)
    function_to_files = build_function_to_files(candidate_entries)
    ranked_results = payload.get("ranked_results", [])
    normalized_results = []
    seen = set()

    for item in ranked_results:
        if not isinstance(item, dict):
            continue

        function_name = str(item.get("function", "")).strip()
        if not function_name or function_name in seen:
            continue

        candidate_files = function_to_files.get(function_name, [])
        file_path = str(item.get("file", "")).strip()
        if not file_path and candidate_files:
            file_path = candidate_files[0]
        elif candidate_files and file_path not in candidate_files:
            file_path = candidate_files[0]

        normalized_results.append({
            "rank": len(normalized_results) + 1,
            "function": function_name,
            "file": file_path,
            "root_cause_hypothesis": str(item.get("root_cause_hypothesis", "")).strip(),
        })
        seen.add(function_name)
        if len(normalized_results) >= 10:
            break

    standard_output = payload.get("standard_output", [])
    if isinstance(standard_output, list):
        for function_name in standard_output:
            function_name = str(function_name).strip()
            if not function_name or function_name in seen:
                continue

            candidate_files = function_to_files.get(function_name, [])
            normalized_results.append({
                "rank": len(normalized_results) + 1,
                "function": function_name,
                "file": candidate_files[0] if candidate_files else "",
                "root_cause_hypothesis": "",
            })
            seen.add(function_name)
            if len(normalized_results) >= 10:
                break

    payload["ranked_results"] = normalized_results
    payload["standard_output"] = [item["function"] for item in normalized_results]
    return payload

def build_case_json_record(payload, timing=None):
    record = {
        "top_10": payload.get("ranked_results", []),
    }
    if timing:
        record["timing"] = timing
    return record


def find_case_file(folder, syzid, extensions):
    folder_path = Path(folder)
    for ext in extensions:
        candidate = folder_path / f"{syzid}{ext}"
        if candidate.exists():
            return str(candidate)
    return str(folder_path / f"{syzid}{extensions[0]}")


summary_lock = Lock()

# === Read standard output prompts ===
standard_output1 = read_file(standard_output1_path)
standard_output2 = read_file(standard_output2_path)

# === Function to process each syzid ===
def process_case(entry, report_folder, testcase_folder, base_prompt1, base_prompt2, mutate_result_dir,candidate_func):
    case_start = datetime.now()
    syzid = entry[0]
    num_mutants = sum(1 for m in entry[1:] if m != '0')
    print(f"Processing {syzid} with {num_mutants} mutants")

    mutate_num1=entry[1]
    mutate_num2=entry[2]
    mutate_num3=entry[3]
    mutate_num4=entry[4]
    mutate_map=parse_mutations(mutate_result_dir)
    report_path = find_case_file(report_folder, syzid, [".txt", ".log"])
    syz_path = find_case_file(testcase_folder, syzid, [".syz"])
    report_content = read_file(report_path)
    syz_content = process_syz_file(syz_path)
    funclist=get_functions_by_case_id(syzid,candidate_func)
    candidate_entries = get_candidate_function_entries(syzid, candidate_func)
    mutate_content1= extract_mutation(mutate_map,syzid, mutate_num1)
    mutate_content2= extract_mutation(mutate_map,syzid, mutate_num2)
    mutate_content3= extract_mutation(mutate_map,syzid, mutate_num3)
    mutate_content4= extract_mutation(mutate_map,syzid, mutate_num4)
    funclist='\n'.join(funclist)
    prompt1=replace_fl_placeholders(base_prompt1, syz_content,mutate_content1, mutate_content2, mutate_content3, mutate_content4, report_content, funclist)
    prompt2=replace_fl_placeholders(base_prompt2, syz_content,mutate_content1, mutate_content2, mutate_content3, mutate_content4, report_content, funclist)

    # === First round: Summarize function capabilities ===
    first_round_seconds = 0.0
    if syzid in func_summaries:
        print(f"Skipping first round for {syzid}, summary already exists.")
    else:
        first_round_start = datetime.now()
        messages = [
            {'role': 'assistant', 'content': standard_output1},
            {'role': 'user', 'content': prompt1}
        ]
        result = call_model( messages)
        if not result:
            return None
        try:
            with summary_lock:
                func_summaries[syzid] = result
                with open(function_summary_path, 'w', encoding='utf-8') as f:
                    json.dump(func_summaries, f, indent=2)
            first_round_seconds = (datetime.now() - first_round_start).total_seconds()
        except json.JSONDecodeError:
            print(f"First round JSON decode error for {syzid}")
            return None

    second_round_start = datetime.now()
    second_prompt_full = prompt2 + "\n\n=== Function Summary ===\n" + json.dumps(func_summaries[syzid], indent=2)
    messages = [
        {'role': 'assistant', 'content': standard_output2},
        {'role': 'user', 'content': second_prompt_full}
    ]
    result = call_model(messages)
    if not result:
        return None

    payload = extract_json_payload(result)
    if not isinstance(payload, dict):
        print(f"Second round JSON decode error for {syzid}, falling back to text extraction")
        payload = build_fallback_payload(result, candidate_entries)
    if "standard_output" not in payload and "ranked_results" not in payload:
        print(f"Second round: Missing both 'standard_output' and 'ranked_results' in JSON for {syzid}")
        payload = build_fallback_payload(result, candidate_entries)

    payload = normalize_payload(payload, candidate_entries)
    serialized = json.dumps(payload, ensure_ascii=False, indent=2)
    second_round_seconds = (datetime.now() - second_round_start).total_seconds()
    total_seconds = (datetime.now() - case_start).total_seconds()
    timing = {
        "processing_time_seconds": round(total_seconds, 3),
        "first_round_time_seconds": round(first_round_seconds, 3),
        "second_round_time_seconds": round(second_round_seconds, 3),
    }
    return syzid, serialized, build_case_json_record(payload, timing)

# === Main execution entry point ===
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="fl for method level")
    parser.add_argument('--case_list', required=True, help='Path to the case list')
    parser.add_argument('--report_dir', required=True, help='Directory containing reports')
    parser.add_argument('--testcase_dir', required=True, help='Directory containing test cases')
    #input
    parser.add_argument('--mutation_output', required=True, help='Path to parse mutation result')
    parser.add_argument('--func_list', required=True, help='Path to save filtered candidate functions')
    #output
    parser.add_argument('--output_dir', required=True, help='Path to save output')
    parser.add_argument('--json_output', required=False, help='Path to save structured per-case top-10 json output')
    parser.add_argument('--function_summary_path', required=False, default=default_function_summary_path, help='Path to save first-round function summaries')
    args = parser.parse_args()

    function_summary_path = args.function_summary_path
    try:
        with open(function_summary_path, 'r', encoding='utf-8') as f:
            func_summaries = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        func_summaries = {}

    # Clear output file before starting
    open(args.output_dir, 'w').close()
    json_output_path = args.json_output if args.json_output else args.output_dir + ".json"
    Path(function_summary_path).parent.mkdir(parents=True, exist_ok=True)

    file_list_dir = args.case_list
    with open(file_list_dir, 'r', encoding="utf-8") as f:
        entries = [line.strip().split('/') for line in f]

    print(f"Start to localize {len(entries)} cases...")

    base_prompt1=read_file("../prompt/func_fl_prompt1.txt")
    base_prompt2=read_file("../prompt/func_fl_prompt_2.txt")
    case_json_results = {}
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = {executor.submit(process_case, entry,args.report_dir,args.testcase_dir,base_prompt1,base_prompt2,args.mutation_output,args.func_list): entry[0] for entry in entries}
        for future in tqdm(as_completed(futures), total=len(entries), desc="Overall Progress"):
            syzid = futures[future]
            try:
                result = future.result()
                if result:
                    syzid_res, results, case_json = result
                    with output_lock:
                        with open(args.output_dir, "a", encoding="utf-8") as f:
                            f.write(f"response for {syzid_res}:\n{results}\n")
                        case_json_results[syzid_res] = case_json
            except Exception as e:
                print(f"{syzid} encountered error: {e}")

    with open(json_output_path, "w", encoding="utf-8") as f:
        json.dump(case_json_results, f, ensure_ascii=False, indent=2)

    print("\nFinished.")
    print(f"Outputs are stored in {args.output_dir}")
    print(f"Structured JSON is stored in {json_output_path}")
