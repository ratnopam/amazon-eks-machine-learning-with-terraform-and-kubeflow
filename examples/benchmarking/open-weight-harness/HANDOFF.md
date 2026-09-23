# Handoff: Open-Weight Benchmarking Harness

Working context for continuing this implementation from the ML Ops desktop. Read this before
touching anything; it records what is already done, what is verified vs. assumed, and the traps
that have already cost time.

## Where things stand

The `owb-harness` cluster is up in us-west-2 (EKS 1.35) with Prometheus, the NVIDIA
dcgm-exporter, EFS, FSx for Lustre, and Karpenter GPU + EFA node pools. Terraform state lives in
S3 under `owb-harness/terraform/state`. Get live values with `terraform output` rather than
copying IDs into docs.

**No GPU capacity is reserved.** Karpenter provisions on demand, so the cluster costs no GPU time
while idle. Nothing has been benchmarked yet.

### Verify before building on it

Two of these fail silently, so run them rather than assuming:

```bash
aws eks update-kubeconfig --region us-west-2 --name owb-harness
kubectl get nodes

# EFA. Must return exactly one subnet, in us-west-2c. Empty means cuda_efa_az did not take:
# every other check still passes and EFA benchmarks quietly schedule onto non-EFA nodes.
aws ec2 describe-subnets --region us-west-2 \
  --filters "Name=tag:karpenter.sh/discovery/cudaefa,Values=owb-harness" \
  --query 'Subnets[].{Id:SubnetId,AZ:AvailabilityZone}' --output table
kubectl get nodepool cudaefa ec2nodeclass cudaefa

# GPU metrics. The exporter pods stay Pending until a GPU node exists -- expected, not broken.
kubectl get servicemonitor -n kube-system dcgm-exporter
```

The dcgm-exporter must report `DCGM_FI_PROF_SM_ACTIVE` and `DCGM_FI_PROF_DRAM_ACTIVE` once a GPU
node is up. Those two fields are the point of choosing the NVIDIA exporter over the CloudWatch
addon's: they separate a model actually saturating the GPU from one latency-bound waiting on the
host. Confirm they appear in `/metrics` before trusting any utilization number.

## Phase 1 — prove it works

1. Port the dataset schema, judge scripts, and the `swe3` skill unchanged from the reference
   harness. Validate the **`bedrock` provider path first** via `claude-code-job` — that exercises
   every ported piece before anything EKS-specific is introduced, so a failure is unambiguous.
2. Onboard one open-weight model onto an `eks` provider, write `litellm-eks.yaml` against it, and
   drive one real task end to end.
3. Run the dataset against `bedrock` (Claude) and the onboarded open-weight model, and produce
   the first cost/Value comparison.

### Three corrections to that plan, found by reading the repo

- **Do not use `examples/inference/rayserve/meta-llama3-8b-vllm` as-is for step 2.** It is
  configured `tensor_parallel_size: 8` / `nvidia.com/gpu: 8`. An 8B model in bf16 is ~16GB and
  does not need 8 GPUs — that config books a full p4d/g6e.48xlarge for a smoke test. Llama 3 is
  also HF-gated (license acceptance). Copy `qwen3-32B-vllm`, drop tensor parallelism to 1, and
  point it at an ungated Qwen instead.
- **Pin the instance type.** The `cuda` NodePool admits `g4dn.xlarge` (T4, 16GB) and Karpenter
  picks cheapest, so an 8B bf16 model lands there and OOMs. Pin to `g6e.xlarge` (L40S, 48GB) or
  `g5.2xlarge`.
- **`baai-bge-reranker-v2-m3-vllm` is the only 1-GPU example, and it cannot serve this purpose.**
  It is a reranker with no chat-completions endpoint, so it cannot drive Claude Code. Fine as a
  pure "does the serving stack work" smoke test, useless as the benchmarked model.

## Reuse, do not rebuild

All of these were verified to exist on this branch:

| Need | Path |
| --- | --- |
| Headless Claude Code as a Job, Bedrock-wired via IRSA | `charts/machine-learning/agentic/claude-code-job` |
| Pull HF weights onto shared storage | `charts/machine-learning/model-prep/hf-snapshot` |
| Ray Serve + vLLM (OpenAI-compatible; multi-node via LeaderWorkerSet) | `charts/machine-learning/serving/rayserve` |
| Triton + TensorRT-LLM / vLLM backends | `charts/machine-learning/serving/triton-inference-server` |
| DJL-LMI, generic Deployment/Service/HPA | `charts/machine-learning/serving/{djl-lmi-server,generic-server}` |
| Sequenced Helm installs, base for a sweep pipeline | `kfp/pipelines/src/helm-charts-pipeline` |

Existing open-weight vLLM examples under `examples/inference/rayserve/`: Qwen3-32B,
DeepSeek-R1 and its Qwen-32B distill, Llama 3.3 70B Instruct, Mixtral 8x22B Instruct, Pixtral
12B, Qwen2.5/3-VL 32B Instruct, BGE reranker v2 m3. Prefer adapting one of these over authoring a
new serving stack.

## Genuinely new work

1. **`litellm-eks.yaml`** — Claude Code speaks only the Anthropic Messages API, so a LiteLLM proxy
   must translate to the OpenAI-compatible backend. Same shape as the reference harness's
   existing proxy config, but `api_base` points at in-cluster Service DNS. Discover the real name with
   `kubectl get svc -n kubeflow-user-example-com`; do not hand-construct it.
2. **A `provider: eks` value in the runner config**, parallel to `bedrock` / `litellm` / `vllm`.
3. **Model catalog / onboarding** — turning a new model name into a working `hf-snapshot` +
   serving-values pairing. No precedent in either repo.
4. **Value Score** — an optional per-task `verification` block (build/test commands) so a
   `patch.diff` is executed rather than only read, plus aggregation into Value per dollar. Extends
   the judge's output shape rather than replacing the judge.

**Validate tool-call round-tripping per backend before trusting any result from it.** Some
agent/tool-parser combinations do not round-trip cleanly, and this is known to differ across vLLM
parsers. A backend working for Bedrock says nothing about it working here.

## Traps already hit

- **`eks-cluster/utils/s3-backend.sh` overwrites `backend.tf` in place.** Never run the
  benchmarking cluster's terraform from a checkout that also manages another cluster — it
  silently repoints state and you lose the ability to destroy the other one.
- **This cluster must stay separate from any MPS/GPU-colocation cluster.** Benchmark numbers are
  only comparable when nothing else contends for the same GPUs, and an MPS-partitioned GPU is
  shared by construction.
- **dcgm-exporter ordering** — fixed on this branch. It creates a `ServiceMonitor`, whose
  `monitoring.coreos.com/v1` CRD ships with kube-prometheus-stack, so it must be ordered after
  that release. `serviceMonitorSelectorNilUsesHelmValues = false` makes *adoption*
  order-independent but says nothing about *creation*. Do not "simplify" that `depends_on` away.
- **`cuda_efa_az` must be `us-west-2c`.** The `cudaefa` NodePool admits only
  p4d/p4de/p5/p5e/p5en, and us-west-2c is the only zone in the region offering all five —
  `p5e.48xlarge` is offered *only* there.

## Repo conventions for this work

- This fork is **public**. Never commit account IDs, real IPs, customer names, internal doc
  links, HF tokens, or live resource IDs. Scrub before every push.
- No PR until the stack is proven with at least one open-weight model end to end — a PR is
  immediately public and visible upstream.
- Match the surrounding examples: one folder per concern, a README paired with a values file.
