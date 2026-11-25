import subprocess
from datetime import datetime
import re
import json
import os
def get_commit_time(commit_hash,work_dir):
    result = subprocess.run(
        ['git', 'log', '-1', '--format=%ci', commit_hash],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=work_dir
    )
    return result.stdout.strip()
def extract_first_commit_hash(stdout):
    match = re.search(r"^commit\s+([0-9a-f]{40})", stdout, re.MULTILINE)
    if match:
        return match.group(1)
    return None
def get_function_first_commit_time(func_name, file_path, before_commit_hash,work_dir):
    result = subprocess.run(
        ['git', 'log', '-S', func_name, '--', file_path, '--before', before_commit_hash, '--reverse', '--date=iso'],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        errors='replace', 
        env={**os.environ, 'LANG': 'C.UTF-8'} ,
        cwd=work_dir
    )

    commit=extract_first_commit_hash(result.stdout)
    return commit

def calculate_days_between(time1_str, time2_str):
    format_str = "%Y-%m-%d %H:%M:%S %z"
    time1 = datetime.strptime(time1_str, format_str)
    time2 = datetime.strptime(time2_str, format_str)
    
    delta = abs(time1 - time2)
    return delta.days

def calculate_function_time_interval(file_path, func_name, commit_hash,work_dir):

    version_commit_time = get_commit_time(commit_hash,work_dir)
    
    first_commit = get_function_first_commit_time(func_name, file_path, version_commit_time,work_dir)
    if not first_commit:
        print(f"function {func_name} before {commit_hash} has no commit")
        return
    first_commit_time=get_commit_time(first_commit,work_dir)
    #print(first_commit_time)

    time_diff = calculate_days_between(first_commit_time, version_commit_time)
    return time_diff
def parse_oracle_file(oracle_file_path):
    """
    Parse the oracle file into a dictionary with case IDs as keys 
    and oracle function names as values.
    """
    oracle = {}
    with open(oracle_file_path, 'r') as f:
        lines = f.readlines()
    
    for line in lines:
        line = line.strip()
        if line:
            # Split the line into case ID and oracle function names
            case_id, oracle_functions = line.split(":", 1)
            oracle[case_id] = oracle_functions.split(",")  # Split function names by comma
    return oracle
def parse_json_to_map(json_file_path):
    with open(json_file_path, 'r') as f:
        data = json.load(f)

    branch_to_files_map = {branch: files for branch, files in data.items()}
    
    return branch_to_files_map



