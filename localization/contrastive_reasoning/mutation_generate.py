import base64
import os
import json
import time
import threading
import argparse
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI, OpenAIError
import requests
from collections import defaultdict
import tempfile
from typing import Any, Dict, Optional

import sys
from pathlib import Path

from tqdm import tqdm

# The localization package root (one level up) provides the shared LLM client;
# every absolute path comes from the repository paths.py.
sys.path.append(str(Path(os.path.abspath(__file__)).parent.parent))
from llm import LLMHelper  # noqa: E402
from settings import DATASETS_DIR  # noqa: E402  (single source of truth)

MAX_RETRIES = 3
MAX_INPUT_LENGTH = 61000
MAX_THREADS = 4
MAX_REQUESTS_PER_MINUTE = 40
REQUEST_INTERVAL = 60 / MAX_REQUESTS_PER_MINUTE

output_lock = threading.Lock()
image_lock = threading.Lock()
rate_limit_lock = threading.Lock()
last_request_time = [0.0]
image_cache_path = "image_path.json"

llm_helper = LLMHelper()


# client = OpenAI(
#     api_key=os.getenv("OPENAI_API_KEY"),
#     base_url=os.getenv("OPENAI_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
# )

def wait_for_rate_limit():
    with rate_limit_lock:
        now = time.time()
        elapsed = now - last_request_time[0]
        if elapsed < REQUEST_INTERVAL:
            time.sleep(REQUEST_INTERVAL - elapsed)
        last_request_time[0] = time.time()


def read_file(file_path, max_length=None) -> str:
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            content = file.read()
            return content[:max_length] if max_length else content
    except FileNotFoundError:
        print(f"Error: File '{file_path}' not found.")
        return ""


def find_case_file(folder, syzid, extensions):
    folder_path = Path(folder)
    for ext in extensions:
        candidate = folder_path / f"{syzid}{ext}"
        if candidate.exists():
            return str(candidate)
    return str(folder_path / f"{syzid}{extensions[0]}")


def extract_syscalls(syz_content: str):
    syscalls = []
    pattern = re.compile(r"\b([a-zA-Z0-9_$]+)\(")
    for line in syz_content.splitlines():
        match = pattern.search(line)
        if match:
            syscall_name = match.group(1)
            if syscall_name not in syscalls:
                syscalls.append(syscall_name)
    return syscalls


def replace_placeholders(content, bug_report, code_line, sys_msg):
    content = content.replace("{BUG_REPORT_HERE}", bug_report or "N/A")
    content = content.replace("{SYZ_HERE}", code_line or "N/A")
    content = content.replace("{SYSCALL_HERE}", sys_msg or "N/A")
    return content


def replace_image_strings(syzid: str, text: str):
    # for syz_content has huge image, replace the IMAGE before using LLM.
    placeholder_path = Path(DATASETS_DIR) / "testcases_placeholder" / f"{syzid}.syz"
    if not placeholder_path.exists():
        return text
    with open(placeholder_path, "r") as f:
        image_map = json.load(f)
        for k, v in image_map["placeholders"].items():
            decoded_bytes = base64.b64decode(v)
            vv = decoded_bytes.decode()
            if vv in text:
                text = text.replace(vv, k)
    return text
    # with image_lock:
    #     if os.path.exists(image_cache_path):
    #         with open(image_cache_path, "r") as f:
    #             image_map = json.load(f)
    #     else:
    #         image_map = {}
    # counter = 0
    # existing_vars = [int(k.replace("$IMAGE_", "")) for k in image_map if k.startswith("$IMAGE_")]
    # if existing_vars:
    #     counter = max(existing_vars) + 1
    # pattern = re.compile(r'(= ?\(?.*?)("([^"]{%d,})")' % threshold)
    #
    # def replacer(match):
    #     nonlocal counter
    #     prefix = match.group(1)
    #     full_string = match.group(2)
    #     raw_string = match.group(3)
    #
    #     var_name = f'$IMAGE_{counter}'
    #     image_map[var_name] = full_string
    #     counter += 1
    #
    #     return f'{prefix}{var_name}'
    #
    # new_text = pattern.sub(replacer, text)
    # with image_lock:
    #     with open(image_cache_path, "w") as f:
    #         json.dump(image_map, f, indent=2)
    # print("done")
    return text


