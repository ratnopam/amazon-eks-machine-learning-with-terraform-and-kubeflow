# Unified LLM Observability Layer

This document outlines the architecture and implementation plan for a unified observability layer that is agnostic of inference engine and server, supporting vLLM, Ray Serve, and Triton metrics along with GPU and Neuron metrics.

## Goals

- Unified metrics collection across vLLM, Ray Serve, and Triton
- GPU metrics (NVIDIA DCGM) and Neuron metrics (AWS Trainium/Inferentia)
- Real-time cost tracking for token usage
- LLM-specific metrics (TTFT, TPOT, throughput)
- Load test metrics integration (Locust)
- Grafana dashboards for visualization

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      UNIFIED OBSERVABILITY LAYER                             │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│   vLLM          │  │   Ray Serve     │  │   Triton        │
│   /metrics      │  │   /metrics      │  │   :8002/metrics │
└────────┬────────┘  └────────┬────────┘  └────────┬────────┘
         │                    │                    │
         └────────────────────┼────────────────────┘
                              │
                              v
┌─────────────────────────────────────────────────────────────────────────────┐
│                     Prometheus (ServiceMonitor CRDs)                         │
│  - LLM metrics (tokens, latency, TTFT, TPOT)                                │
│  - Cost metrics (tokens * price_per_token)                                  │
│  - Locust test metrics                                                       │
└─────────────────────────────────────────────────────────────────────────────┘
         │                    │                    │
         v                    v                    v
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│   DCGM Exporter │  │  Neuron Monitor │  │   Node Exporter │
│   (NVIDIA GPUs) │  │  (Trainium/Inf) │  │   (CPU/Memory)  │
└─────────────────┘  └─────────────────┘  └─────────────────┘
                              │
                              v
┌─────────────────────────────────────────────────────────────────────────────┐
│                          Grafana Dashboards                                  │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│  │ LLM Metrics │  │ Cost/Token  │  │ GPU/Neuron  │  │   Locust    │        │
│  │  Dashboard  │  │  Dashboard  │  │  Dashboard  │  │  Dashboard  │        │
│  └─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘        │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Component Details

### 1. Inference Engine Metrics

All three inference engines expose Prometheus-compatible `/metrics` endpoints natively.

| Engine | Metrics Endpoint | Native Prometheus | Key Metrics |
|--------|------------------|-------------------|-------------|
| **vLLM** | `:8000/metrics` | Yes | `vllm:num_requests`, `vllm:time_to_first_token`, `vllm:time_per_output_token`, `vllm:prompt_tokens_total`, `vllm:generation_tokens_total` |
| **Ray Serve** | `:8000/metrics` | Yes | `ray_serve_num_requests`, `ray_serve_request_latency`, `ray_serve_replica_processing_queries` |
| **Triton** | `:8002/metrics` | Yes | `nv_inference_request_success`, `nv_inference_queue_duration`, `nv_inference_compute_infer_duration` |

#### Unified Recording Rules

Since metric names differ across engines, create unified recording rules in Prometheus:

```yaml
# prometheus-rules.yaml
groups:
  - name: llm_unified_metrics
    rules:
      # Unified token counter
      - record: llm:tokens_total
        expr: |
          vllm:prompt_tokens_total + vllm:generation_tokens_total
          or ray_serve_tokens_total
          or nv_inference_tokens_total

      # Unified latency (P99)
      - record: llm:request_latency_p99
        expr: |
          histogram_quantile(0.99, vllm:request_latency_bucket)
          or histogram_quantile(0.99, ray_serve_request_latency_bucket)
          or histogram_quantile(0.99, nv_inference_request_duration_bucket)

      # Unified TTFT (Time to First Token)
      - record: llm:time_to_first_token_p99
        expr: |
          histogram_quantile(0.99, vllm:time_to_first_token_bucket)
          or histogram_quantile(0.99, ray_serve_ttft_bucket)

      # Unified throughput (tokens/sec)
      - record: llm:tokens_per_second
        expr: |
          rate(llm:tokens_total[1m])
```

---

### 2. Hardware Metrics

