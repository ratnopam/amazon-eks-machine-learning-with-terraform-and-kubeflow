# EKS Inference Architecture Guide

This document explains how inference examples work end-to-end, and how the different components (charts, containers, eks-cluster, examples) work together.

## Project Structure Overview

```
amazon-eks-machine-learning-with-terraform-and-kubeflow/
├── eks-cluster/           # Infrastructure (Terraform)
│   └── terraform/         # EKS, VPC, storage, addons
├── containers/            # Docker images for inference
│   ├── ray-pytorch/       # Ray Serve base image
│   ├── tritonserver-*/    # Triton variants
│   └── ...
├── charts/                # Helm charts
│   └── machine-learning/
│       ├── serving/       # Inference charts (rayserve, triton)
│       └── model-prep/    # Model download charts
└── examples/
    └── inference/         # User-facing examples
        ├── rayserve/      # Ray Serve examples
        └── triton-inference-server/  # Triton examples
```

## How Components Work Together

```
┌────────────────────────────────────────────────────────────────────────────┐
│                           COMPONENT RELATIONSHIPS                           │
└────────────────────────────────────────────────────────────────────────────┘

┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│   containers/   │     │     charts/     │     │    examples/    │
│                 │     │                 │     │                 │
│ Build Docker    │────>│ Helm templates  │<────│ User configs    │
│ images with     │     │ reference       │     │ (values.yaml)   │
│ inference deps  │     │ container images│     │                 │
└─────────────────┘     └────────┬────────┘     └─────────────────┘
                                 │
                                 v
                        ┌─────────────────┐
                        │   eks-cluster/  │
                        │                 │
                        │ EKS + Storage   │
                        │ (FSx, EFS)      │
                        └─────────────────┘
```

## End-to-End Inference Flow

### Phase 1: Infrastructure (Already Done)

Terraform creates:
- **EKS Cluster**: Kubernetes control plane
- **GPU Node Groups**: g6.48xlarge (8x L40S GPUs) via Karpenter
- **FSx for Lustre**: High-performance storage at `/fsx` for model weights
- **EFS**: Shared storage at `/efs` for logs and workspace
- **Operators**: Ray Operator, Training Operator, MPI Operator

### Phase 2: Model Preparation

**Chart Used**: `charts/machine-learning/model-prep/hf-snapshot`

```bash
helm install <model-name> \
    charts/machine-learning/model-prep/hf-snapshot \
    --set-json='env=[
        {"name":"HF_MODEL_ID","value":"meta-llama/Meta-Llama-3-8B-Instruct"},
        {"name":"HF_TOKEN","value":"your-token"}
    ]' \
    -n kubeflow-user-example-com
```

**What Happens**:
1. Creates a Kubernetes Job
2. Job downloads model from HuggingFace
3. Stores at `/fsx/pretrained-models/<org>/<model-name>/`
4. Model persists on FSx for all inference pods to access

### Phase 3: Container Images

**Location**: `/containers/`

| Container | Base Image | Purpose |
|-----------|------------|---------|
| `ray-pytorch` | `rayproject/ray:2.52.1-py312-cu128` | Ray Serve with vLLM |
| `tritonserver-ray-vllm` | `nvcr.io/nvidia/tritonserver:25.10-vllm-python-py3` | Triton + vLLM backend |
| `tritonserver-trtllm` | `nvcr.io/nvidia/tritonserver:25.10-trtllm-python-py3` | Triton + TensorRT-LLM |
| `ray-pytorch-neuronx-vllm` | Ray + AWS Neuron | Ray Serve on Trainium/Inferentia |

**Build Example**:
```bash
cd containers/ray-pytorch
./build_tools/build_and_push.sh us-west-2
```

### Phase 4: Deployment

---

## Ray Serve Flow

### Files in an Example

```
examples/inference/rayserve/meta-llama3-8b-vllm/
├── README.md           # Instructions
├── rayservice.yaml     # Helm values (main config)
└── serve.ipynb         # Jupyter notebook walkthrough
```

### rayservice.yaml Structure

```yaml
# 1. Container image
image:
  name: <account>.dkr.ecr.<region>.amazonaws.com/ray-pytorch:latest

# 2. Ray cluster configuration
ray:
  version: '2.52.1'
  ports:
    - {name: serve, value: 8000}      # Inference API
    - {name: dashboard, value: 8265}  # Ray dashboard

  # 3. Ray Serve application config
  serve_config_v2:
    serveConfigV2: |
      applications:
        - name: meta-llama3-8b-instruct
          import_path: openai_api:deployment    # Python module
          runtime_env:
            env_vars:
              ENGINE_CONFIG: "/etc/engine-config/engine-config.json"
            pip: ["vllm==0.11.0"]
          deployments:
            - name: VLLMDeployment
              ray_actor_options:
                num_gpus: 8                     # Tensor parallel

# 4. vLLM engine configuration
engine:
  config:
    served_model_name: "meta-llama3-8b-instruct"
    model: "/fsx/pretrained-models/meta-llama/Meta-Llama-3-8B-Instruct"
    tensor_parallel_size: 8

# 5. Resources
resources:
  requests: {"nvidia.com/gpu": 8}
  node_type: 'g6.48xlarge'

# 6. Storage mounts
pvc:
  - name: pv-fsx
    mount_path: /fsx
  - name: pv-efs
    mount_path: /efs
```

