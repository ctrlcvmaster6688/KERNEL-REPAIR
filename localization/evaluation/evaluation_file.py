import argparse
import json
import re
from pathlib import Path
from typing import Dict, List, Tuple

DEFAULT_ORACLE_FILE = Path(__file__).resolve().parents[2] / "data" / "datasets" / "oracle_file_list.json"


def process_output_to_responses(input_file: str) -> Dict[str, List[str]]:
    responses = {}
    content = open(input_file, "r", encoding="utf-8").read()
    pattern = re.compile(
        r"response for (\w+).*?\"standard_output\":\s*\[\s*(.*?)\s*\]",
        re.DOTALL | re.IGNORECASE,
    )
    matches = pattern.findall(content)
    for case_id, file_list in matches:
        responses[case_id] = []
        files = re.findall(r"\"(.*?)\"", file_list)
        for file in files:
            responses[case_id].append(file)
    return responses


def read_caseid_list(filelist_path: str) -> List[str]:
    with open(filelist_path, "r", encoding="utf-8") as f:
        return [line.strip().split("/")[0] for line in f if line.strip()]


def calculate_metrics(
    responses: Dict[str, List[str]],
    oracle: Dict[str, List[str]],
    caseid_list: List[str],
    top_n: List[int],
) -> Tuple[Dict[int, float], float, float, float, Dict[str, dict]]:
    correct_counts = {n: 0 for n in top_n}
    reciprocal_rank_sum = 0.0
    all_oracle_ranks = []
    first_match_ranks = []

    valid_caseids = sorted(set(caseid_list) & set(oracle.keys()) & set(responses.keys()))
    total_cases = len(valid_caseids)
    case_details = {}

    for case_id in valid_caseids:
        predicted_files = responses[case_id]
        oracle_files = set(oracle[case_id])
        max_rank = 11
        matched_ranks = [i + 1 for i, file in enumerate(predicted_files) if file in oracle_files]
        missed_count = len(oracle_files) - len(matched_ranks)

        current_mar_ranks = matched_ranks + [max_rank] * missed_count
        all_oracle_ranks.extend(current_mar_ranks)

        first_match_rank = min(matched_ranks) if matched_ranks else max_rank
        if matched_ranks:
            reciprocal_rank_sum += 1.0 / first_match_rank
        first_match_ranks.append(first_match_rank)

        hits = {}
        for n in top_n:
            hit = bool(matched_ranks and first_match_rank <= n)
            hits[n] = hit
            if hit:
                correct_counts[n] += 1

        case_details[case_id] = {
            "first_match_rank": first_match_rank,
            "matched_ranks": matched_ranks,
            "oracle_count": len(oracle_files),
            "top_hits": hits,
        }

    accuracies = {n: correct_counts[n] / total_cases if total_cases > 0 else 0.0 for n in top_n}
    mrr = reciprocal_rank_sum / total_cases if total_cases > 0 else 0.0
    mar = sum(all_oracle_ranks) / len(all_oracle_ranks) if all_oracle_ranks else 0.0
    mfr = sum(first_match_ranks) / total_cases if total_cases > 0 else 0.0
    return accuracies, mrr, mar, mfr, case_details


def main():
    parser = argparse.ArgumentParser(description="Evaluate file-level fault localization metrics.")
    parser.add_argument("--case_list", required=True, help="Path to the case list.")
    parser.add_argument("--result_file", required=True, help="Path to the model result output file.")
    parser.add_argument(
        "--oracle_file",
        default=str(DEFAULT_ORACLE_FILE),
        help="Path to the file-level oracle JSON.",
    )
    args = parser.parse_args()

    responses = process_output_to_responses(args.result_file)
    with open(args.oracle_file, "r", encoding="utf-8") as f:
        oracle = json.load(f)
    caseid_list = read_caseid_list(args.case_list)

    top_n = [1, 3, 5, 10]
    accuracy, mrr, mar, mfr, case_details = calculate_metrics(responses, oracle, caseid_list, top_n)

    missing_in_responses = set(caseid_list) - set(responses.keys())
    missing_in_oracle = set(caseid_list) - set(oracle.keys())
    if missing_in_responses:
        print(f"警告：{len(missing_in_responses)} 个 caseid 在结果文件中不存在")
    if missing_in_oracle:
        print(f"警告：{len(missing_in_oracle)} 个 caseid 在 oracle 文件中不存在")

    for n in top_n:
        print(f"Top-{n}: {accuracy[n]:.2%}")
    print(f"MRR: {mrr:.4f}")
    print(f"MAR: {mar:.4f}")
    print(f"MFR: {mfr:.4f}")

    for case_id in sorted(case_details):
        detail = case_details[case_id]
        print(
            "CASE_EVAL "
            f"{case_id} stage=file "
            f"ground_truth_rank={detail['first_match_rank']}"
        )


if __name__ == "__main__":
    main()
