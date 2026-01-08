# EKS Training Architecture Guide

This document explains how training jobs work end-to-end, focusing on Kubernetes orchestration from Helm submission to GPU execution.

## Project Structure Overview

```
amazon-eks-machine-learning-with-terraform-and-kubeflow/
├── eks-cluster/
│   └── terraform/              # Creates EKS + Training Operators
├── charts/
│   └── machine-learning/
│       └── training/           # Training Helm charts
│           ├── pytorchjob-distributed/   # PyTorchJob (Master/Worker)
│           ├── pytorchjob-elastic/       # PyTorchJob (Elastic)
│           ├── mpijob-horovod-*/         # MPIJob (Horovod)
│           └── raytrain/                 # RayJob
└── examples/
    └── training/               # User-facing examples
        ├── accelerate/         # HuggingFace Accelerate
        ├── pytorch-lightning/  # PyTorch Lightning
        ├── nemo2/              # NVIDIA NeMo
        └── raytrain/           # Ray Train
```

## Training Operators on EKS

Terraform installs these operators in your cluster:

| Operator | CRD | Use Case |
|----------|-----|----------|
| **Training Operator** | PyTorchJob, TFJob | PyTorch/TensorFlow distributed training |
| **MPI Operator** | MPIJob | Horovod, MPI-based training |
| **KubeRay Operator** | RayJob | Ray Train distributed training |

```bash
# Verify operators are running
kubectl get pods -n kubeflow | grep -E "training-operator|mpi-operator|kuberay"
```

---

## End-to-End Training Flow

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         TRAINING JOB FLOW                                    │
└─────────────────────────────────────────────────────────────────────────────┘

┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│   User       │    │   Helm       │    │  Kubernetes  │    │  Training    │
│   (helm      │───>│   Chart      │───>│  API Server  │───>│  Operator    │
│   install)   │    │   Templates  │    │              │    │              │
└──────────────┘    └──────────────┘    └──────────────┘    └──────┬───────┘
                                                                    │
                    ┌───────────────────────────────────────────────┘
                    │
                    v
┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│  Scheduler   │───>│   Kubelet    │───>│  Container   │───>│  Training    │
│  (place on   │    │  (start      │    │  (GPU +      │    │  Process     │
│  GPU nodes)  │    │   pods)      │    │   NCCL)      │    │  (FSDP/DDP)  │
└──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
```

---

## Step-by-Step Walkthrough

### Step 1: User Submits Job via Helm

```bash
helm install accel-qwen3-sft \
  charts/machine-learning/training/pytorchjob-distributed \
  -f examples/training/accelerate/qwen3-14b-sft/fine-tune.yaml \
  -n kubeflow-user-example-com
```

### Step 2: Helm Renders Templates

The chart creates **3 Kubernetes resources**:

```
helm install creates:
│
├── ConfigMap: train-script-<release>
│   └── train-script.sh (setup + training command)
│
├── ConfigMap: framework-scripts-<release>
│   └── fine_tune.py, dataset_module.py, etc.
│
└── PyTorchJob: pytorchjob-<release>
    ├── Master: 1 replica
    └── Worker: (nnodes - 1) replicas
```

### Step 3: Training Operator Creates Pods

The Training Operator watches for PyTorchJob resources and creates pods:

```
PyTorchJob spec (nnodes: 2)
│
└── Training Operator creates:
    ├── Pod: pytorchjob-<release>-master-0
    │   ├── Environment: PET_NODE_RANK=0, PET_MASTER_ADDR=<self>
    │   └── Runs: /etc/config/train-script.sh
    │
    └── Pod: pytorchjob-<release>-worker-0
        ├── Environment: PET_NODE_RANK=1, PET_MASTER_ADDR=<master-ip>
        └── Runs: /etc/config/train-script.sh
```

### Step 4: Kubernetes Scheduler Places Pods

Scheduler matches pods to nodes based on:

```yaml
# From values.yaml
resources:
  requests:
    "nvidia.com/gpu": 8           # Need 8 GPUs
    "vpc.amazonaws.com/efa": 4    # Need EFA for fast networking
  node_type: 'p4d.24xlarge'       # Instance type selector

tolerations:
  - key: "nvidia.com/gpu"         # Allow scheduling on GPU nodes
    operator: "Exists"
    effect: "NoSchedule"