#### NVIDIA GPUs - DCGM Exporter

Already supported in this project via `dcgm_exporter_enabled` Terraform variable.

```bash
helm install dcgm-exporter gpu-helm-charts/dcgm-exporter
```

Key metrics:
| Metric | Description |
|--------|-------------|
| `DCGM_FI_DEV_GPU_UTIL` | GPU utilization % |
| `DCGM_FI_DEV_MEM_COPY_UTIL` | Memory utilization % |
| `DCGM_FI_DEV_FB_USED` | Framebuffer memory used (MB) |
| `DCGM_FI_DEV_POWER_USAGE` | Power draw (Watts) |
| `DCGM_FI_DEV_GPU_TEMP` | Temperature (Celsius) |
| `DCGM_FI_DEV_TOTAL_ENERGY_CONSUMPTION` | Total energy (mJ) |

#### AWS Neuron - Neuron Monitor

Deploy as DaemonSet on Trainium/Inferentia nodes:

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: neuron-monitor
  namespace: monitoring
spec:
  selector:
    matchLabels:
      app: neuron-monitor
  template:
    metadata:
      labels:
        app: neuron-monitor
    spec:
      nodeSelector:
        node.kubernetes.io/instance-type: trn1.32xlarge  # or inf2.*
      containers:
        - name: neuron-monitor
          image: public.ecr.aws/neuron/neuron-monitor:latest
          command:
            - /bin/sh
            - -c
            - "neuron-monitor | neuron-monitor-prometheus.py"
          ports:
            - containerPort: 8000
              name: metrics
          securityContext:
            privileged: true
          volumeMounts:
            - name: neuron-devices
              mountPath: /dev
      volumes:
        - name: neuron-devices
          hostPath:
            path: /dev
```

Key metrics:
| Metric | Description |
|--------|-------------|
| `neuroncore_utilization_ratio` | NeuronCore utilization % |
| `neuron_runtime_memory_used_bytes` | Memory used |
| `execution_latency_seconds` | Execution latency |
| `neuroncore_memory_used_bytes` | Per-core memory |

**Note**: For production, copy the Neuron Monitor image to your private ECR to avoid throttling.

---

### 3. Cost Tracking

#### Cost Calculation Recording Rules

```yaml
groups:
  - name: llm_cost_metrics
    rules:
      # Cost per model (prices from ConfigMap)
      - record: llm:cost_usd_total
        expr: |
          (llm:prompt_tokens_total * on(model) group_left(price) llm_model_input_price / 1000)
          +
          (llm:generation_tokens_total * on(model) group_left(price) llm_model_output_price / 1000)

      # Cost rate ($ per hour)
      - record: llm:cost_usd_per_hour
        expr: rate(llm:cost_usd_total[1h]) * 3600

      # Cost rate ($ per day)
      - record: llm:cost_usd_per_day
        expr: rate(llm:cost_usd_total[1d]) * 86400

      # Cost by namespace
      - record: llm:cost_usd_by_namespace
        expr: sum(llm:cost_usd_total) by (namespace)

      # Cost by model
      - record: llm:cost_usd_by_model
        expr: sum(llm:cost_usd_total) by (model)
```

#### Model Pricing ConfigMap

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: llm-pricing
  namespace: monitoring
data:
  pricing.yaml: |
    # Prices per 1K tokens (USD)
    # Based on self-hosted inference costs
    models:
      meta-llama3-8b-instruct:
        input_per_1k: 0.0001
        output_per_1k: 0.0002
      meta-llama3-70b-instruct:
        input_per_1k: 0.0008
        output_per_1k: 0.0016
      mixtral-8x22b-instruct:
        input_per_1k: 0.0006
        output_per_1k: 0.0012
      qwen3-14b:
        input_per_1k: 0.0002
        output_per_1k: 0.0004

    # GPU hourly costs (for TCO calculation)
    gpu_costs:
      p4d.24xlarge: 32.77  # $/hour on-demand
      p5.48xlarge: 98.32
      g6.48xlarge: 13.35
      inf2.48xlarge: 12.98
      trn1.32xlarge: 21.50
```