def replace_string_images(syzid, content: str) -> str:
    placeholder_path = Path(DATASETS_DIR) / "testcases_placeholder" / f"{syzid}.syz"
    if not placeholder_path.exists():
        return content
    with open(placeholder_path, "r") as f:
        image_map = json.load(f)
        for k, v in image_map["placeholders"].items():
            if k in content:
                decoded_bytes = base64.b64decode(v)
                vv = decoded_bytes.decode()
                content = content.replace(k, vv)


    # with open(image_cache_path, "r") as f:
    #     image_map = json.load(f)
    #
    # pattern = re.compile(r'\$IMAGE_\d+')
    #
    # def replacer(match):
    #     var = match.group(0)
    #     return image_map.get(var, var)
    #
    # restored_content = pattern.sub(replacer, content)

    return content


def extract_mutations_from_content(content):
    mutation_pattern = re.compile(
        r'\{\s*"origin"\s*:\s*"(.*?)"\s*,\s*"mutated"\s*:\s*"(.*?)"\s*\}', re.DOTALL
    )
    mutations = mutation_pattern.findall(content)
    output = ""
    for idx, (origin_line, mutated_line) in enumerate(mutations, start=1):
        output += f"// Mutation {idx}\n"
        output += f"// Original: {origin_line.strip()}\n"
        output += f"{mutated_line.strip()}\n\n"
    return output


def mutations_from_payload(payload: Optional[Dict[str, Any]]) -> str:
    """Build the text block consumed by ``parse_mutations`` from the parsed JSON.

    Some models return the mutations only inside the JSON payload (``standard_output``)
    and the regex in :func:`extract_mutations_from_content` needs one exact key order,
    so it can come back empty even though the payload is fine.  Emit the same
    ``// Original: ...`` format here as a fallback.
    """
    payload = ensure_dict(payload)
    items = payload.get("standard_output") or []
    blocks = []
    for idx, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            continue
        origin = " ".join(normalize_text(item.get("origin")).split())
        mutated = " ".join(normalize_text(item.get("mutated")).split())
        if not origin or not mutated:
            continue
        blocks.append(f"// Mutation {idx}\n// Original: {origin}\n{mutated}\n")
    return "\n".join(blocks)


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
        return "\n".join(normalize_text(item) for item in value if normalize_text(item))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value).strip()


