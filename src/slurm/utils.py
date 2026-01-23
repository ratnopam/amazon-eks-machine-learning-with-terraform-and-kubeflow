"""Slurm utilities for generating and submitting jobs from YAML configs.

This module provides utilities to:
- Generate sbatch scripts from training YAML configs (same format as pytorchjob-distributed)
- Support multiple launcher frameworks: lightning, accelerate, torchrun, nemo, ray
- Submit jobs to Slurm via kubectl exec
- Wait for job completion
- Scale Slurm NodeSets

Supported Launchers:
- lightning: PyTorch Lightning (default) - auto-detects Slurm environment
- accelerate: HuggingFace Accelerate - uses accelerate.commands.launch
- torchrun: PyTorch distributed.run - native PyTorch launcher
- nemo: NVIDIA NeMo 2.0 - uses torchrun with NeMo-specific settings
- ray: Ray Train - starts Ray cluster, then submits job

Usage:
    from slurm.utils import generate_sbatch_from_yaml, submit_slurm_job, wait_for_slurm_job

    # Load yaml configs
    with open('fine-tune.yaml') as f:
        base_config = yaml.safe_load(f)
    with open('slurm.yaml') as f:
        slurm_config = yaml.safe_load(f)

    # Merge and generate sbatch
    config = {**base_config, **slurm_config}
    sbatch_content = generate_sbatch_from_yaml(config, 'my-training-job')

    # Submit and wait
    job_id = submit_slurm_job(sbatch_content, 'my-training-job')
    wait_for_slurm_job(job_id)
"""

import subprocess
import time
from typing import Optional, Dict, Any


def generate_sbatch_from_yaml(config: dict, job_name: str) -> str:
    """
    Generate sbatch script content from training YAML config.

    Reads the same YAML format used by pytorchjob-distributed helm chart
    and generates equivalent sbatch script. Supports multiple launchers.

    Args:
        config: Merged config dict (base yaml + slurm.yaml overlay)
        job_name: Name for the Slurm job

    Returns:
        sbatch script content as string
    """
    resources = config.get('resources', {})
    train = config.get('train', {})
    slurm = config.get('slurm', {})

    # Extract resource config
    nnodes = resources.get('nnodes', 1)
    nproc_per_node = resources.get('nproc_per_node', 8)
    gpu_request = resources.get('requests', {}).get('nvidia.com/gpu', 8)
    gpu_count = int(gpu_request) if gpu_request else 8
    cpus_per_task = resources.get('cpus_per_task', 96)

    # Slurm-specific options
    launcher = slurm.get('launcher', 'lightning')
    partition = slurm.get('partition', 'gpu')
    exclusive = slurm.get('exclusive', True)
    time_limit = slurm.get('time', '')

    # Launcher-specific options
    launcher_config = slurm.get('launcher_config', {})

    # Build environment variables section
    env_vars = _build_env_vars(train.get('env', []), job_name)

    # Build pre_script section
    pre_script = _build_pre_script(config.get('pre_script', []))

    # Build command and args (for non-launcher commands like python script.py)
    command_list = train.get('command', ['python'])
    args_list = train.get('args', [])

    # For lightning launcher: each srun task handles 1 GPU, so gpus_per_node arg should be 1
    # For other launchers: torchrun/accelerate spawn workers, so use full nproc_per_node
    effective_gpus_per_node = 1 if launcher == 'lightning' else nproc_per_node
    processed_args = _process_args(args_list, effective_gpus_per_node)

    command = ' '.join(command_list)
    args = ' \\\n    '.join(processed_args) if processed_args else ''

    # Time limit directive
    time_directive = f'#SBATCH --time={time_limit}' if time_limit else ''
    exclusive_directive = '#SBATCH --exclusive' if exclusive else ''

    # ntasks-per-node depends on launcher:
    # - lightning: srun spawns tasks, need ntasks = gpus
    # - torchrun/accelerate/nemo: torchrun spawns workers, need ntasks = 1
    ntasks_per_node = nproc_per_node if launcher == 'lightning' else 1

    # Adjust cpus-per-task based on ntasks (total CPUs / tasks)
    cpus_per_task_adjusted = cpus_per_task // ntasks_per_node if ntasks_per_node > 1 else cpus_per_task

    # Generate header
    header = f'''#!/bin/bash
#SBATCH --job-name={job_name}
#SBATCH --nodes={nnodes}
#SBATCH --ntasks-per-node={ntasks_per_node}
#SBATCH --cpus-per-task={cpus_per_task_adjusted}
#SBATCH --gpus-per-node={gpu_count}
#SBATCH --output=/efs/home/{job_name}/logs/%x_%j.out
#SBATCH --error=/efs/home/{job_name}/logs/%x_%j.err
{exclusive_directive}
{time_directive}

echo "START TIME: $(date)"

# Auto-fail on errors
set -eo pipefail

# ============================================
# Environment Setup (from train.env in yaml)
# ============================================
{chr(10).join(env_vars)}

# Distributed training variables
export MASTER_ADDR=$(scontrol show hostnames $SLURM_JOB_NODELIST | head -n 1)
export MASTER_PORT={slurm.get('master_port', 29500)}
export GPUS_PER_NODE={gpu_count}
export NNODES=$SLURM_NNODES
export NUM_PROCESSES=$(($NNODES * $GPUS_PER_NODE))

# PET_* compatibility variables (for scripts using PyTorchJob conventions)
export PET_MASTER_ADDR=$MASTER_ADDR
export PET_MASTER_PORT=$MASTER_PORT
export PET_NNODES=$SLURM_JOB_NUM_NODES
export PET_NPROC_PER_NODE={nproc_per_node}
export PET_NODE_RANK=$SLURM_NODEID

# Create directories
mkdir -p /efs/home/{job_name}/logs
mkdir -p /efs/home/{job_name}/output

# ============================================
# Pre-script (from pre_script in yaml)
# ============================================
{pre_script}
'''

    # Generate launcher-specific section
    launch_section = _generate_launcher_section(
        launcher=launcher,
        launcher_config=launcher_config,
        command=command,
        args=args,
        nproc_per_node=nproc_per_node,
        job_name=job_name
    )

    footer = '''
echo "END TIME: $(date)"
'''

    sbatch = header + launch_section + footer

    # Clean up empty SBATCH lines
    lines = sbatch.split('\n')
    lines = [l for l in lines if not (l.startswith('#SBATCH') and l.strip() == '#SBATCH')]
    return '\n'.join(lines)


