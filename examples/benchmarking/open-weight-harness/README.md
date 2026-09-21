# Open-Weight Model Benchmarking Harness

An agent harness for evaluating open-weight models and running benchmarks on Amazon EKS.

This work is intentionally isolated from the multi-model GPU colocation / MPS effort. Benchmark
numbers are only comparable when nothing else contends for the same GPUs, and an MPS-partitioned
GPU is shared by construction — so this gets its own cluster, its own Terraform state, and its
own branch.

> **Status:** cluster bring-up is documented and reproducible below. The harness itself is under
> active implementation.

## Cluster

| Property | Value |
| --- | --- |
| Cluster name | `owb-harness` |
| Region | `us-west-2` |
| AZs | `us-west-2a`, `us-west-2b`, `us-west-2c` |
| EFA AZ (`cuda_efa_az`) | `us-west-2c` |
| Prometheus | enabled (`kube-prometheus-stack`) |
| GPU metrics | NVIDIA `dcgm-exporter` via ServiceMonitor (CloudWatch's exporter off) |
| Neuron | not configured |

EFA comes from the `cudaefa` NodePool, which admits `p4d.24xlarge`, `p4de.24xlarge`,
`p5.48xlarge`, `p5e.48xlarge` and `p5en.48xlarge`. `us-west-2c` is the only zone in the region
offering all five — `p5e.48xlarge` is offered *only* there.

The cluster is created with EFA subnets tagged but **no GPU capacity reserved**. Karpenter
provisions p-family nodes on demand when a benchmark asks for them, so the cluster costs nothing
in GPU time while idle. If a benchmark run needs guaranteed capacity, attach an on-demand
capacity reservation and set `karpenter_cr_enabled` / `karpenter_cr_cudaefa_ids` instead of
relying on spot or on-demand availability.

## Bring-up

EFA requires the **advanced** setup path. The Quick Start template
(`ml-ops-desktop-basic.yaml`) does not support EFA, so the desktop and the cluster are created as
two separate steps.

### 1. S3 bucket

Holds Terraform state and the FSx for Lustre data repository. Must be globally unique.

```bash
aws s3 mb s3://<YOUR_S3_BUCKET> --region us-west-2
aws s3api put-bucket-versioning --bucket <YOUR_S3_BUCKET> \
  --versioning-configuration Status=Enabled
aws s3api put-object --bucket <YOUR_S3_BUCKET> --key ml-platform/
```

Versioning is not optional in practice — it is the only way back from a corrupted or
truncated `terraform.tfstate`.

### 2. ML Ops desktop

Create a stack from [`ml-ops-desktop.yaml`](../../../ml-ops-desktop.yaml). It needs an existing
VPC and a **public** subnet; the default VPC is fine, and note the desktop lives in a *different*
VPC than the cluster Terraform creates — they communicate over the public EKS API endpoint.

```bash
aws cloudformation create-stack \
  --region us-west-2 \
  --stack-name owb-harness-desktop \
  --template-body file://ml-ops-desktop.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters \
    ParameterKey=AWSUbuntuAMIType,ParameterValue=UbuntuPro2404LTS \
    ParameterKey=DesktopVpcId,ParameterValue=<VPC_ID> \
    ParameterKey=DesktopVpcSubnetId,ParameterValue=<PUBLIC_SUBNET_ID> \
    ParameterKey=DesktopHasPublicIpAddress,ParameterValue=true \
    ParameterKey=EbsVolumeSize,ParameterValue=1000 \
    ParameterKey=EbsVolumeType,ParameterValue=gp3 \
    ParameterKey=DesktopSecurityGroupId,ParameterValue= \
    ParameterKey=KeyName,ParameterValue=<EC2_KEY_PAIR> \
    ParameterKey=DesktopInstanceType,ParameterValue=m7i.2xlarge \
    ParameterKey=DesktopAccessCIDR,ParameterValue=<YOUR_IP>/32 \
    ParameterKey=EBSOptimized,ParameterValue=true \
    ParameterKey=UbuntuAMIOverride,ParameterValue=
```

Two things that will bite you:

- `UbuntuAMIOverride` has no default, so it must be passed explicitly as an empty value even
  though it is optional. Omitting it fails with
  `Parameters: [UbuntuAMIOverride] must have values`.
- This template attaches `PowerUserAccess` but **not** `AmazonSSMManagedInstanceCore`, so there
  is no Session Manager access. Reaching the desktop requires SSH with the key pair named above.
  (The basic template does attach SSM; the advanced one does not.)

`CREATE_COMPLETE` on the stack only means the instance launched. The desktop installs the DCV
server on first boot and **reboots**, so allow ~15 minutes before SSH succeeds. If you see
`Cloud init in progress!`, disconnect and retry. `ML Ops desktop is enabled!` means it is ready.

If your public IP changes, the security group ingress rule must be updated or SSH will hang
rather than fail fast.

### 3. Cluster, from the desktop

```bash
ssh -i <KEY>.pem ubuntu@<DESKTOP_PUBLIC_IP>

git clone https://github.com/<YOUR_FORK>/amazon-eks-machine-learning-with-terraform-and-kubeflow.git
cd amazon-eks-machine-learning-with-terraform-and-kubeflow
git checkout feature/open-weight-benchmarking-harness

./eks-cluster/utils/install-kubectl-linux.sh

# Logging out of public ECR first; an authenticated-but-stale token makes apply fail on
# anonymous pulls.
docker logout public.ecr.aws

./eks-cluster/utils/s3-backend.sh <YOUR_S3_BUCKET> owb-harness
```

`s3-backend.sh` **overwrites `backend.tf` in place**. On a machine that manages more than one
cluster from this directory, that silently repoints state at a different cluster — back the file
up first, or keep one checkout per cluster.

Then edit `cluster/owb-harness.tfvars` to replace `<YOUR_S3_BUCKET>` in `import_path`, and apply:

```bash
cd eks-cluster/terraform/aws-eks-cluster-and-nodegroup
terraform init
terraform apply -var-file=../../../examples/benchmarking/open-weight-harness/cluster/owb-harness.tfvars
```

Roughly 30–45 minutes.

### 4. Verify

```bash
aws eks update-kubeconfig --region us-west-2 --name owb-harness
kubectl get nodes

# EFA NodePool and its node class both present
kubectl get nodepool cudaefa
kubectl get ec2nodeclass cudaefa

# The EFA subnet tag actually landed on a us-west-2c subnet
aws ec2 describe-subnets --region us-west-2 \
  --filters "Name=tag:karpenter.sh/discovery/cudaefa,Values=owb-harness" \
  --query 'Subnets[].{Id:SubnetId,AZ:AvailabilityZone}' --output table

# Prometheus + the NVIDIA DCGM exporter
kubectl get pods -n kube-system -l app.kubernetes.io/name=kube-prometheus-stack-prometheus
kubectl get servicemonitor -A | grep dcgm
```

The subnet-tag check is worth running: if `cuda_efa_az` did not take, everything above still
comes up green and EFA benchmarks just quietly schedule onto non-EFA nodes.

Create the shared-storage home directories before running anything:

```bash
kubectl apply -f eks-cluster/utils/attach-pvc.yaml -n kubeflow
kubectl wait --for=condition=ready pod/attach-pvc -n kubeflow --timeout=300s
kubectl exec -it -n kubeflow attach-pvc -- bash -c \
  "cd /efs && mkdir -p home && chown 1000:100 home && cd /fsx && mkdir -p home && chown 1000:100 home"
```

## Related

`examples/inference/rayserve/` already carries vLLM deployments for a number of open-weight
models (Qwen3-32B, DeepSeek-R1 and its Qwen-32B distill, Llama 3.3 70B Instruct, Mixtral
8x22B Instruct, Pixtral 12B, Qwen2.5/3-VL 32B Instruct, BGE reranker v2 m3). These are the
natural first targets for the harness rather than new serving stacks.