#### Cost Metrics Exporter (Custom)

For accurate cost tracking, deploy a small exporter that reads the pricing ConfigMap:

```python
# cost_exporter.py
from prometheus_client import start_http_server, Gauge
import yaml

model_input_price = Gauge('llm_model_input_price', 'Input price per 1K tokens', ['model'])
model_output_price = Gauge('llm_model_output_price', 'Output price per 1K tokens', ['model'])

def load_prices():
    with open('/config/pricing.yaml') as f:
        config = yaml.safe_load(f)

    for model, prices in config['models'].items():
        model_input_price.labels(model=model).set(prices['input_per_1k'])
        model_output_price.labels(model=model).set(prices['output_per_1k'])

if __name__ == '__main__':
    load_prices()
    start_http_server(9090)
```

---

### 4. Locust Load Test Metrics

Locust has native Prometheus exporter:

```bash
# Run Locust with Prometheus metrics
locust --master --prometheus-port 9100 -f loadtest.py
```

Key metrics:
| Metric | Description |
|--------|-------------|
| `locust_requests_total` | Total requests |
| `locust_requests_fail_total` | Failed requests |
| `locust_request_latency_seconds` | Request latency histogram |
| `locust_users_count` | Current user count |
| `locust_requests_current_rps` | Current RPS |

#### ServiceMonitor for Locust

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: locust
  namespace: monitoring
spec:
  selector:
    matchLabels:
      app: locust
  endpoints:
    - port: prometheus
      interval: 5s
```

---

### 5. Grafana Dashboards

#### Dashboard 1: LLM Metrics

Panels:
- Requests per second (by model)
- Time to First Token (TTFT) - P50, P95, P99
- Time per Output Token (TPOT) - P50, P95, P99
- Throughput (tokens/sec)
- Queue depth
- Active requests
- Error rate

#### Dashboard 2: Cost Tracker

Panels:
- Total cost (last 24h, 7d, 30d)
- Cost per hour (real-time)
- Cost by model (pie chart)
- Cost by namespace (pie chart)
- Token usage (input vs output)
- Cost projection (based on current rate)

#### Dashboard 3: GPU/Neuron Hardware

Panels:
- GPU utilization heatmap
- GPU memory usage
- GPU temperature
- GPU power consumption
- NeuronCore utilization (for Trainium/Inferentia)
- Neuron memory usage

#### Dashboard 4: Load Test Results

Panels:
- Request rate (RPS)
- Latency percentiles
- Error rate
- User ramp-up
- Response time distribution

---

## Implementation Plan

### Directory Structure

```
charts/
└── ml-platform/
    └── observability/
        ├── Chart.yaml
        ├── values.yaml
        ├── templates/
        │   ├── prometheus-rules.yaml       # Unified metrics + cost rules
        │   ├── servicemonitor-vllm.yaml
        │   ├── servicemonitor-rayserve.yaml
        │   ├── servicemonitor-triton.yaml
        │   ├── servicemonitor-locust.yaml
        │   ├── neuron-monitor-daemonset.yaml
        │   ├── cost-exporter-deployment.yaml
        │   ├── pricing-configmap.yaml
        │   └── grafana-dashboards-configmap.yaml
        └── dashboards/
            ├── llm-metrics.json
            ├── cost-tracker.json
            ├── gpu-neuron.json
            └── load-test.json
```

### Terraform Integration

```hcl
variable "observability_enabled" {
  description = "Enable unified LLM observability"
  type        = bool
  default     = false
}

variable "observability_namespace" {
  description = "Namespace for observability components"
  type        = string
  default     = "monitoring"
}

resource "helm_release" "kube_prometheus_stack" {
  count = var.observability_enabled ? 1 : 0

  name             = "kube-prometheus-stack"
  repository       = "https://prometheus-community.github.io/helm-charts"
  chart            = "kube-prometheus-stack"
  namespace        = var.observability_namespace
  create_namespace = true
  version          = "65.1.0"

  values = [
    yamlencode({
      prometheus = {
        prometheusSpec = {
          serviceMonitorSelectorNilUsesHelmValues = false
          podMonitorSelectorNilUsesHelmValues     = false
        }
      }
      grafana = {
        enabled = true
        sidecar = {
          dashboards = {
            enabled = true
          }
        }
      }
    })
  ]
}