def ensure_dict(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def build_analysis_record(payload: Optional[Dict[str, Any]], syzid: str) -> Dict[str, Any]:
    payload = ensure_dict(payload)
    thought_process = ensure_dict(payload.get("thought_process"))
    mutations = payload.get("standard_output", []) if payload else []
    step_1 = ensure_dict(thought_process.get("step_1"))
    step_2 = ensure_dict(thought_process.get("step_2"))
    step_3 = ensure_dict(thought_process.get("step_3"))

    mutation_hypotheses = []
    for key in sorted(step_3.keys()):
        if key.startswith("mutate"):
            details = normalize_text(step_3.get(key))
            if details:
                mutation_hypotheses.append({
                    "mutation_slot": key,
                    "hypothesis": details,
                })

    normalized_mutations = []
    if isinstance(mutations, list):
        for idx, item in enumerate(mutations, start=1):
            if not isinstance(item, dict):
                continue
            normalized_mutations.append({
                "mutation_id": idx,
                "origin": normalize_text(item.get("origin")),
                "mutated": normalize_text(item.get("mutated")),
            })

    return {
        "case_id": syzid,
        "test_intent_guess": normalize_text(step_2.get("details")),
        "pre_mutation_root_cause_hypothesis": normalize_text(step_1.get("details")),
        "mutation_hypotheses": mutation_hypotheses,
        "mutations": normalized_mutations,
        "raw_thought_process": thought_process,
    }


def parse_mutations(file_path):
    lines = Path(file_path).read_text().split("\n")
    # with open(file_path, 'r') as file:
    #     lines = file.readlines()
    case_id = []
    mutations_map = defaultdict(list)
    current_case_id = None

    for i, line in enumerate(lines):
        if line.startswith("Response for"):
            current_case_id = line.split()[2]
            if current_case_id.endswith(":"):
                current_case_id = current_case_id[:-1]
            # print(current_case_id)
            case_id.append(current_case_id)
            continue

        if "// Original:" in line and current_case_id:
            original_line = line.split("// Original:")[1].strip()
            mutated_line = lines[i + 1].strip()
            mutations_map[current_case_id].append((original_line, mutated_line))

    return dict(mutations_map), case_id


def apply_individual_mutations(mutations_map, case_paths, output_dir):
    def candidate_replacements(original_line, mutated_line):
        candidates = [(original_line, mutated_line)]
        unescaped_original = original_line.replace("\\\\", "\\")
        unescaped_mutated = mutated_line.replace("\\\\", "\\")
        if (unescaped_original, unescaped_mutated) not in candidates:
            candidates.append((unescaped_original, unescaped_mutated))
        return candidates

    for case_id, mutation_list in mutations_map.items():
        file_path = case_paths.get(case_id)
        # print(file_path)
        if not file_path:
            print(f"No path found for case ID: {case_id}")
            continue

        # Read file content
        try:
            with open(file_path, 'r') as file:
                original_lines = file.readlines()
        except FileNotFoundError:
            print(f"File not found: {file_path}")
            continue

        for idx, (original_line, mutated_line) in enumerate(mutation_list, start=1):
            modified_lines = original_lines[:]
            flag = False
            for original_candidate, mutated_candidate in candidate_replacements(original_line, mutated_line):
                for i, line in enumerate(modified_lines):
                    if original_candidate in line:
                        modified_lines[i] = line.replace(original_candidate, mutated_candidate)
                        # print("succ",case_id,idx)
                        flag = True
                        break
                if flag:
                    break
            # Create output directory
            case_output_dir = Path(output_dir) / case_id
            case_output_dir.mkdir(parents=True, exist_ok=True)
            # os.makedirs(case_output_dir, exist_ok=True, parents=True)

            # Save the modified file
            output_file_path = case_output_dir / f"{case_id}_{idx}.syz"
            output_file_path.write_text("\n".join(modified_lines))
            # with open(output_file_path, 'w') as output_file:
            #     output_file.writelines(modified_lines)


def extract_and_write_by_id(file_path, syzid):
    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    extracting = False
    syscall_description = []
    target_header = f"response for {syzid}"

    for line in lines:
        if line.strip() == target_header:
            if extracting:
                # Second appearance means the extraction is complete
                break
            else:
                extracting = True
                continue
        elif extracting and line.strip().startswith("response for "):
            # The next response has started, end early
            break
        elif extracting:
            syscall_description.append(line)

    return "".join(syscall_description)

def cache_all_images(cache_path, testcase_folder, force_update=False, threshold=200):
    if not force_update and cache_path and os.path.exists(cache_path):
        print(f"Using existing image cache at {cache_path}")
        return
    image_map = {}
    for syz_path in Path(testcase_folder).glob("*.syz"):
        syz_content = read_file(syz_path)

        counter = 0
        pattern = re.compile(r'(= ?\(?.*?)("([^"]{%d,})")' % threshold)
        def replacer(match):
            nonlocal counter
            full_string = match.group(2)
            var_name = f'$IMAGE_{counter}'
            image_map[var_name] = full_string
            counter += 1
        pattern.sub(replacer, syz_content)

    json.dump(image_map, Path(cache_path).open("w"), indent=4)

def process_entry(entry, report_folder, testcase_folder, base_prompt, standard_output, output_file, output_file_4case,
                  analysis_output):
    syzid = entry[0]
    report_path = find_case_file(report_folder, syzid, [".txt", ".log"])
    syz_path = find_case_file(testcase_folder, syzid, [".syz"])
    report_content = read_file(report_path)
    syz_content = read_file(syz_path)
    # print(syz_content)
    # print("=================")
    syz_content = replace_image_strings(syzid, syz_content)
    # print(syzid)
    # print(syz_content)
    # print("=============================")
    # exit(-1)
    # print(syz_content)
    syscall_list = extract_syscalls(syz_content)

    syscall_description = extract_and_write_by_id(str(Path(DATASETS_DIR) / "syscall_description.txt"), syzid)
    # Here, to avoid introducing new dependencies, we have prepared the syscall descriptions in advance.
    # If you wish to generate them yourself, please refer to the link: https://github.com/google/syzkaller/tree/master/sys/linux .
    prompt = replace_placeholders(base_prompt, report_content, syz_content, syscall_description)
    # print(prompt)
    retries = 0
    while retries < MAX_RETRIES:
        try:
            wait_for_rate_limit()
            p = [
                {'role': 'user', 'content': prompt},
                {'role': 'assistant', 'content': standard_output}
            ]
            response, time_cost = llm_helper.query(p, number_of_result=1)
            # response = client.chat.completions.create(
            #     model="deepseek-v3",
            #     temperature=0,
            #     messages=[
            #         {'role': 'user', 'content': prompt},
            #         {'role': 'assistant', 'content': standard_output}
            #     ],
            # )
            result = response.choices[0].message.content if response.choices else ""
            payload = extract_json_payload(result)
            analysis_record = build_analysis_record(payload, syzid)
            mutation_result_extract = extract_mutations_from_content(result)
            if not mutation_result_extract.strip():
                # model returned the mutations only as JSON -> synthesise the text form
                mutation_result_extract = mutations_from_payload(payload)
            mutation_result = replace_string_images(syzid, mutation_result_extract)
            with output_lock:
                with open(output_file, "a", encoding="utf-8") as output:
                    output.write(f"Response for {syzid}:\n{mutation_result_extract}\n")
                with open(output_file_4case, "a", encoding="utf-8") as output:
                    output.write(f"Response for {syzid}:\n{mutation_result}\n")
                if analysis_output:
                    analysis_path = Path(analysis_output)
                    existing = {}
                    if analysis_path.exists():
                        try:
                            existing = json.loads(analysis_path.read_text(encoding="utf-8"))
                        except json.JSONDecodeError:
                            existing = {}
                    existing[syzid] = analysis_record
                    analysis_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
            # print(f"\n====== Finish case {syzid} ======\n")
            return
        except (requests.exceptions.Timeout, OpenAIError) as e:
            retries += 1
            print(f"Warning: API request for {syzid} failed (attempt {retries}/{MAX_RETRIES}), error: {e}")
            time.sleep(2 ** retries)
        except Exception as e:
            print(f"Error: An unknown error occurred while processing {syzid}: {e}")
            return


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--case_list', required=True, help='Path to case list')
    parser.add_argument('--report_dir', required=True, help='Directory containing reports')
    parser.add_argument('--testcase_dir', required=True, help='Directory containing test cases')
    parser.add_argument('--output_file', required=True, help='Path to save outputs')
    parser.add_argument('--mutation_case_dir', required=True, help='Path to save mutation testcase')
    parser.add_argument('--mutation_detail_file', required=False, help='Path to save detailed mutation text used to build testcase variants')
    parser.add_argument('--analysis_output', required=False, help='Path to save structured mutation analysis json')
    args = parser.parse_args()
    # with tempfile.NamedTemporaryFile(mode='w+', delete=False) as tmp_file:
    #     tmp_file.write("Hello in text mode!")
    #     tmp_file.seek(0)
    #     print("File content:", tmp_file.read())
    # output_file_4case = "/mp.txt"  # Change to something like /tmp/xxx
    output_file_4case = Path(args.mutation_detail_file) if args.mutation_detail_file else Path("output/mutation_details.txt")
    output_file_4case.parent.mkdir(parents=True, exist_ok=True)
    with open(output_file_4case, 'w'):
        # Do nothing; this creates an empty file
        pass
    if args.analysis_output:
        analysis_path = Path(args.analysis_output)
        analysis_path.parent.mkdir(parents=True, exist_ok=True)
        analysis_path.write_text("{}", encoding="utf-8")
    with open(args.case_list, 'r', encoding="utf-8") as f:
        entries = [line.strip().split('/') for line in f]

    print(f"Start to generate mutants for the {len(entries)} cases...")

    # print(entries)
    base_prompt = read_file("../prompt/mutation_prompt.txt", MAX_INPUT_LENGTH)
    standard_output = read_file("../prompt/mutation_standard_output.txt", MAX_INPUT_LENGTH)

    with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
        list(tqdm(
            executor.map(
                lambda entry: process_entry(entry, args.report_dir, args.testcase_dir, base_prompt, standard_output,
                                            args.output_file, output_file_4case, args.analysis_output),
                entries
            ),
            total=len(entries),
            desc="Use LLMs to generate mutants"
        ))

    # with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
    #     futures = [
    #         executor.submit(process_entry, entry, args.report_dir, args.testcase_dir, base_prompt, standard_output,
    #                         args.output_file, output_file_4case)
    #         for entry in entries
    #     ]
    # with tqdm(total=len(futures), desc="Use LLMs to generate mutants") as pbar:
    #     for future in as_completed(futures):
    #         try:
    #             future.result()
    #         except Exception as e:
    #             print(f"Error during processing: {e}")
    #         pbar.update(1)
    case_paths = {}
    mutations, caseid = parse_mutations(output_file_4case)
    for entry in entries:
        syzid = entry[0]
        case_path = find_case_file(args.testcase_dir, syzid, [".syz"])
        case_paths[syzid] = case_path
    apply_individual_mutations(mutations, case_paths, args.mutation_case_dir)
    print(f"All mutants have been saved to {args.mutation_case_dir}")

if __name__ == "__main__":
    main()