def _build_env_vars(env_list: list, job_name: str) -> list:
    """Build environment variable export statements."""
    env_vars = []
    for env in env_list:
        name = env['name']
        value = env['value'].replace('{{ .Release.Name }}', job_name)
        env_vars.append(f'export {name}="{value}"')
    return env_vars


def _build_pre_script(pre_script_lines: list) -> str:
    """Build pre-script section, filtering PET_* exports."""
    filtered = []
    for line in pre_script_lines:
        if isinstance(line, str):
            # Skip export lines that reference PET_ vars (we set those ourselves)
            if line.strip().startswith('export ') and 'PET_' in line:
                continue
            filtered.append(line)
    return '\n'.join(filtered)


def _process_args(args_list: list, nproc_per_node: int) -> list:
    """Replace PET_* variables with Slurm equivalents."""
    processed = []
    for arg in args_list:
        if isinstance(arg, str):
            arg = arg.replace('$PET_NNODES', '${SLURM_JOB_NUM_NODES}')
            arg = arg.replace('$PET_NPROC_PER_NODE', str(nproc_per_node))
            arg = arg.replace('$PET_NODE_RANK', '${SLURM_NODEID}')
            arg = arg.replace('$PET_MASTER_ADDR', '${MASTER_ADDR}')
            arg = arg.replace('$PET_MASTER_PORT', '${MASTER_PORT}')
            processed.append(arg)
    return processed


def _generate_launcher_section(
    launcher: str,
    launcher_config: Dict[str, Any],
    command: str,
    args: str,
    nproc_per_node: int,
    job_name: str
) -> str:
    """Generate launcher-specific launch section."""

    if launcher == 'lightning':
        return _generate_lightning_launcher(command, args, nproc_per_node)
    elif launcher == 'accelerate':
        return _generate_accelerate_launcher(command, args, launcher_config, nproc_per_node)
    elif launcher == 'torchrun':
        return _generate_torchrun_launcher(command, args, launcher_config, nproc_per_node)
    elif launcher == 'nemo':
        return _generate_nemo_launcher(command, args, launcher_config, nproc_per_node)
    elif launcher == 'ray':
        return _generate_ray_launcher(command, args, launcher_config, job_name)
    else:
        raise ValueError(f"Unknown launcher: {launcher}. Supported: lightning, accelerate, torchrun, nemo, ray")