### What the Helm Chart Creates

**Chart**: `charts/machine-learning/serving/rayserve/`

```
helm install → Creates:
│
├── ConfigMap: engine-config
│   └── engine-config.json (vLLM settings from engine.config)
│
├── ConfigMap: framework-scripts
│   └── openai_api.py (from charts/serving/rayserve/scripts/vllm-0.11.0/)
│
└── RayService CR
    ├── Ray Head Pod
    │   ├── Runs Ray GCS + Serve controller
    │   ├── Mounts ConfigMaps at /etc/engine-config, /etc/framework-scripts
    │   └── Mounts PVCs at /fsx, /efs
    │
    └── Ray Worker Pods (auto-created)
        ├── Run Ray actors (vLLM AsyncLLMEngine)
        ├── Each actor uses num_gpus: 8
        └── Auto-scale based on target_ongoing_requests
```

### Runtime Execution

```
1. Ray head starts → imports openai_api.py
2. openai_api.py reads /etc/engine-config/engine-config.json
3. Creates vLLM AsyncLLMEngine with config
4. Loads model weights from /fsx/pretrained-models/...
5. Exposes FastAPI endpoints on port 8000
6. OpenAI-compatible API ready: /v1/chat/completions
```

---

## Triton Inference Server Flow

### Files in an Example

```
examples/inference/triton-inference-server/vllm_backend/llama3-8b-instruct/
├── README.md           # Instructions
├── triton_server.yaml  # Helm values
└── serve.ipynb         # Jupyter notebook
```

### triton_server.yaml Structure

```yaml
# 1. Container image
image:
  name: nvcr.io/nvidia/tritonserver:25.10-vllm-python-py3

# 2. Resources
resources:
  node_type: g6.48xlarge
  requests: {"nvidia.com/gpu": 8}

# 3. Storage
ebs:
  storage: 400Gi
  mount_path: /tmp

pvc:
  - name: pv-fsx
    mount_path: /fsx
  - name: pv-efs
    mount_path: /efs

# 4. Model configuration (inline scripts)
inline_script:
  - |
    cat > /tmp/config.pbtxt <<EOF
    backend: "vllm"
    instance_group [{count: 1, kind: KIND_MODEL}]
    EOF
  - |
    cat > /tmp/model.json <<EOF
    {
      "model": "$MODEL_PATH",
      "tensor_parallel_size": 8,
      "max_num_seqs": 8
    }
    EOF

# 5. Pre-startup script (setup model repo)
pre_script:
  - mkdir -p $MODEL_REPO/$MODEL_NAME/1
  - cp /tmp/model.json $MODEL_REPO/$MODEL_NAME/1/
  - cp /tmp/config.pbtxt $MODEL_REPO/$MODEL_NAME/

# 6. Server configuration
server:
  ports:
    - {name: http, value: 8000}   # REST API
    - {name: grpc, value: 8001}   # gRPC API
    - {name: metric, value: 8002} # Prometheus metrics
  env:
    - {name: MODEL_REPO, value: "/efs/home/{{ .Release.Name }}/model_repository"}
    - {name: MODEL_PATH, value: "/fsx/pretrained-models/meta-llama/Meta-Llama-3-8B-Instruct"}
  command: [tritonserver]
  args:
    - --model-repository=${MODEL_REPO}

# 7. Autoscaling
  autoscaling:
    minReplicas: 1
    maxReplicas: 4
    metrics:
      - type: Pods
        pods:
          metric: {name: avg_time_queue_us}
          target: {averageValue: 50}
```

### What the Helm Chart Creates

**Chart**: `charts/machine-learning/serving/triton-inference-server/`

```
helm install → Creates:
│
├── ConfigMap: launcher
│   └── launcher.sh (runs inline_script + pre_script + server command)
│
├── Deployment
│   ├── Container: tritonserver
│   │   ├── Runs launcher.sh on startup
│   │   ├── Mounts PVCs at /fsx, /efs
│   │   └── EBS volume at /tmp
│   └── Creates model repository structure
│
├── HorizontalPodAutoscaler
│   └── Scales based on avg_time_queue_us metric
│
└── Service
    ├── Port 8000: HTTP/REST
    ├── Port 8001: gRPC
    └── Port 8002: Metrics
```

### Runtime Execution

```
1. Pod starts → runs launcher.sh
2. inline_script creates config.pbtxt and model.json in /tmp
3. pre_script copies them to model repository structure:
   /efs/home/<release>/model_repository/
   └── llama3-8b-instruct/
       ├── config.pbtxt
       └── 1/
           └── model.json
4. tritonserver starts with --model-repository
5. vLLM backend reads model.json, loads from /fsx
6. Inference ready on ports 8000/8001
```

