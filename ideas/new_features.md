  ---
  1. Dynamic Resource Allocation (DRA) for GPUs

  What it is: DRA (GA in Kubernetes 1.34) replaces traditional device plugins with a more flexible GPU allocation model.

  Why it matters for this project:
  - Fine-grained GPU control (request specific GPU memory amounts)
  - Per-workload sharing strategies (MPS, time-slicing, MIG)
  - Topology-aware scheduling (understands NVLink for multi-GPU workloads)
  - Better support for GB200/NVLink multi-node GPU interconnects

  Implementation:
  - Add https://github.com/NVIDIA/k8s-dra-driver-gpu as optional Terraform module
  - Update training/inference charts to use ResourceClaim instead of nvidia.com/gpu requests
  - Works alongside LeaderWorkerSet for multi-node NVLink coordination

  Sources: https://aws.amazon.com/blogs/containers/unlocking-next-generation-ai-performance-with-dynamic-resource-allocation-on-amazon-eks-and-amazon-ec2-p6e-gb200/, https://github.com/NVIDIA/k8s-dra-driver-gpu

  ---
  2. Karpenter ODCR (On-Demand Capacity Reservations) Support

  What it is: Karpenter v1.3+ supports targeting specific capacity reservations for GPU instances.

  Why it matters for this project:
  - Guaranteed GPU availability for training jobs (no spot interruptions)
  - Pre-paid capacity is modeled as "free" - Karpenter consolidates to ODCRs first
  - Critical for P5/H100 instances which are often capacity-constrained

  Implementation:
  # Add to EC2NodeClass
  spec:
    capacityReservationSelectorTerms:
      - id: "cr-abc123"
      - tags:
          team: ml-training

  # Add to NodePool
  spec:
    template:
      spec:
        requirements:
          - key: karpenter.sh/capacity-type
            operator: In
            values: ["reserved", "on-demand"]  # Prioritize reserved

  Sources: https://karpenter.sh/docs/tasks/odcrs/, https://aws-ia.github.io/terraform-aws-eks-blueprints/patterns/machine-learning/targeted-odcr/

  ---
  3. Model Streaming for Faster Cold Starts

  What it is: Stream model weights directly from S3 to GPU memory, reducing load times by 5-10x.

  Why it matters for this project:
  - Current flow: S3 → FSx → Pod → GPU (slow, sequential)
  - With streaming: S3 → GPU (concurrent, parallel)
  - Llama-2 70B: 20x faster loading with streaming

  Implementation options:
  - https://docs.vllm.ai/en/stable/models/extensions/runai_model_streamer/: vLLM native support, 250+ concurrent threads
  - S3 Express One Zone: Single-digit ms latency (vs 10s of ms for standard S3)
  - Add load_format: "runai_streamer" option to rayservice.yaml/triton configs

  Example config:
  engine:
    config:
      load_format: "runai_streamer"
      model_loader_extra_config:
        concurrency: 64

  Sources: https://developer.nvidia.com/blog/reducing-cold-start-latency-for-llm-inference-with-nvidia-runai-model-streamer/, https://docs.vllm.ai/en/stable/models/extensions/runai_model_streamer/

  ---
  4. Batch Inference Support

  What it is: Offline processing of large datasets (vs real-time inference).

  Why it matters for this project:
  - Different optimization goal: throughput > latency
  - Use cases: meeting transcription, document processing, overnight analytics
  - Better GPU utilization through larger batches

  Implementation:
  - Add charts/machine-learning/serving/batch-inference/ chart
  - Use Ray Data + vLLM pattern for distributed batch processing
  - Integrate with Kubernetes Jobs (not Deployments)
  - Optional Kueue integration for job queuing

  Architecture:
  S3 (input data) → Ray Data → vLLM workers → S3 (results)
                       │
                    Kueue queue (optional)

  Sources: https://bentoml.com/llm/inference-optimization/offline-batch-inference, https://developers.redhat.com/articles/2025/08/07/batch-inference-openshift-ai-ray-data-vllm-and-codeflare

  ---
  5. Image/Video Generation Model Support

  What it is: Serving diffusion models (Stable Diffusion, FLUX, video models) differs from LLMs.

  Why it matters for this project:
  - Currently only LLM-focused (vLLM, TensorRT-LLM)
  - Growing demand for image/video generation APIs
  - Different resource patterns (more VRAM, different batching)

  Implementation:
  - Add examples/inference/diffusion/ with:
    - FLUX.1/FLUX.2 (image generation)
    - Stable Video Diffusion (video)
  - Use ComfyUI or Diffusers as backend
  - TensorRT optimization for 60% latency reduction
  - Support FP8 quantization on Hopper GPUs

  New containers needed:
  containers/
  ├── comfyui-server/
  └── diffusers-server/

  Sources: https://wavespeed.ai/blog/posts/20250702, https://huggingface.co/blog/video_gen, https://developer.nvidia.com/blog/optimizing-transformer-based-diffusion-models-for-video-generation-with-nvidia-tensorrt/

  ---
  6. Kueue Integration for Training Jobs

  What it is: Fair queuing and quota management for GPU workloads.

  Why it matters for this project:
  - Multi-tenant GPU sharing across teams
  - Job prioritization (preempt low-priority jobs)
  - Gang scheduling (ensure all pods start together)
  - Resource quotas per namespace

  Implementation:
  - Already have kueue_enabled in Terraform
  - Add examples showing:
    - ClusterQueue/LocalQueue setup
    - PyTorchJob with Kueue annotations
    - Priority classes for training jobs
  - Integrate with existing pytorchjob-distributed chart

  Example annotation:
  metadata:
    labels:
      kueue.x-k8s.io/queue-name: training-queue
      kueue.x-k8s.io/priority-class: high-priority

  Sources: https://kueue.sigs.k8s.io/

  ---
  Summary Table

  | Feature            | Complexity | Impact | Priority                           |
  |--------------------|------------|--------|------------------------------------|
  | DRA for GPUs       | High       | High   | Medium (wait for 1.34 adoption)    |
  | Karpenter ODCR     | Low        | High   | High (quick win)                   |
  | Model Streaming    | Medium     | High   | High (reduces cold starts)         |
  | Batch Inference    | Medium     | Medium | Medium                             |
  | Image/Video Models | High       | High   | Medium (new market)                |
  | Kueue Integration  | Low        | Medium | Low (examples already infra-ready) |