def _generate_lightning_launcher(command: str, args: str, nproc_per_node: int = 8) -> str:
    """
    PyTorch Lightning launcher.

    Lightning auto-detects Slurm environment via SLURMEnvironment.
    sbatch allocates ntasks-per-node=gpus, srun spawns one task per GPU for FSDP/DDP.
    Each task gets CUDA_VISIBLE_DEVICES set to its local rank (SLURM_LOCALID).
    """
    return f'''
# ============================================
# Launch Training (PyTorch Lightning)
# ============================================
# sbatch allocated {nproc_per_node} tasks per node (one per GPU)
# Each task sees only its assigned GPU via CUDA_VISIBLE_DEVICES
# Lightning detects Slurm and coordinates FSDP/DDP across tasks

srun --export=ALL bash -c 'export CUDA_VISIBLE_DEVICES=$SLURM_LOCALID && {command} {args}'
'''


def _generate_accelerate_launcher(
    command: str,
    args: str,
    config: Dict[str, Any],
    nproc_per_node: int
) -> str:
    """
    HuggingFace Accelerate launcher.

    Uses python -m accelerate.commands.launch with delayed variable interpolation.
    """
    config_file = config.get('config_file', '')
    config_arg = f'--config_file {config_file}' if config_file else ''

    # Extract just the python script and its args (remove 'python' prefix if present)
    program = args if not command.strip().endswith('python') else f'{command} {args}'
    if program.startswith('python '):
        program = program[7:]  # Remove 'python ' prefix

    return f'''
# ============================================
# Launch Training (HuggingFace Accelerate)
# ============================================
# Uses delayed variable interpolation for SLURM_PROCID

LAUNCHER="python -u -m accelerate.commands.launch \\
    --rdzv_conf rdzv_backend=c10d,rdzv_endpoint=$MASTER_ADDR:$MASTER_PORT \\
    {config_arg} \\
    --num_processes $NUM_PROCESSES \\
    --num_machines $NNODES \\
    --main_process_ip $MASTER_ADDR \\
    --main_process_port $MASTER_PORT \\
    --machine_rank \\$SLURM_PROCID \\
    --tee 3"

PROGRAM="{program}"

export CMD="$LAUNCHER $PROGRAM"
echo "Running: $CMD"

# srun with delayed interpolation
SRUN_ARGS="--wait=60 --kill-on-bad-exit=1"
srun $SRUN_ARGS bash -c "$CMD" 2>&1 | tee -a /efs/home/$SLURM_JOB_NAME/logs/main_log.txt
'''


def _generate_torchrun_launcher(
    command: str,
    args: str,
    config: Dict[str, Any],
    nproc_per_node: int
) -> str:
    """
    PyTorch torchrun (torch.distributed.run) launcher.

    """
    max_restarts = config.get('max_restarts', 0)

    # Extract just the python script and its args
    program = args if not command.strip().endswith('python') else f'{command} {args}'
    if program.startswith('python '):
        program = program[7:]

    return f'''
# ============================================
# Launch Training (torchrun)
# ============================================
# Uses delayed variable interpolation for SLURM_PROCID

LAUNCHER="python -u -m torch.distributed.run \\
    --nproc_per_node $GPUS_PER_NODE \\
    --nnodes $NNODES \\
    --node_rank \\$SLURM_PROCID \\
    --rdzv_endpoint $MASTER_ADDR:$MASTER_PORT \\
    --rdzv_backend c10d \\
    --max_restarts {max_restarts} \\
    --tee 3"

PROGRAM="{program}"

export CMD="$LAUNCHER $PROGRAM"
echo "Running: $CMD"

# srun with delayed interpolation
SRUN_ARGS="--wait=60 --kill-on-bad-exit=1"
srun $SRUN_ARGS bash -c "$CMD" 2>&1 | tee -a /efs/home/$SLURM_JOB_NAME/logs/main_log.txt
'''