---

## Triton Backends Comparison

| Backend | Location | Use Case |
|---------|----------|----------|
| **vLLM** | `vllm_backend/` | Fast LLM inference, single/multi-node |
| **TensorRT-LLM** | `tensorrtllm_backend/` | Optimized NVIDIA inference |
| **Ray vLLM** | `ray_vllm_backend/` | Distributed inference with Ray |
| **Python** | `python_backend/` | Custom PyTorch models |

---

## Multi-Node Inference

For large models (70B+), use LeaderWorkerSet (LWS):

**Chart**: `charts/machine-learning/serving/triton-inference-server-lws/`

```yaml
# Example: Mixtral 8x22B across 2 nodes
resources:
  node_type: g6.48xlarge

lws:
  replicas: 2          # 2 nodes
  size: 2              # Workers per replica

engine:
  config:
    tensor_parallel_size: 8   # Within node
    pipeline_parallel_size: 2 # Across nodes
```

---

## Storage Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                      STORAGE LAYOUT                          │
└─────────────────────────────────────────────────────────────┘

FSx for Lustre (/fsx) - High Performance
├── pretrained-models/
│   └── meta-llama/
│       └── Meta-Llama-3-8B-Instruct/
│           ├── config.json
│           ├── model-00001-of-00004.safetensors
│           └── ...
└── Auto-syncs with S3 bucket (import_path)

EFS (/efs) - Shared Workspace
├── home/
│   └── <helm-release-name>/
│       ├── model_repository/    # Triton model configs
│       └── logs/                # Inference logs
└── Persistent across pod restarts
```

---

## Deployment Commands Summary

### Ray Serve Deployment

```bash
# 1. Build container (one-time)
cd containers/ray-pytorch
./build_tools/build_and_push.sh us-west-2

# 2. Download model
helm install model-prep \
    charts/machine-learning/model-prep/hf-snapshot \
    --set-json='env=[{"name":"HF_MODEL_ID","value":"meta-llama/Meta-Llama-3-8B-Instruct"}]' \
    -n kubeflow-user-example-com

# 3. Deploy inference
helm install rayserve-llama3 \
    charts/machine-learning/serving/rayserve/ \
    -f examples/inference/rayserve/meta-llama3-8b-vllm/rayservice.yaml \
    -n kubeflow-user-example-com

# 4. Test
kubectl port-forward svc/rayserve-llama3 8000:8000 -n kubeflow-user-example-com
curl http://localhost:8000/v1/models
```

### Triton Deployment

```bash
# 1. Download model (same as above)

# 2. Deploy inference
helm install triton-llama3 \
    charts/machine-learning/serving/triton-inference-server/ \
    -f examples/inference/triton-inference-server/vllm_backend/llama3-8b-instruct/triton_server.yaml \
    -n kubeflow-user-example-com

# 3. Test
kubectl port-forward svc/triton-llama3 8000:8000 -n kubeflow-user-example-com
curl -X POST http://localhost:8000/v2/models/llama3-8b-instruct/generate \
    -d '{"text_input": "Hello", "max_tokens": 50}'
```

---

## Configuration Inheritance

```
Example YAML (rayservice.yaml / triton_server.yaml)
         │
         │  User provides: image, resources, model path, engine config
         v
Helm Chart (serving/rayserve or serving/triton-*)
         │
         │  Chart adds: ConfigMaps, Service, Deployment/RayService
         │  Loads framework scripts from charts/serving/*/scripts/
         v
Kubernetes Resources
         │
         │  Creates: Pods, Services, HPA, ConfigMaps
         v
Runtime
         │
         │  Mounts storage, loads model, starts inference server
         v
API Endpoint (port 8000)
```

---

## Quick Reference: What Goes Where

| I want to... | Look in... |
|--------------|------------|
| Change model path | Example's `rayservice.yaml` or `triton_server.yaml` |
| Change GPU count | Example YAML → `resources.requests` |
| Change node type | Example YAML → `resources.node_type` |
| Add vLLM parameters | Example YAML → `engine.config` |
| Modify serving logic | Chart's `scripts/` directory |
| Change base image | `containers/` + rebuild |
| Add new model | Copy existing example, modify YAML |
| Debug issues | Check `/efs/home/<release>/logs/` |

---

## Troubleshooting

```bash
# Check pod status
kubectl get pods -n kubeflow-user-example-com

# Check logs
kubectl logs <pod-name> -n kubeflow-user-example-com

# Exec into pod
kubectl exec -it <pod-name> -n kubeflow-user-example-com -- /bin/bash

# Check Ray dashboard (for RayServe)
kubectl port-forward svc/<release>-head-svc 8265:8265 -n kubeflow-user-example-com
# Open http://localhost:8265

# Check model loaded
ls /fsx/pretrained-models/

# Check Triton model repository
ls /efs/home/<release>/model_repository/
```
