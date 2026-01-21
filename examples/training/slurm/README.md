# Slurm Training Examples

Distributed training examples using Slurm job scheduler on EKS with Slinky.

## Overview

These examples demonstrate the same training workflows as the Kubeflow Training Operator examples, but using Slurm (`sbatch`) for job submission instead of Helm/PyTorchJob.

**Key Features:**
- Reuses existing YAML configurations (`fine-tune.yaml`, `evaluate.yaml`, etc.)
- Adds `slurm.yaml` overlay for Slurm-specific settings
- Dynamically generates sbatch scripts from YAML
- Karpenter provisions GPU nodes on demand
- Same output locations (EFS/FSx)

## Prerequisites

1. EKS cluster deployed with `slurm_enabled = true`
2. slurmd container image built and pushed to ECR
3. Karpenter configured with GPU NodePool

## Available Examples

| Example | Framework | Description |
|---------|-----------|-------------|
| [pytorch-lightning/qwen3-14b-sft](./pytorch-lightning/qwen3-14b-sft/) | PyTorch Lightning | Fine-tune Qwen3-14B with FSDP |

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