resource "helm_release" "ml_observability" {
  count = var.observability_enabled ? 1 : 0

  name      = "ml-observability"
  chart     = "${path.module}/../../../charts/ml-platform/observability"
  namespace = var.observability_namespace

  depends_on = [helm_release.kube_prometheus_stack]
}
```

### Values.yaml

```yaml
# charts/ml-platform/observability/values.yaml

# Prometheus rules
prometheusRules:
  enabled: true

# ServiceMonitors
serviceMonitors:
  vllm:
    enabled: true
    interval: 15s
  rayserve:
    enabled: true
    interval: 15s
  triton:
    enabled: true
    interval: 15s
  locust:
    enabled: false
    interval: 5s

# Hardware monitoring
dcgmExporter:
  enabled: true  # Use existing dcgm_exporter_enabled

neuronMonitor:
  enabled: true
  image: public.ecr.aws/neuron/neuron-monitor:latest
  # For production, use private ECR
  # image: <account>.dkr.ecr.<region>.amazonaws.com/neuron-monitor:latest

# Cost tracking
costExporter:
  enabled: true
  image: python:3.11-slim

pricing:
  models:
    meta-llama3-8b-instruct:
      input_per_1k: 0.0001
      output_per_1k: 0.0002
    meta-llama3-70b-instruct:
      input_per_1k: 0.0008
      output_per_1k: 0.0016

# Grafana dashboards
dashboards:
  llmMetrics: true
  costTracker: true
  gpuNeuron: true
  loadTest: true