def _generate_nemo_launcher(
    command: str,
    args: str,
    config: Dict[str, Any],
    nproc_per_node: int
) -> str:
    """
    NVIDIA NeMo 2.0 launcher.

    NeMo uses torchrun under the hood with specific environment settings.
    Reference: https://docs.nvidia.com/nemo-framework/user-guide/latest/nemo-2.0/quickstart.html
    """
    max_restarts = config.get('max_restarts', 0)

    # Extract script and args
    program = args if not command.strip().endswith('python') else f'{command} {args}'
    if program.startswith('python '):
        program = program[7:]

    return f'''
# ============================================
# Launch Training (NVIDIA NeMo 2.0)
# ============================================
# NeMo-specific environment settings
export NCCL_IB_SL=1
export CUDA_DEVICE_MAX_CONNECTIONS=1
export NCCL_ASYNC_ERROR_HANDLING=1

LAUNCHER="python -u -m torch.distributed.run \\
    --nproc_per_node $GPUS_PER_NODE \\
    --nnodes $NNODES \\
    --node_rank \\$SLURM_PROCID \\
    --rdzv_endpoint $MASTER_ADDR:$MASTER_PORT \\
    --rdzv_backend c10d \\
    --max_restarts {max_restarts} \\
    --tee 3"

PROGRAM="{program}"

export CMD="$LAUNCHER $PROGRAM"
echo "Running: $CMD"

# srun with delayed interpolation
SRUN_ARGS="--wait=60 --kill-on-bad-exit=1"
srun $SRUN_ARGS bash -c "$CMD" 2>&1 | tee -a /efs/home/$SLURM_JOB_NAME/logs/main_log.txt
'''


def _generate_ray_launcher(
    command: str,
    args: str,
    config: Dict[str, Any],
    job_name: str
) -> str:
    """
    Ray Train launcher.

    Ray uses a different model: starts Ray cluster first, then submits jobs.
    Reference: https://docs.ray.io/en/latest/cluster/vms/user-guides/community/slurm.html
    """
    return f'''
# ============================================
# Launch Training (Ray Train)
# ============================================
# Start Ray cluster on all nodes, run training on head node

# Get head node address
HEAD_NODE=$(scontrol show hostnames $SLURM_JOB_NODELIST | head -n 1)
HEAD_NODE_IP=$(srun --nodes=1 --ntasks=1 -w "$HEAD_NODE" hostname -I | awk '{{print $1}}')
RAY_PORT=6379

echo "Starting Ray cluster with head node: $HEAD_NODE_IP:$RAY_PORT"

# Start Ray head on first node
srun --nodes=1 --ntasks=1 -w "$HEAD_NODE" \\
    ray start --head --port=$RAY_PORT --num-cpus=$SLURM_CPUS_PER_TASK --num-gpus=$GPUS_PER_NODE --block &

sleep 10

# Start Ray workers on remaining nodes
if [ $SLURM_NNODES -gt 1 ]; then
    WORKER_NODES=$(scontrol show hostnames $SLURM_JOB_NODELIST | tail -n +2 | tr '\\n' ',')
    srun --nodes=$((SLURM_NNODES-1)) --ntasks=$((SLURM_NNODES-1)) -w "$WORKER_NODES" \\
        ray start --address="$HEAD_NODE_IP:$RAY_PORT" --num-cpus=$SLURM_CPUS_PER_TASK --num-gpus=$GPUS_PER_NODE --block &
fi

sleep 10

# Run training script on head node
echo "Submitting Ray job..."
srun --nodes=1 --ntasks=1 -w "$HEAD_NODE" {command} {args}

# Cleanup Ray cluster
ray stop
'''


def wait_for_slurm_job(job_id: str, namespace: str = 'slurm', timeout: int = 7200) -> bool:
    """
    Wait for Slurm job to complete.

    Args:
        job_id: Slurm job ID
        namespace: Kubernetes namespace where Slurm is deployed
        timeout: Maximum wait time in seconds (default 2 hours)

    Returns:
        True if job completed successfully, False otherwise

    Raises:
        TimeoutError: If job doesn't complete within timeout
    """
    start_time = time.time()
    login_pod = _get_login_pod(namespace)

    while time.time() - start_time < timeout:
        result = subprocess.run(
            ['kubectl', 'exec', '-n', namespace, login_pod, '--',
             'squeue', '-j', str(job_id), '-h', '-o', '%T'],
            capture_output=True, text=True
        )
        state = result.stdout.strip()

        if not state:
            # Job not in queue - check final state
            result = subprocess.run(
                ['kubectl', 'exec', '-n', namespace, login_pod, '--',
                 'sacct', '-j', str(job_id), '--format=State', '-n', '-P'],
                capture_output=True, text=True
            )
            final_state = result.stdout.strip().split('\n')[0] if result.stdout.strip() else 'UNKNOWN'
            print(f"Job {job_id} completed: {final_state}")
            return final_state == 'COMPLETED'

        print(f"Job {job_id}: {state}")
        time.sleep(30)

    raise TimeoutError(f"Job {job_id} timeout after {timeout}s")