```

### Step 5: Container Executes Training

Inside each pod, `train-script.sh` runs:

```bash
# Phase 1: Generate config (inline_script)
cat > /tmp/accel_config.yaml <<EOF
distributed_type: FSDP
num_machines: $PET_NNODES           # 2
machine_rank: $PET_NODE_RANK        # 0 or 1
main_process_ip: $PET_MASTER_ADDR   # Master pod IP
main_process_port: $PET_MASTER_PORT # 29500
EOF

# Phase 2: Install dependencies (pre_script)
pip3 install transformers accelerate peft

# Phase 3: Launch training (train.command + train.args)
accelerate launch --config_file /tmp/accel_config.yaml \
  /etc/framework-scripts/fine_tune.py \
  --model_path=/fsx/pretrained-models/Qwen/Qwen3-14B \
  --max_steps=10000
```

### Step 6: Distributed Coordination

```
Master Pod (rank 0)                    Worker Pod (rank 1)
┌─────────────────────┐                ┌─────────────────────┐
│ 8 GPU processes     │                │ 8 GPU processes     │
│ (rank 0-7)          │◄──── NCCL ────►│ (rank 8-15)         │
│                     │    over EFA    │                     │
│ - Forward pass      │                │ - Forward pass      │
│ - Backward pass     │                │ - Backward pass     │
│ - All-reduce grads  │◄─────────────►│ - All-reduce grads  │
│ - Update weights    │                │ - Update weights    │
└─────────────────────┘                └─────────────────────┘
         │                                      │
         └──────────── Synchronized ────────────┘
```

---

## Training Operators Explained

### PyTorchJob (Training Operator)

**Used by**: Accelerate, PyTorch Lightning, NeMo

```yaml
apiVersion: kubeflow.org/v1
kind: PyTorchJob
spec:
  nprocPerNode: "8"              # Processes per pod
  pytorchReplicaSpecs:
    Master:
      replicas: 1                # Always 1 master
      template: ...
    Worker:
      replicas: N                # Additional workers
      template: ...
```

**How it works**:
1. Creates Master pod first
2. Creates Worker pods
3. Injects environment variables:
   - `PET_NNODES`: Total nodes
   - `PET_NODE_RANK`: This node's rank
   - `PET_MASTER_ADDR`: Master's IP
   - `PET_MASTER_PORT`: Rendezvous port
4. Training processes use these to coordinate via NCCL

### MPIJob (MPI Operator)

**Used by**: Horovod, TensorFlow with MPI

```yaml
apiVersion: kubeflow.org/v1
kind: MPIJob
spec:
  mpiReplicaSpecs:
    Launcher:
      replicas: 1                # Runs mpirun
    Worker:
      replicas: N                # MPI workers
```

**How it works**:
1. Creates Worker pods with SSH daemons
2. Creates Launcher pod
3. Launcher runs: `mpirun -np 16 -host worker0,worker1 script.py`
4. OpenMPI coordinates process placement
5. Horovod uses MPI for gradient synchronization

### RayJob (KubeRay Operator)

**Used by**: Ray Train

```yaml
apiVersion: ray.io/v1
kind: RayJob
spec:
  entrypoint: "python train.py"
  rayClusterSpec:
    headGroupSpec: ...           # Ray head (scheduler)
    workerGroupSpecs:
      - replicas: N              # Ray workers (GPUs)
```

**How it works**:
1. Creates Ray head pod (scheduler, object store)
2. Creates Ray worker pods
3. Workers join Ray cluster via gRPC
4. Ray Train distributes training tasks to workers
5. NCCL used for GPU synchronization

---

## Values.yaml Configuration

### Key Sections Explained

```yaml
# 1. Container image
image: 'nvcr.io/nvidia/pytorch:25.10-py3'

# 2. Framework selection (determines which scripts get mounted)
framework: 'accelerate'    # Options: accelerate, pytorch_lightning, etc.

# 3. GPU and node configuration
resources:
  requests:
    "nvidia.com/gpu": 8              # GPUs per pod
    "vpc.amazonaws.com/efa": 4       # EFA devices for inter-node
  limits:
    "nvidia.com/gpu": 8
    "vpc.amazonaws.com/efa": 4
  nnodes: 2                          # Total nodes (1 master + N-1 workers)
  nproc_per_node: 8                  # Processes per node (usually = GPUs)
  node_type: 'p4d.24xlarge'          # EC2 instance type

# 4. Storage mounts
pvc:
  - name: pv-fsx                     # FSx for models
    mount_path: /fsx
  - name: pv-efs                     # EFS for checkpoints
    mount_path: /efs

ebs:
  storage: 200Gi                     # Ephemeral storage
  mount_path: /tmp

