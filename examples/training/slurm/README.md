# Slurm Training Examples

Distributed training examples using Slurm job scheduler on EKS with Slinky.

## Overview

These examples demonstrate the same training workflows as the Kubeflow Training Operator examples, but using Slurm (`sbatch`) for job submission instead of Helm/PyTorchJob.

**Key Features:**
- Reuses existing YAML configurations (`fine-tune.yaml`, `evaluate.yaml`, etc.)
- Adds `slurm.yaml` overlay for Slurm-specific settings
- Dynamically generates sbatch scripts from YAML
- Supports multiple launcher frameworks (see below)
- Karpenter provisions GPU nodes on demand
- Same output locations (EFS/FSx)

## Supported Launchers

The Slurm integration supports multiple distributed training launchers:

| Launcher | Framework | Description |
|----------|-----------|-------------|
| `lightning` | PyTorch Lightning | Default. Auto-detects Slurm, handles distributed internally |
| `accelerate` | HuggingFace Accelerate | Uses `accelerate.commands.launch` with rendezvous backend |
| `torchrun` | PyTorch | Native `torch.distributed.run` launcher |
| `nemo` | NVIDIA NeMo 2.0 | torchrun with NeMo-specific environment settings |
| `ray` | Ray Train | Starts Ray cluster, then submits job |

Configure the launcher in `slurm.yaml`:

```yaml
slurm:
  launcher: lightning  # or accelerate, torchrun, nemo, ray
  launcher_config:
    # Framework-specific options
    config_file: accelerate.yaml  # For accelerate
    max_restarts: 0               # For torchrun/nemo
```

## Prerequisites

1. EKS cluster deployed with `slurm_enabled = true`
2. slurmd container image built and pushed to ECR
3. Karpenter configured with GPU NodePool

## Available Examples

| Example | Framework | Launcher | Description |
|---------|-----------|----------|-------------|
| [pytorch-lightning/qwen3-14b-sft](./pytorch-lightning/qwen3-14b-sft/) | PyTorch Lightning | lightning | Fine-tune Qwen3-14B with FSDP |

## How It Works

1. **Base Config**: Training parameters defined in existing YAML files (e.g., `fine-tune.yaml`)
2. **Slurm Overlay**: `slurm.yaml` adds Slurm-specific settings (job name, partition, etc.)
3. **Dynamic Generation**: Notebook reads YAML and generates sbatch script
4. **Submit**: sbatch script submitted to Slurm via login pod
5. **Karpenter Scaling**: GPU nodes provisioned automatically for slurmd pods

```
┌──────────────────┐     ┌──────────────┐     ┌─────────────────┐
│  fine-tune.yaml  │ + │  slurm.yaml  │ → │ sbatch script   │
│  (base config)   │     │  (overlay)   │     │ (generated)     │
└──────────────────┘     └──────────────┘     └────────┬────────┘
                                                       │
                                                       ▼
                                              ┌─────────────────┐
                                              │  Slurm Login    │
                                              │  (sbatch)       │
                                              └────────┬────────┘
                                                       │
                                                       ▼
                                              ┌─────────────────┐
                                              │  Slurm NodeSet  │
                                              │  (slurmd pods)  │
                                              └────────┬────────┘
                                                       │
                                                       ▼
                                              ┌─────────────────┐
                                              │  Karpenter      │
                                              │  (GPU nodes)    │
                                              └─────────────────┘
```

## Quick Start

1. Build slurmd container (one-time):
   ```bash
   cd eks-cluster/docker/slurmd
   ./build.sh
   ```

2. Enable Slurm in terraform.tfvars:
   ```hcl
   slurm_enabled = true
   slurm_login_enabled = true
   slurmd_image_repository = "<account>.dkr.ecr.<region>.amazonaws.com/slurmd-pytorch"
   slurmd_image_tag = "25.05.0-cu126-py312"
   ```

3. Apply terraform:
   ```bash
   terraform apply
   ```

4. Run a training notebook (e.g., `pytorch-lightning/qwen3-14b-sft/finetune.ipynb`)

## Launcher Selection Guide

### When to use each launcher:

| Use Case | Recommended Launcher |
|----------|---------------------|
| Scripts using PyTorch Lightning `Trainer` | `lightning` |
| Scripts using HuggingFace `Trainer` or `transformers` | `accelerate` |
| Custom PyTorch distributed scripts | `torchrun` |
| NeMo 2.0 recipes and models | `nemo` |
| Ray Train or hybrid Ray+PyTorch | `ray` |

### Scripts are Scheduler-Agnostic

Training scripts don't need Slurm-specific modifications. The launcher layer translates:

1. **Environment variables**: `PET_*` (PyTorchJob) → `SLURM_*` (Slurm)
2. **Rank assignment**: Handled by the launcher (Lightning, torchrun, etc.)
3. **Rendezvous**: `MASTER_ADDR` extracted from `scontrol show hostnames`

### Launcher Technical Details

| Launcher | Launch Command | Process Model |
|----------|---------------|---------------|
| `lightning` | `srun python script.py` | Lightning spawns GPU workers |
| `accelerate` | `srun bash -c "accelerate launch ..."` | Accelerate spawns GPU workers |
| `torchrun` | `srun bash -c "torchrun ..."` | torchrun spawns GPU workers |
| `nemo` | `srun bash -c "torchrun ..."` + NeMo env | torchrun spawns GPU workers |
| `ray` | Ray cluster + `python script.py` | Ray manages workers |

Reference: [stas00/ml-engineering Slurm launchers](https://github.com/stas00/ml-engineering/tree/master/orchestration/slurm/launchers)