def scale_slurm_nodeset(replicas: int, nodeset: str = 'slurm-worker-slinky', namespace: str = 'slurm') -> bool:
    """
    Scale Slurm NodeSet replicas.

    Args:
        replicas: Number of slurmd pod replicas
        nodeset: Name of the NodeSet (default 'slurm-worker-slinky')
        namespace: Kubernetes namespace

    Returns:
        True if scaling succeeded
    """
    cmd = ['kubectl', 'patch', 'nodeset', nodeset, '-n', namespace,
           '--type=merge', '-p', f'{{"spec":{{"replicas":{replicas}}}}}']
    result = subprocess.run(cmd, capture_output=True, text=True)
    print(f"Scaled NodeSet {nodeset} to {replicas} replicas")
    if result.returncode != 0:
        print(f"Error: {result.stderr}")
    return result.returncode == 0


def submit_slurm_job(sbatch_content: str, job_name: str, namespace: str = 'slurm') -> str:
    """
    Write sbatch script to shared storage and submit to Slurm.

    Args:
        sbatch_content: Content of the sbatch script
        job_name: Name for the job (used for directory structure)
        namespace: Kubernetes namespace where Slurm is deployed

    Returns:
        Slurm job ID
    """
    login_pod = _get_login_pod(namespace)

    # Create job directory
    subprocess.run(['kubectl', 'exec', '-n', namespace, login_pod, '--',
                    'mkdir', '-p', f'/efs/home/{job_name}/logs', f'/efs/home/{job_name}/scripts'],
                   capture_output=True)

    # Write sbatch content to file using heredoc
    sbatch_path = f'/efs/home/{job_name}/scripts/job.sbatch'

    # Escape any single quotes in sbatch_content
    escaped_content = sbatch_content.replace("'", "'\"'\"'")

    write_cmd = f"cat > {sbatch_path} << 'SBATCH_SCRIPT_EOF'\n{sbatch_content}\nSBATCH_SCRIPT_EOF"
    subprocess.run(
        ['kubectl', 'exec', '-n', namespace, login_pod, '--', 'bash', '-c', write_cmd],
        capture_output=True
    )

    # Make executable
    subprocess.run(
        ['kubectl', 'exec', '-n', namespace, login_pod, '--', 'chmod', '+x', sbatch_path],
        capture_output=True
    )

    # Submit job
    result = subprocess.run(
        ['kubectl', 'exec', '-n', namespace, login_pod, '--', 'sbatch', sbatch_path],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        print(f"Error submitting job: {result.stderr}")
        raise RuntimeError(f"Failed to submit job: {result.stderr}")

    job_id = result.stdout.strip().split()[-1]
    print(f"Submitted job {job_name}: {job_id}")
    return job_id


def _get_login_pod(namespace: str = 'slurm') -> str:
    """Get the Slurm login pod name."""
    result = subprocess.run(
        ['kubectl', 'get', 'pod', '-n', namespace, '-l', 'app.kubernetes.io/component=login',
         '-o', 'jsonpath={.items[0].metadata.name}'],
        capture_output=True, text=True
    )
    if not result.stdout.strip():
        raise RuntimeError(f"No Slurm login pod found in namespace {namespace}")
    return result.stdout.strip()


def get_slurm_job_status(job_id: str, namespace: str = 'slurm') -> Optional[str]:
    """
    Get current status of a Slurm job.

    Args:
        job_id: Slurm job ID
        namespace: Kubernetes namespace

    Returns:
        Job state string or None if job not found
    """
    login_pod = _get_login_pod(namespace)

    # First try squeue (for running/pending jobs)
    result = subprocess.run(
        ['kubectl', 'exec', '-n', namespace, login_pod, '--',
         'squeue', '-j', str(job_id), '-h', '-o', '%T'],
        capture_output=True, text=True
    )
    state = result.stdout.strip()

    if state:
        return state

    # Job not in queue - check sacct for completed jobs
    result = subprocess.run(
        ['kubectl', 'exec', '-n', namespace, login_pod, '--',
         'sacct', '-j', str(job_id), '--format=State', '-n', '-P'],
        capture_output=True, text=True
    )

    if result.stdout.strip():
        return result.stdout.strip().split('\n')[0]

    return None