# 5. Setup scripts (run before training)
inline_script:                       # Generate config files
  - |
    cat > /tmp/config.yaml <<EOF
    ...
    EOF

pre_script:                          # Install dependencies
  - pip3 install transformers accelerate

# 6. Training command
train:
  command:
    - accelerate
  args:
    - launch
    - --config_file
    - /tmp/config.yaml
    - /etc/framework-scripts/fine_tune.py
    - --model_path=/fsx/pretrained-models/Qwen/Qwen3-14B
```

---

## Chart → Operator Mapping

| Chart | Creates CRD | Operator |
|-------|-------------|----------|
| `pytorchjob-distributed` | PyTorchJob | Training Operator |
| `pytorchjob-elastic` | PyTorchJob (elastic) | Training Operator |
| `mpijob-horovod-*` | MPIJob | MPI Operator |
| `raytrain` | RayJob | KubeRay Operator |

---

## Framework Scripts

Each chart includes framework-specific scripts in `charts/.../scripts/`:

```
pytorchjob-distributed/scripts/
├── accelerate/
│   ├── fine_tune.py                 # Training loop
│   ├── test_checkpoint.py           # Evaluation
│   ├── convert_checkpoint_to_hf.py  # Checkpoint conversion
│   └── dataset_module.py            # Data loading
└── pytorch_lightning/
    └── ...
```

These are:
1. Packaged into a ConfigMap by Helm
2. Mounted at `/etc/framework-scripts/` in pods
3. Referenced by the training command

---

## Storage Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    STORAGE LAYOUT                            │
└─────────────────────────────────────────────────────────────┘

FSx for Lustre (/fsx) - High Performance
├── pretrained-models/           # Base models (downloaded once)
│   └── Qwen/Qwen3-14B/
└── Syncs with S3 bucket

EFS (/efs) - Shared Workspace
├── home/<release-name>/
│   ├── checkpoints/             # Training checkpoints
│   ├── logs/                    # TensorBoard logs
│   └── output/                  # Final model
└── Accessible from all pods

EBS (/tmp) - Ephemeral
└── Per-pod temporary storage
```

---

## GPU Communication

### Intra-Node (Within a single p4d.24xlarge)

```
GPU 0 ◄──── NVLink ────► GPU 1
  │                        │
  └──── NVLink ────────────┘
           ...
8 GPUs connected via NVLink (600 GB/s)
```

### Inter-Node (Between nodes)

```
Node 0                           Node 1
┌─────────────┐                 ┌─────────────┐
│ 8 GPUs      │◄──── EFA ──────►│ 8 GPUs      │
│             │   (400 Gbps)    │             │
└─────────────┘                 └─────────────┘

EFA (Elastic Fabric Adapter):
- Low latency (~10μs)
- High bandwidth (400 Gbps on p4d)
- NCCL uses EFA for all-reduce
```

---

## Example Workflows

### Accelerate Fine-Tuning (Qwen3-14B)

```bash
# 1. Download model (one-time)
helm install model-prep \
  charts/machine-learning/model-prep/hf-snapshot \
  --set-json='env=[{"name":"HF_MODEL_ID","value":"Qwen/Qwen3-14B"}]' \
  -n kubeflow-user-example-com

# 2. Fine-tune
helm install qwen3-sft \
  charts/machine-learning/training/pytorchjob-distributed \
  -f examples/training/accelerate/qwen3-14b-sft/fine-tune.yaml \
  -n kubeflow-user-example-com

# 3. Monitor
kubectl logs -f pytorchjob-qwen3-sft-master-0 -n kubeflow-user-example-com

# 4. Evaluate
helm install qwen3-eval \
  charts/machine-learning/training/pytorchjob-distributed \
  -f examples/training/accelerate/qwen3-14b-sft/evaluate.yaml \
  -n kubeflow-user-example-com

# 5. Convert checkpoint to HuggingFace format
helm install qwen3-convert \
  charts/machine-learning/training/pytorchjob-distributed \
  -f examples/training/accelerate/qwen3-14b-sft/convert-hf.yaml \
  -n kubeflow-user-example-com
```

### PyTorch Lightning Training

```bash
helm install lightning-qwen3 \
  charts/machine-learning/training/pytorchjob-distributed \
  -f examples/training/pytorch-lightning/qwen3-14b-sft/fine-tune.yaml \
  -n kubeflow-user-example-com
```

### Ray Train

