import os
import subprocess
import time
import logging
import re
import argparse
import random
import json
import sys
from tqdm import tqdm
from pathlib import Path

# The localization package root (one level up) provides the path constants; every
# absolute path used by this stage comes from the repository paths.py.
sys.path.append(str(Path(os.path.abspath(__file__)).parent.parent))
import settings  # noqa: E402

BOOT_TIMEOUT_SECONDS = 300
REPRO_TIMEOUT_SECONDS = 120
POLL_INTERVAL_SECONDS = 2
TARGET_SUCCESS_COUNT = 3
TARGET_FAILURE_COUNT = 1
def setup_logging(log_file):
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = logging.FileHandler(log_file)
    handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
    logger.addHandler(handler)

# --- Global Configurations ---
oracle_file_path = str(settings.ORACLE_FILE)
# path to syzkaller folder

syzkaller_path = os.environ.get("SYZKALLER_PATH") or str(settings.SYZKALLER_PATH) # path to syzkaller folder

# QEMU guest port/memory.  The repair half uses 10021 + worker_id for its own
# QEMU guests, so this stage must not silently collide with it: keep a separate
# port (default 10022, the historical value) or set it explicitly when the two
# stages are meant to share one worker.  Both are overridable from the command
# line as well.
qemu_ssh_port = int(os.environ.get("COHIKER_LOCALIZATION_QEMU_SSH_PORT", "10022"))
qemu_mem = os.environ.get("COHIKER_LOCALIZATION_QEMU_MEM", "4G")

oracle_data_cache = None
DATASETS_DIR = Path(settings.DATASETS_DIR)

