"""Slurm utilities for generating and submitting jobs from YAML configs.

This module provides utilities to:
- Generate sbatch scripts from training YAML configs (same format as pytorchjob-distributed)
- Submit jobs to Slurm via kubectl exec
- Wait for job completion
- Scale Slurm NodeSets

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
from typing import Optional


def generate_sbatch_from_yaml(config: dict, job_name: str) -> str:
    """
    Generate sbatch script content from training YAML config.

    Reads the same YAML format used by pytorchjob-distributed helm chart
    and generates equivalent sbatch script.

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

    # Slurm-specific options
    partition = slurm.get('partition', 'gpu')
    exclusive = slurm.get('exclusive', True)
    time_limit = slurm.get('time', '')

    # Build environment variables section
    env_vars = []
    for env in train.get('env', []):
        name = env['name']
        # Replace helm template variable with job_name
        value = env['value'].replace('{{ .Release.Name }}', job_name)
        env_vars.append(f'export {name}="{value}"')

    # Build pre_script section
    pre_script_lines = config.get('pre_script', [])
    # Filter out lines that use PET_* variables in export statements
    # (we'll set those ourselves)
    filtered_pre_script = []
    for line in pre_script_lines:
        if isinstance(line, str):
            # Skip export lines that reference PET_ vars
            if line.strip().startswith('export ') and 'PET_' in line:
                continue
            filtered_pre_script.append(line)
    pre_script = '\n'.join(filtered_pre_script)

    # Build command and args
    command_list = train.get('command', ['python'])
    args_list = train.get('args', [])

    # Replace PET_* variables with Slurm equivalents in args
    processed_args = []
    for arg in args_list:
        if isinstance(arg, str):
            arg = arg.replace('$PET_NNODES', '${SLURM_JOB_NUM_NODES}')
            arg = arg.replace('$PET_NPROC_PER_NODE', str(nproc_per_node))
            arg = arg.replace('$PET_NODE_RANK', '${SLURM_NODEID}')
            arg = arg.replace('$PET_MASTER_ADDR', '${MASTER_ADDR}')
            arg = arg.replace('$PET_MASTER_PORT', '${MASTER_PORT}')
            processed_args.append(arg)

    command = ' '.join(command_list)
    args = ' \\\n    '.join(processed_args) if processed_args else ''

    # Time limit directive
    time_directive = f'#SBATCH --time={time_limit}' if time_limit else ''
    exclusive_directive = '#SBATCH --exclusive' if exclusive else ''

    # Generate sbatch script
    sbatch = f'''#!/bin/bash
#SBATCH --job-name={job_name}
#SBATCH --nodes={nnodes}
#SBATCH --ntasks-per-node=1
#SBATCH --cpus-per-task=96
#SBATCH --gpus-per-node={gpu_count}
#SBATCH --output=/efs/home/{job_name}/logs/%x_%j.out
#SBATCH --error=/efs/home/{job_name}/logs/%x_%j.err
{exclusive_directive}
{time_directive}

# ============================================
# Environment Setup (from train.env in yaml)
# ============================================
{chr(10).join(env_vars)}

# Distributed training variables (Slurm equivalents of PET_* vars)
export MASTER_ADDR=$(scontrol show hostnames $SLURM_JOB_NODELIST | head -n 1)
export MASTER_PORT=29500
export PET_MASTER_ADDR=$MASTER_ADDR
export PET_MASTER_PORT=$MASTER_PORT
export PET_NNODES=$SLURM_JOB_NUM_NODES
export PET_NPROC_PER_NODE={nproc_per_node}
export PET_NODE_RANK=$SLURM_NODEID

# Create directories
mkdir -p $HOME/logs/$SLURM_NODEID
mkdir -p $HOME/output

# ============================================
# Pre-script (from pre_script in yaml)
# ============================================
{pre_script}

# ============================================
# Launch Training (from train.command/args)
# ============================================
srun --export=ALL {command} {args}
'''
    # Clean up empty SBATCH lines
    lines = sbatch.split('\n')
    lines = [l for l in lines if not (l.startswith('#SBATCH') and l.strip() == '#SBATCH')]
    return '\n'.join(lines)


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


def scale_slurm_nodeset(replicas: int, nodeset: str = 'gpu', namespace: str = 'slurm') -> bool:
    """
    Scale Slurm NodeSet replicas.

    Args:
        replicas: Number of slurmd pod replicas
        nodeset: Name of the NodeSet (default 'gpu')
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