```

---

## Complexity Assessment

| Component | Effort | Notes |
|-----------|--------|-------|
| Prometheus Stack | **Low** | kube-prometheus-stack Helm chart |
| ServiceMonitors for vLLM/Ray/Triton | **Low** | Standard K8s CRDs |
| DCGM Exporter | **Low** | Already supported (`dcgm_exporter_enabled`) |
| Neuron Monitor | **Low** | DaemonSet + ServiceMonitor |
| Unified Recording Rules | **Medium** | Custom Prometheus rules |
| Cost Tracking | **Medium** | Custom exporter + pricing config |
| Locust Integration | **Low** | Native Prometheus support |
| Grafana Dashboards | **Medium** | 4 dashboards, 2 custom |

### Total Estimate: **Medium Complexity** (2-3 weeks)

---

## Existing Dashboards to Reuse

- [NVIDIA DCGM Exporter Dashboard](https://grafana.com/grafana/dashboards/12239-nvidia-dcgm-exporter-dashboard/) (ID: 12239)
- [Pulze LLM Application Overview](https://grafana.com/grafana/dashboards/19853-pulze-llm-application-overview/)
- [Ray Dashboard](https://grafana.com/grafana/dashboards/16850-ray-dashboard/)

---

## References

- [vLLM Metrics Documentation](https://docs.vllm.ai/en/latest/design/metrics/)
- [Ray Serve Observability](https://docs.ray.io/en/master/serve/llm/user-guides/observability.html)
- [Triton Metrics](https://docs.nvidia.com/deeplearning/triton-inference-server/user-guide/docs/user_guide/metrics.html)
- [NVIDIA DCGM Exporter](https://github.com/NVIDIA/dcgm-exporter)
- [AWS Neuron Monitor](https://aws.amazon.com/blogs/machine-learning/scale-and-simplify-ml-workload-monitoring-on-amazon-eks-with-aws-neuron-monitor-container/)
- [LLM Watch Grafana Plugin](https://github.com/anglerfishlyy/llm-watch-grafana)
- [Grafana LLM Observability Guide](https://grafana.com/blog/a-complete-guide-to-llm-observability-with-opentelemetry-and-grafana-cloud/)
- [vLLM OpenTelemetry Integration](https://www.parseable.com/blog/vllm-inference-metrics-otel)

---

## Assessment: Unified vs. Per-Framework Observability

This section evaluates whether a unified observability layer makes sense versus adding observability separately for each inference/training framework.

### Recommendation: Hybrid Approach

A pure unified approach has merit but a **hybrid approach** is more practical:

| Layer | Approach | Rationale |
|-------|----------|-----------|
| **Hardware Metrics** | Unified | DCGM and Neuron Monitor are framework-agnostic; no benefit to duplicating |
| **Cost Tracking** | Unified | Token pricing and GPU costs are universal concerns |
| **Load Testing** | Unified | Locust metrics apply regardless of backend |
| **Inference Metrics** | Per-Framework | vLLM, Ray Serve, Triton expose different metrics; abstraction loses detail |
| **Training Metrics** | Per-Framework | PyTorch, DeepSpeed, FSDP have distinct performance characteristics |

### Why Unified Works Well For:

**1. Hardware Metrics (DCGM + Neuron Monitor)**
- GPU/Neuron utilization is hardware-level, not framework-specific
- Same dashboard works for any workload
- Already implemented as DaemonSets

**2. Cost Tracking**
- Token counting is universal (input/output tokens)
- GPU hourly costs are fixed regardless of framework
- Single source of truth for billing/chargeback

**3. Load Testing (Locust)**
- HTTP endpoint testing is framework-agnostic
- TTFT, TPOT, throughput measured at API level
- Same test harness works for vLLM, Ray, Triton

### Why Per-Framework Works Better For:

**1. Inference Engine Metrics**
- vLLM exposes KV cache hit rates, speculative decoding stats
- Ray Serve has replica autoscaling metrics, batch queue depth
- Triton has model loading times, ensemble pipeline metrics
- Unified recording rules lose these framework-specific insights

**2. Training Metrics**
- PyTorchJob: gradient norm, loss curves, learning rate schedules
- MPIJob: all-reduce times, communication overhead
- RayJob: actor placement, object store usage
- Trying to unify these would be forced abstraction

### Proposed Hybrid Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        HYBRID OBSERVABILITY                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │                    UNIFIED LAYER                                     │   │
│   │   ┌───────────┐   ┌───────────┐   ┌───────────┐   ┌───────────┐    │   │
│   │   │   DCGM    │   │  Neuron   │   │   Cost    │   │  Locust   │    │   │
│   │   │  Exporter │   │  Monitor  │   │  Tracker  │   │  Metrics  │    │   │
│   │   └───────────┘   └───────────┘   └───────────┘   └───────────┘    │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │                 PER-FRAMEWORK LAYER                                  │   │
│   │   ┌───────────┐   ┌───────────┐   ┌───────────┐                     │   │
│   │   │   vLLM    │   │ Ray Serve │   │  Triton   │   (Inference)       │   │
│   │   │ Dashboard │   │ Dashboard │   │ Dashboard │                     │   │
│   │   └───────────┘   └───────────┘   └───────────┘                     │   │
│   │                                                                      │   │
│   │   ┌───────────┐   ┌───────────┐   ┌───────────┐                     │   │
│   │   │ PyTorch   │   │  MPI/     │   │   Ray     │   (Training)        │   │
│   │   │ Dashboard │   │ DeepSpeed │   │ Dashboard │                     │   │
│   │   └───────────┘   └───────────┘   └───────────┘                     │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Implementation Impact

| Approach | Complexity | Maintenance | Flexibility |
|----------|------------|-------------|-------------|
| Pure Unified | Medium | Low | Low (forced abstraction) |
| Pure Per-Framework | High | High (duplicate effort) | High |
| **Hybrid** | **Medium** | **Medium** | **High** |

### Final Verdict

**Use the unified approach documented above for:**
- Hardware monitoring (DCGM, Neuron)
- Cost tracking
- Load testing (Locust)
- High-level SLO dashboards (P99 latency, error rate)

**Add per-framework dashboards for:**
- Deep debugging and optimization
- Framework-specific tuning (KV cache, batch sizes)
- Training job monitoring

This gives you the best of both worlds: unified operational visibility while retaining framework-specific debugging capabilities.