```bash
helm install raytrain-qwen3 \
  charts/machine-learning/training/raytrain \
  -f examples/training/raytrain/qwen3-14b-sft/fine-tune.yaml \
  -n kubeflow-user-example-com
```

---

## Job Lifecycle

```
┌─────────────┐
│   Pending   │  Waiting for GPU nodes
└──────┬──────┘
       │
       v
┌─────────────┐
│   Running   │  Training in progress
└──────┬──────┘
       │
       v
┌─────────────┐     ┌─────────────┐
│  Succeeded  │ or  │   Failed    │
└─────────────┘     └─────────────┘
```

```bash
# Check job status
kubectl get pytorchjob -n kubeflow-user-example-com

# Check pods
kubectl get pods -n kubeflow-user-example-com | grep pytorchjob

# View logs
kubectl logs pytorchjob-<release>-master-0 -n kubeflow-user-example-com

# Delete job
helm uninstall <release> -n kubeflow-user-example-com
```

---

## Troubleshooting

### Job Stuck in Pending

```bash
# Check pod events
kubectl describe pod pytorchjob-<release>-master-0 -n kubeflow-user-example-com

# Common causes:
# - No GPU nodes available → Check Karpenter provisioner
# - Insufficient GPU quota → Check AWS service limits
# - PVC not found → Check pv-efs, pv-fsx exist
```

### Training Hangs

```bash
# Check if all pods are running
kubectl get pods -n kubeflow-user-example-com | grep pytorchjob

# Check NCCL connectivity
kubectl exec -it pytorchjob-<release>-master-0 -n kubeflow-user-example-com -- \
  env | grep -E "NCCL|PET_"

# Common causes:
# - NCCL timeout → Check EFA security groups
# - Mismatched ranks → Check PET_NNODES matches actual pods
```

### Out of Memory

```bash
# Check GPU memory usage
kubectl exec -it pytorchjob-<release>-master-0 -n kubeflow-user-example-com -- \
  nvidia-smi

# Solutions:
# - Reduce batch size in values.yaml
# - Enable gradient checkpointing
# - Use more nodes (nnodes) for model sharding
```

---

## Quick Reference

| I want to... | Look in... |
|--------------|------------|
| Change GPU count | values.yaml → `resources.requests."nvidia.com/gpu"` |
| Change node count | values.yaml → `resources.nnodes` |
| Change instance type | values.yaml → `resources.node_type` |
| Add pip packages | values.yaml → `pre_script` |
| Modify training script | chart's `scripts/<framework>/` directory |
| Change model path | values.yaml → `train.env` or `train.args` |
| View checkpoints | `/efs/home/<release>/checkpoints/` |
| View TensorBoard logs | `/efs/home/<release>/logs/` |

---

## Operator Environment Variables

### PyTorchJob (set by Training Operator)

| Variable | Description | Example |
|----------|-------------|---------|
| `PET_NNODES` | Total number of nodes | `2` |
| `PET_NPROC_PER_NODE` | Processes per node | `8` |
| `PET_NODE_RANK` | This node's rank | `0` (master), `1` (worker) |
| `PET_MASTER_ADDR` | Master pod IP | `10.0.1.5` |
| `PET_MASTER_PORT` | Rendezvous port | `29500` |

### MPIJob (set by MPI Operator)

| Variable | Description | Example |
|----------|-------------|---------|
| `OMPI_COMM_WORLD_SIZE` | Total MPI processes | `16` |
| `OMPI_COMM_WORLD_RANK` | This process rank | `0-15` |
| `OMPI_COMM_WORLD_LOCAL_RANK` | Local rank on node | `0-7` |

### RayJob (set by Ray)

| Variable | Description | Example |
|----------|-------------|---------|
| `RAY_ADDRESS` | Ray head address | `ray://head-svc:10001` |
| `RAY_NUM_GPUS` | GPUs allocated | `8` |

---

## Future: Kubeflow Trainer V2

The project currently uses Training Operator V1 (PyTorchJob, MPIJob). Kubeflow is developing Trainer V2 with a unified `TrainJob` API:

| Aspect | V1 (Current) | V2 (Future) |
|--------|--------------|-------------|
| CRDs | PyTorchJob, MPIJob, TFJob | Single `TrainJob` |
| Config | Replica specs per role | Simplified `trainer` section |
| SDK | YAML/Helm | Python SDK-first |

Migration guide: https://www.kubeflow.org/docs/components/trainer/operator-guides/migration/

**Recommendation**: Monitor V2 maturity. The current Helm-based approach provides similar abstraction benefits.