def run_command(command, cwd=None, check=False):
    """Executes a shell command and logs its output."""
    result = subprocess.run(command, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stdout = result.stdout.decode(errors='ignore')
    stderr = result.stderr.decode(errors='ignore')
    if result.returncode != 0:
        logging.error(f"Command failed: {command}")
        if stdout:
            logging.error(stdout)
        if stderr:
            logging.error(stderr)
        if check:
            raise RuntimeError(f"Command failed with exit code {result.returncode}: {command}")
    else:
        logging.info(f"Command succeeded: {command}")
    return result

def ssh_base_command(image_path):
    return (
        f'ssh -i {image_path}/bullseye.id_rsa -p {qemu_ssh_port} '
        f'-o "StrictHostKeyChecking no" -o "UserKnownHostsFile=/dev/null" '
        f'-o "ConnectTimeout 5"'
    )

def scp_base_command(image_path):
    return (
        f'scp -i {image_path}/bullseye.id_rsa -P {qemu_ssh_port} '
        f'-o "StrictHostKeyChecking no" -o "UserKnownHostsFile=/dev/null" '
        f'-o "ConnectTimeout 5"'
    )

def execute_cmd_vm(cmd, image_path, log_path):
    """Executes a command inside the QEMU VM via SSH."""
    ssh_command = f'{ssh_base_command(image_path)} -t root@localhost "{cmd}" >> {log_path} 2>&1'
    res = run_command(ssh_command)
    logging.info(f"VM command executed: {cmd}")
    return res

def can_ssh(image_path):
    probe = f'{ssh_base_command(image_path)} root@localhost "echo ok"'
    return run_command(probe).returncode == 0

def run_make_commands(directory):
    """Runs make commands to build the kernel."""
    run_command('make olddefconfig', cwd=directory, check=True)
    logging.info(f"Start building kernel: {directory}")
    start_time = time.time()
    run_command(f'make -j{os.cpu_count()}', cwd=directory, check=True)
    end_time = time.time()
    logging.info(f"Kernel build completed: {directory}, took {end_time - start_time:.2f} seconds")


def resolve_cached_kernel_dir(cache_root, commit_hash):
    if not cache_root or not os.path.isdir(cache_root):
        return None

    exact_path = os.path.join(cache_root, commit_hash)
    if os.path.isdir(exact_path):
        return exact_path

    candidates = []
    for name in os.listdir(cache_root):
        path = os.path.join(cache_root, name)
        if not os.path.isdir(path):
            continue
        if commit_hash.startswith(name) or name.startswith(commit_hash):
            candidates.append(name)

    if not candidates:
        return None

    best_match = max(candidates, key=len)
    return os.path.join(cache_root, best_match)


def resolve_kernel_dir(work_dir, cache_dir):
    if cache_dir:
        logging.info(f"Using cached kernel from {cache_dir}")
        return cache_dir
    return work_dir

def switch_to_branch(branch, directory):
    """Switches to a specific git branch."""
    run_command(f'git checkout -f {branch}', cwd=directory, check=True)
    current_head = subprocess.run(
        'git rev-parse --short=12 HEAD',
        shell=True,
        cwd=directory,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if current_head.returncode != 0:
        raise RuntimeError(f"Unable to verify checked out HEAD for {branch} in {directory}")
    resolved_head = current_head.stdout.strip()
    if resolved_head != branch:
        raise RuntimeError(
            f"Checkout verification failed in {directory}: expected {branch}, got {resolved_head}"
        )
    logging.info(f"Switched to branch '{branch}' in {directory}")

def write_configs(file_path, configs):
    """Appends kernel configurations to the .config file."""
    with open(file_path, 'a') as f:
        for config in configs:
            f.write(config + "\n")
    logging.info(f"Configurations have been written to '{file_path}'.")

def start_qemu(runtime_dir, kernel_dir, image_path):
    """Starts the QEMU virtual machine."""
    vm_log_path = f"{runtime_dir}/vm.log"
    if os.path.exists(vm_log_path):
        os.remove(vm_log_path)
    
    kernel_path = f"{kernel_dir}/arch/x86/boot/bzImage"
    temp_image_path = f"/tmp/llmfskernel-qemu-{Path(runtime_dir).name}-{os.getpid()}.img"
    image_ref_file = f"{runtime_dir}/vm.image"
    if os.path.exists(temp_image_path):
        os.remove(temp_image_path)
    copy_result = subprocess.run(
        [
            "cp",
            "--reflink=auto",
            "--sparse=always",
            f"{image_path}/bullseye.img",
            temp_image_path,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if copy_result.returncode != 0:
        logging.error("Failed to prepare temporary QEMU image.")
        logging.error(copy_result.stderr.decode(errors="ignore"))
        return False
    with open(image_ref_file, "w") as f:
        f.write(temp_image_path)

    qemu_command = [
        "qemu-system-x86_64", "-m", qemu_mem,
        "-smp", "1",
        "-cpu", "host",
        "-kernel", kernel_path,
        "-append", "console=ttyS0 root=/dev/sda earlyprintk=serial net.ifnames=0 selinux=0",
        "-drive", f"file={temp_image_path},format=raw",
        "-net", f"user,hostfwd=tcp:127.0.0.1:{qemu_ssh_port}-:22",
        "-net", "nic,model=e1000", "-enable-kvm", "-nographic",
        "-pidfile", f"{runtime_dir}/vm.pid"
    ]
    
    with open(vm_log_path, 'w') as log_file:
        qemu_env = os.environ.copy()
        for tmp_var in ("TMPDIR", "TMP", "TEMP", "TEMPDIR"):
            qemu_env[tmp_var] = "/tmp"
        qemu_proc = subprocess.Popen(qemu_command, stdout=log_file, stderr=subprocess.STDOUT, cwd=kernel_dir, env=qemu_env)

    logging.info("Starting QEMU...")
    deadline = time.time() + BOOT_TIMEOUT_SECONDS
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL_SECONDS)
        if os.path.exists(vm_log_path):
            with open(vm_log_path, "r", errors="ignore") as log_file:
                log_content = log_file.read()
            if (
                "Kernel panic - not syncing" in log_content
                or "not syncing: Attempted to kill init!" in log_content
                or "VFS: Unable to mount root fs" in log_content
                or "failed to initialize kvm" in log_content
                or "Could not access KVM kernel module" in log_content
            ):
                logging.warning("Fatal kernel boot failure detected.")
                return False
            if "syzkaller login:" in log_content:
                logging.info("QEMU started successfully.")
                return True
        if qemu_proc.poll() is not None:
            logging.warning(f"QEMU exited before boot completed, return code: {qemu_proc.returncode}")
            return False
        if can_ssh(image_path):
            logging.info("QEMU started successfully.")
            return True
    logging.warning(f"QEMU boot timed out after {BOOT_TIMEOUT_SECONDS} seconds.")
    return False

def ensure_syzkaller_tools(image_path):
    execprog_path = f"{syzkaller_path}/bin/linux_amd64/syz-execprog"
    executor_path = f"{syzkaller_path}/bin/linux_amd64/syz-executor"
    run_command(f'{scp_base_command(image_path)} {execprog_path} root@localhost:/root')
    run_command(f'{scp_base_command(image_path)} {executor_path} root@localhost:/root')

def load_oracle_data():
    global oracle_data_cache
    if oracle_data_cache is not None:
        return oracle_data_cache

    oracle_data_cache = {}
    if not os.path.exists(oracle_file_path):
        logging.error(f"Oracle file not found: {oracle_file_path}")
        return oracle_data_cache

    with open(oracle_file_path, 'r') as file:
        for line in file:
            if ':' in line:
                fn, keyword = line.strip().split(':', 1)
                oracle_data_cache[fn.strip()] = keyword.strip()
    return oracle_data_cache

def log_contains_oracle_keyword(case_id, vm_log_path):
    oracle_data = load_oracle_data()
    keyword = oracle_data.get(case_id)
    if not keyword or not os.path.exists(vm_log_path):
        return False

    with open(vm_log_path, 'r', errors='ignore') as log_file:
        return keyword in log_file.read()


def read_log_text(vm_log_path):
    if not os.path.exists(vm_log_path):
        return ""
    with open(vm_log_path, 'r', errors='ignore') as log_file:
        return log_file.read()


def classify_reproduction(case_id, vm_log_path):
    oracle_data = load_oracle_data()
    keyword = oracle_data.get(case_id, "").strip()
    log_text = read_log_text(vm_log_path)

    if not log_text:
        return False, "empty vm log"

    has_keyword = bool(keyword) and keyword in log_text

    if has_keyword:
        return True, f"oracle match: {keyword}"
    if keyword:
        return False, f"oracle missing: {keyword}"
    return False, "no oracle keyword configured"


def persist_mutation_vm_log(case_id, mutation_id, vm_log_path, archive_dir):
    archive_dir.mkdir(parents=True, exist_ok=True)
    target_path = archive_dir / f"{case_id}_{mutation_id}.log"
    target_path.write_text(read_log_text(vm_log_path), encoding="utf-8")
    return target_path

def trans_and_excute(case_id, repro_exe_path, image_path, vm_log_path):
    """Transfers and executes the syzkaller reproducer in the VM."""
    if not os.path.exists(repro_exe_path):
        logging.error(f"Syz file does not exist: {repro_exe_path}")
        return False

    # Transfer reproducer
    scp_command = f"{scp_base_command(image_path)} {repro_exe_path} root@localhost:{settings.GUEST_REPRO_FILE}"
    if run_command(scp_command).returncode != 0:
        logging.error("SCP file transfer failed.")
        return False
    logging.info(f"Reproduction file transferred to VM: {repro_exe_path}")

    ensure_syzkaller_tools(image_path)

    # Execute reproducer
    run_cmd = "./syz-execprog -repeat 10 repro.txt"
    runssh_command = f'{ssh_base_command(image_path)} -t root@localhost "{run_cmd}" >> {vm_log_path} 2>&1'
    proc = subprocess.Popen(runssh_command, shell=True, text=True)
    deadline = time.time() + REPRO_TIMEOUT_SECONDS
    while time.time() < deadline:
        if log_contains_oracle_keyword(case_id, vm_log_path):
            break
        if proc.poll() is not None:
            break
        time.sleep(POLL_INTERVAL_SECONDS)

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    logging.info("Reproduction execution finished.")

    # Cleanup
    if can_ssh(image_path):
        execute_cmd_vm("rm repro.txt", image_path, vm_log_path)
    return True

def check_crash_in_log(folder_name, vm_log_path):
    """Checks for a crash signature in the VM log based on an oracle file."""
    if not os.path.exists(vm_log_path):
        logging.error(f"VM log file not found: {vm_log_path}")
        return False

    oracle_data = load_oracle_data()
    if not oracle_data:
        logging.error("Failed to load oracle data.")
        return False
        
    if folder_name not in oracle_data:
        logging.error(f"Folder name not in oracle data: {folder_name}")
        return False
        
    keyword = oracle_data[folder_name]
    with open(vm_log_path, 'r', errors='ignore') as log_file:
        log_content = log_file.read()
        if keyword in log_content:
            logging.info(f"Found matching keyword in log: {keyword}")
            # Clean the keyword from log to avoid matching again
            updated_content = log_content.replace(keyword, "")
            with open(vm_log_path, "w") as file:
                file.write(updated_content)
            return True
            
    return False

def kill_qemu_process(folder_name):
    """Kills the QEMU process using its PID file."""
    pid_file = f"{folder_name}/vm.pid"
    if os.path.exists(pid_file):
        with open(pid_file, 'r') as f:
            pid = f.read().strip()
            if pid:
                run_command(f"kill -9 {pid}")
                logging.info(f"QEMU process terminated (PID: {pid}): {folder_name}")
            os.remove(pid_file)
            logging.info(f"PID file deleted: {pid_file}")
    else:
        # This is not necessarily an error, QEMU might not have started.
        logging.info(f"QEMU PID file not found (might not have been running): {folder_name}")

    image_ref_file = f"{folder_name}/vm.image"
    if os.path.exists(image_ref_file):
        with open(image_ref_file, "r") as f:
            temp_image = f.read().strip()
        if temp_image and os.path.exists(temp_image):
            os.remove(temp_image)
            logging.info(f"Temporary QEMU image deleted: {temp_image}")
        os.remove(image_ref_file)

def parse_syzid_commit_file(file_path):
    """Parses a file mapping syz IDs to commit hashes."""
    result = {}
    with open(file_path, 'r') as f:
        for line in f:
            if ':' in line:
                syzid, commit = [p.strip() for p in line.split(':', 1)]
                result[syzid] = commit
    return result

def process_syz_file(input_file):
    """Processes a .syz file to ensure each line is commented out correctly."""
    with open(input_file, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    processed_lines = [line.rstrip() + "#" if not line.startswith("#") else line.rstrip() for line in lines]
    
    while processed_lines and processed_lines[-1] == "#":
        processed_lines.pop()
    
    with open(input_file, 'w', encoding='utf-8') as f:
        f.write("\n".join(processed_lines) + "\n")

def list_mutation_files(mutation_case_dir, syzid):
    """Returns available mutation files as (mutation_id, path), sorted by mutation_id."""
    case_dir = Path(mutation_case_dir) / syzid
    if not case_dir.exists():
        return []

    mutation_files = []
    pattern = re.compile(rf"^{re.escape(syzid)}_(\d+)\.syz$")
    for syz_path in case_dir.glob(f"{syzid}_*.syz"):
        match = pattern.match(syz_path.name)
        if not match:
            logging.warning(f"Unexpected mutation file name, skipping: {syz_path}")
            continue
        mutation_files.append((int(match.group(1)), str(syz_path)))
    return sorted(mutation_files)

def main():
    global syzkaller_path, oracle_data_cache, oracle_file_path, qemu_ssh_port, qemu_mem
    parser = argparse.ArgumentParser(description="Automated kernel test case reproduction.")
    parser.add_argument('--case_list', required=True, help='Path to the case list file.')
    parser.add_argument('--mutation_case_dir', required=True, help='Path to the directory with mutation test cases.')
    parser.add_argument('--image_path', required=True, help='Path to the QEMU image directory.')
    parser.add_argument('--output_file', required=True, help='Path to the output file for results.')
    parser.add_argument('--linux_dir', required=True, help='Path to the Linux kernel source directory.')
    parser.add_argument('--bug_config_dir', required=True, help='Path to the bug-specific kernel config directory.')
    parser.add_argument('--log_file', required=False, default='repro.log', help='Path to the reproduction log file.')
    parser.add_argument('--cache_linux_dir', required=False, default=str(settings.CACHE_ROOT), help='Directory containing cached built kernels keyed by commit.')
    parser.add_argument('--syzkaller_path', required=False, help='Path to the syzkaller directory containing bin/linux_amd64 tools.')
    parser.add_argument('--analysis_output', required=False, help='Path to save structured reproduction analysis json.')
    parser.add_argument('--commit_map', required=False, default=str(DATASETS_DIR / "case_commit.txt"), help='Path to case-to-commit mapping file.')
    parser.add_argument('--oracle_content', required=False, default=oracle_file_path, help='Path to crash keyword oracle file.')
    parser.add_argument('--ssh_port', required=False, type=int, default=None,
                        help='Host port forwarded to the guest ssh (default 10022, env '
                             'COHIKER_LOCALIZATION_QEMU_SSH_PORT). The repair half uses '
                             '10021 + worker_id, so pick a different port when both run at once.')
    parser.add_argument('--qemu_mem', required=False, default=None,
                        help='Guest RAM (default 4G, env COHIKER_LOCALIZATION_QEMU_MEM).')
    args = parser.parse_args()
    oracle_file_path = args.oracle_content
    oracle_data_cache = None
    if args.ssh_port is not None:
        qemu_ssh_port = args.ssh_port
    if args.qemu_mem:
        qemu_mem = args.qemu_mem
    commit_map = parse_syzid_commit_file(args.commit_map)
    setup_logging(args.log_file)
    if args.syzkaller_path:
        syzkaller_path = args.syzkaller_path

    work_dir = args.linux_dir
    image_path = args.image_path
    bug_config_dir = args.bug_config_dir
    config_path = f"{work_dir}/.config"
    testcase_path = args.mutation_case_dir
    vm_log_path = f"{work_dir}/vm.log"
    mutation_log_dir = Path(args.output_file).resolve().parent / "mutation_vm_logs"

    with open(args.case_list, "r") as f:
        entries = [line.strip().split('/') for line in f if line.strip()]

    print(f"Start to verify {len(entries)} cases...")
    logging.info(f"Start to verify {len(entries)} cases...")
    analysis_records = {}
    for entry in tqdm(entries, desc="Overall Progress"):
        syzid = entry[0]
        logging.info(f"Processing {syzid} with 10 mutants")
        
        kill_qemu_process(work_dir) # Clean up previous run
        
        branch = commit_map.get(syzid)
        if not branch:
            logging.info(f"{syzid} is not an upstream case, skipping.")
            continue
            
        switch_to_branch(branch, work_dir)

        cached_kernel_dir = resolve_cached_kernel_dir(args.cache_linux_dir, branch)
        if not cached_kernel_dir:
            logging.info(f"No cached kernel found for {branch}, falling back to local build.")
        kernel_dir = resolve_kernel_dir(work_dir, cached_kernel_dir)
        
        syz_config_path = os.path.join(bug_config_dir, f"{syzid}.txt")
        if not os.path.exists(syz_config_path):
            logging.warning(f"Bug config not found for {syzid}, skipping.")
            continue

        if not cached_kernel_dir:
            with open(syz_config_path, "r") as file:
                bug_config = file.read()
                # Disable debugging and coverage options
                for cfg in ['CONFIG_DEBUG_INFO', 'CONFIG_DEBUG_INFO_DWARF4', 'CONFIG_KASAN', 'CONFIG_KCOV']:
                    bug_config = re.sub(f'{cfg}=y', f'# {cfg} is not set', bug_config)

            with open(config_path, "w") as target:
                target.write(bug_config)

            run_make_commands(work_dir)
        
        logging.info("Repeating test mutations...")
        avoided, triggered = [], []
        mutation_results = []
        mutation_files = list_mutation_files(testcase_path, syzid)
        if not mutation_files:
            logging.warning(f"No generated mutation files found for {syzid}, skipping reproduction.")
            analysis_records[syzid] = {
                "case_id": syzid,
                "commit": branch,
                "used_cached_kernel": bool(cached_kernel_dir),
                "successful_mutation_ids": [],
                "failed_mutation_ids": [],
                "selected_success_mutation_ids": [],
                "selected_failed_mutation_id": None,
                "success_count": 0,
                "failure_count": 0,
                "skipped": True,
                "skip_reason": "no generated mutation files",
                "mutation_results": [],
            }
            kill_qemu_process(work_dir)
            continue

        for i, syz_path in mutation_files:
            if len(avoided) >= TARGET_SUCCESS_COUNT and len(triggered) >= TARGET_FAILURE_COUNT:
                logging.info(
                    f"Collected enough samples for {syzid}: "
                    f"{len(avoided)} bug-avoiding and {len(triggered)} bug-triggering mutations."
                )
                break
            logging.info(f"Running mutation #{i} for {syzid}")
            boot_ok = start_qemu(work_dir, kernel_dir, image_path)
            if not boot_ok:
                logging.warning(f"VM failed to boot cleanly for mutation {i}, skipping.")
                log_archive = persist_mutation_vm_log(syzid, i, vm_log_path, mutation_log_dir)
                mutation_results.append({
                    "mutation_id": i,
                    "result": "invalid",
                    "reason": "vm failed to boot cleanly",
                    "vm_log": str(log_archive),
                })
                kill_qemu_process(work_dir)
                continue

            process_syz_file(syz_path)
            if not trans_and_excute(syzid, syz_path, image_path, vm_log_path):
                log_archive = persist_mutation_vm_log(syzid, i, vm_log_path, mutation_log_dir)
                mutation_results.append({
                    "mutation_id": i,
                    "result": "invalid",
                    "reason": "reproducer execution failed",
                    "vm_log": str(log_archive),
                })
                kill_qemu_process(work_dir)
                continue

            reproduced, reason = classify_reproduction(syzid, vm_log_path)
            log_archive = persist_mutation_vm_log(syzid, i, vm_log_path, mutation_log_dir)
            logging.info(f"Mutation #{i} classification for {syzid}: {reason}")
            if reproduced:
                triggered.append(i)
            else:
                avoided.append(i)
            mutation_results.append({
                "mutation_id": i,
                "result": "triggered" if reproduced else "avoided",
                "reason": reason,
                "vm_log": str(log_archive),
            })
                
            kill_qemu_process(work_dir)
        
        kill_qemu_process(work_dir) # Final cleanup
        
        # Record results
        succ_sample = (random.sample(avoided, min(len(avoided), 3)) + [0, 0, 0])[:3]
        fail_sample = random.choice(triggered) if triggered else 0
        with open(args.output_file, "a") as f:
            f.write(f"{syzid}/")
            f.write("/".join(map(str, succ_sample + [fail_sample])))
            f.write("\n")
        analysis_records[syzid] = {
            "case_id": syzid,
            "commit": branch,
            "used_cached_kernel": bool(cached_kernel_dir),
            "successful_mutation_ids": avoided,
            "failed_mutation_ids": triggered,
            "selected_success_mutation_ids": [idx for idx in succ_sample if idx != 0],
            "selected_failed_mutation_id": fail_sample if fail_sample != 0 else None,
            "success_count": len(avoided),
            "failure_count": len(triggered),
            "mutation_results": mutation_results,
        }

    print("\nFinished.")
    print(f"Outputs are stored in {args.output_file}")
    logging.info(f"Outputs are stored in {args.output_file}")
    if args.analysis_output:
        analysis_path = Path(args.analysis_output)
        analysis_path.parent.mkdir(parents=True, exist_ok=True)
        analysis_path.write_text(json.dumps(analysis_records, ensure_ascii=False, indent=2), encoding="utf-8")
if __name__ == "__main__":
    main()
