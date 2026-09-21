# Terraform variables for the open-weight benchmarking cluster.
#
# Deliberately a separate cluster from any GPU-colocation / MPS test cluster: benchmark
# numbers are only comparable if nothing else is contending for the same GPUs, and an
# MPS-partitioned GPU is by construction shared.
#
# Usage, from the ML Ops desktop:
#   cd eks-cluster/terraform/aws-eks-cluster-and-nodegroup
#   terraform apply -var-file=../../../examples/benchmarking/open-weight-harness/cluster/owb-harness.tfvars

cluster_name = "owb-harness" # 16 char limit
region       = "us-west-2"
azs          = ["us-west-2a", "us-west-2b", "us-west-2c"]
profile      = "default"

# Replace with the bucket you created for Terraform state + FSx Lustre data.
import_path = "s3://<YOUR_S3_BUCKET>/ml-platform/"

# EFA. Setting this tags the subnet in this AZ with `karpenter.sh/discovery/cudaefa`,
# which is what the `cudaefa` EC2NodeClass selects on. Without it the `cudaefa` NodePool
# has no subnet to launch into and multi-node benchmarks silently fall back to the
# non-EFA `cuda` pool.
#
# us-west-2c and not another zone: the `cudaefa` NodePool admits exactly p4d.24xlarge,
# p4de.24xlarge, p5.48xlarge, p5e.48xlarge and p5en.48xlarge, and us-west-2c is the only
# zone in this region that offers all of them -- p5e.48xlarge is offered *only* there.
# Picking 2a or 2b would quietly narrow which instance types can ever be benchmarked.
cuda_efa_az = "us-west-2c"

# No Neuron subnets. Open-weight benchmarking here targets CUDA; add a zone that supports
# inf/trn if the harness is later extended to Neuron.
neuron_az = "none"

prometheus_enabled = true

# GPU metrics: hand them to the NVIDIA dcgm-exporter, not the CloudWatch addon's.
# The NVIDIA exporter carries DCGM_FI_PROF_SM_ACTIVE and DCGM_FI_PROF_DRAM_ACTIVE; the
# addon's operator-managed field list omits both. Those two fields are what separate a
# model that is actually saturating the GPU from one that is latency-bound waiting on the
# host, which is the whole question a benchmark harness exists to answer.
#
# Both true would put two DCGM hostengines on the same GPUs, so the CloudWatch one is
# turned off rather than left at its default.
dcgm_exporter_enabled            = true
cloudwatch_dcgm_exporter_enabled = false
