# Karpenter ODCR (On-Demand Capacity Reservations) Support

This document analyzes the implementation requirements for enabling ODCR support in Karpenter for this project.

---

## Executive Summary

| Aspect | Assessment |
|--------|------------|
| **Complexity** | Medium - Helm chart + Terraform changes |
| **Version Compatible** | Yes - Project uses Karpenter 1.5.0, ODCR requires 1.3+ |
| **IAM Changes Required** | Yes - Need `ec2:DescribeCapacityReservations` permission |
| **Workload Disruption Risk** | Low - Additive change, no breaking changes to existing workloads |
| **Pricing Impact** | Positive - Reserved capacity is pre-paid, reduces effective hourly cost |

---

## 1. What is ODCR?

On-Demand Capacity Reservations (ODCR) allow you to reserve compute capacity in a specific Availability Zone for any duration. This is particularly valuable for:

- **ML/AI workloads**: Guaranteeing GPU capacity (p4d, p5, g5, g6) during training/inference
- **Neuron instances**: Securing trn1/trn2/inf2 capacity which can be scarce
- **Peak workloads**: Ensuring capacity during known high-demand periods

### How Karpenter ODCR Works

1. Karpenter discovers ODCRs matching `capacityReservationSelectorTerms` in EC2NodeClass
2. When a NodePool allows `reserved` capacity type, Karpenter prioritizes ODCR capacity
3. If ODCR capacity is exhausted, Karpenter falls back to on-demand or spot (if allowed)
4. Nodes launched into ODCRs get labels:
   - `karpenter.k8s.aws/capacity-reservation-id`: The ODCR ID
   - `karpenter.k8s.aws/capacity-reservation-type`: "default" or "capacity-block"

---

## 2. Current Project State Analysis

### Karpenter Version

```hcl
# variables.tf:264
variable "karpenter_version" {
  default = "1.5.0"  # ODCR requires 1.3+
}
```

**Status**: Compatible - ODCR support was introduced in Karpenter v1.3 (Beta)

### Existing EC2NodeClass Configuration

The project defines three EC2NodeClasses in `charts/karpenter-components/templates/node-class.yaml`:

| NodeClass | Purpose | Subnet Discovery Tag |
|-----------|---------|---------------------|
| `default` | General GPU workloads | `karpenter.sh/discovery: {{ cluster_id }}` |
| `neuron` | Trainium/Inferentia | `karpenter.sh/discovery/neuron: {{ cluster_id }}` |
| `cudaefa` | High-bandwidth GPU (p4d, p5) | `karpenter.sh/discovery/cudaefa: {{ cluster_id }}` |

**Current ODCR Configuration**: None - No `capacityReservationSelectorTerms` defined

### Existing NodePool Configuration

NodePools in `charts/karpenter-components/templates/node-pool.yaml`:

| NodePool | Instance Types | Current Capacity Type |
|----------|---------------|----------------------|
| `neuron` | inf2.*, trn1.*, trn2.* | `on-demand` |
| `cuda` | g4dn.*, g5.*, g6.*, g6e.* | `on-demand` |
| `cudaefa` | p4d.*, p4de.*, p5.*, p5e.*, p5en.* | `on-demand` |

### Existing ODCR Variables (Unused by Karpenter)

```hcl
# variables.tf:507-517
variable "neuron_capacity_reservation_id" {
  description = "targeted odcr id for neuron type devices"
  default = ""
}

variable "nvidia_capacity_reservation_id" {
  description = "targeted odcr id for nvidia devices"
  default = ""
}
```

These variables are currently only used by EKS managed node groups (when `karpenter_enabled=false`), not by Karpenter.

### IAM Permissions Analysis

The `terraform-aws-modules/eks//modules/karpenter` module (v20.37.0) uses an IAM policy based on Karpenter v0.33 which does **not** include capacity-reservation resources in the RunInstances statement.

**Missing Permissions** (both are required):

1. `ec2:DescribeCapacityReservations` on `*` - Required for ODCR discovery
2. `ec2:RunInstances` and `ec2:CreateFleet` on `capacity-reservation/*` - Required to launch instances into ODCR

Without the second permission, Karpenter will fail with:
```
UnauthorizedOperation: You are not authorized to perform: ec2:RunInstances on resource:
arn:aws:ec2:us-west-2:ACCOUNT_ID:capacity-reservation/cr-xxxxx
```

---

## 3. Implementation Changes Required

### 3.1 Enable ReservedCapacity Feature Gate

Add feature gate to Karpenter Helm values in `main.tf`:

```hcl
# main.tf - helm_release.karpenter
values = [
  <<-EOT
    controller:
      resources:
        limits:
          cpu: 1
          memory: 2Gi
        requests:
          cpu: 1
          memory: 2Gi
    settings:
      clusterName: "${aws_eks_cluster.eks_cluster.id}"
      clusterEndpoint: "${aws_eks_cluster.eks_cluster.endpoint}"
      interruptionQueue: "${module.karpenter[0].queue_name}"
      featureGates:
        reservedCapacity: true  # NEW: Enable ODCR support
    serviceAccount:
      annotations:
        eks.amazonaws.com/role-arn: "${module.karpenter[0].iam_role_arn}"
    webhook:
      enabled: false
  EOT
]
```

### 3.2 Add IAM Permissions

Add both required permissions using the `iam_policy_statements` variable in the Karpenter module:

```hcl
# main.tf - module.karpenter
module "karpenter" {
  # ... existing config ...

  # ODCR support - add permissions for capacity reservations
  iam_policy_statements = var.karpenter_odcr_enabled ? [
    {
      sid       = "AllowDescribeCapacityReservations"
      effect    = "Allow"
      actions   = ["ec2:DescribeCapacityReservations"]
      resources = ["*"]
    },
    {
      sid       = "AllowRunInstancesOnCapacityReservation"
      effect    = "Allow"
      actions   = ["ec2:RunInstances", "ec2:CreateFleet"]
      resources = ["arn:aws:ec2:${var.region}:${data.aws_caller_identity.current.account_id}:capacity-reservation/*"]
    }
  ] : []
}
```

This adds both:
1. Permission to discover/describe ODCRs
2. Permission to launch instances into ODCRs (missing from the base Karpenter module policy)

### 3.3 Add New Terraform Variables

```hcl
# variables.tf - New variables

variable "karpenter_odcr_enabled" {
  description = "Enable ODCR support for Karpenter"
  type        = bool
  default     = false
}

variable "karpenter_odcr_neuron_selector" {
  description = "ODCR selector for Neuron instances (tag-based or ID-based)"
  type = object({
    type  = string  # "id" or "tags"
    value = map(string)
  })
  default = null
}

variable "karpenter_odcr_cuda_selector" {
  description = "ODCR selector for CUDA instances (tag-based or ID-based)"
  type = object({
    type  = string  # "id" or "tags"
    value = map(string)
  })
  default = null
}

variable "karpenter_odcr_cudaefa_selector" {
  description = "ODCR selector for CUDA EFA instances (tag-based or ID-based)"
  type = object({
    type  = string  # "id" or "tags"
    value = map(string)
  })
  default = null
}

variable "karpenter_capacity_types" {
  description = "Karpenter capacity types: 'on-demand', 'spot', 'reserved', or list like ['reserved', 'on-demand']"
  type        = list(string)
  default     = ["on-demand"]
}
```

### 3.4 Update Helm Chart - values.yaml

```yaml
# charts/karpenter-components/values.yaml
namespace: "karpenter"
role_name:
cluster_id:
consolidate_after: "600s"
capacity_type: "on-demand"  # Deprecated: use capacity_types
capacity_types:
  - "on-demand"
max_pods: 20

# ODCR Configuration
odcr:
  enabled: false
  neuron:
    enabled: false
    selector: {}  # e.g., { id: "cr-xxx" } or { tags: { "purpose": "ml-training" }}
  cuda:
    enabled: false
    selector: {}
  cudaefa:
    enabled: false
    selector: {}
```

### 3.5 Update Helm Chart - EC2NodeClass Template

```yaml
# charts/karpenter-components/templates/node-class.yaml
---
apiVersion: karpenter.k8s.aws/v1
kind: EC2NodeClass
metadata:
  name: neuron
  namespace: {{ .Values.namespace }}
spec:
  amiFamily: AL2023
  subnetSelectorTerms:
    - tags:
        karpenter.sh/discovery/neuron: "{{ .Values.cluster_id }}"
  securityGroupSelectorTerms:
    - tags:
        "kubernetes.io/cluster/{{ .Values.cluster_id }}": "owned"

  {{- if and .Values.odcr.enabled .Values.odcr.neuron.enabled }}
  capacityReservationSelectorTerms:
    {{- if .Values.odcr.neuron.selector.id }}
    - id: {{ .Values.odcr.neuron.selector.id }}
    {{- else if .Values.odcr.neuron.selector.tags }}
    - tags:
        {{- range $key, $value := .Values.odcr.neuron.selector.tags }}
        {{ $key }}: {{ $value | quote }}
        {{- end }}
    {{- end }}
  {{- end }}

  amiSelectorTerms:
    - alias: al2023@v20251103
  # ... rest of spec ...
```

### 3.6 Update Helm Chart - NodePool Template

```yaml
# charts/karpenter-components/templates/node-pool.yaml
---
apiVersion: karpenter.sh/v1
kind: NodePool
metadata:
  name: neuron
  namespace: {{ .Values.namespace }}
spec:
  disruption:
    consolidationPolicy: WhenEmpty
    consolidateAfter: {{ .Values.consolidate_after }}
  template:
    spec:
      nodeClassRef:
        group: karpenter.k8s.aws
        kind: EC2NodeClass
        name: neuron
      requirements:
      - key: kubernetes.io/arch
        operator: In
        values: ["amd64"]
      - key: kubernetes.io/os
        operator: In
        values: ["linux"]
      - key: karpenter.sh/capacity-type
        operator: In
        values:
          {{- if .Values.capacity_types }}
          {{- range .Values.capacity_types }}
          - {{ . | quote }}
          {{- end }}
          {{- else }}
          - {{ .Values.capacity_type | quote }}
          {{- end }}
      # ... rest of requirements ...
```

### 3.7 Update Terraform - Pass Values to Helm

```hcl
# main.tf - helm_release.karpenter_components
resource "helm_release" "karpenter_components" {
  count = var.karpenter_enabled ? 1 : 0

  chart     = "${var.local_helm_repo}/karpenter-components"
  name      = "karpenter-components"
  version   = "1.1.0"  # Bump version
  namespace = var.karpenter_namespace

  values = [
    <<-EOT
      namespace: "${var.karpenter_namespace}"
      role_name: "${module.karpenter[0].node_iam_role_name}"
      cluster_id: "${aws_eks_cluster.eks_cluster.id}"
      consolidate_after: "${var.karpenter_consolidate_after}"
      capacity_types: ${jsonencode(var.karpenter_capacity_types)}
      max_pods: ${var.karpenter_max_pods}
      odcr:
        enabled: ${var.karpenter_odcr_enabled}
        neuron:
          enabled: ${var.karpenter_odcr_neuron_selector != null}
          selector: ${var.karpenter_odcr_neuron_selector != null ? jsonencode(var.karpenter_odcr_neuron_selector.value) : "{}"}
        cuda:
          enabled: ${var.karpenter_odcr_cuda_selector != null}
          selector: ${var.karpenter_odcr_cuda_selector != null ? jsonencode(var.karpenter_odcr_cuda_selector.value) : "{}"}
        cudaefa:
          enabled: ${var.karpenter_odcr_cudaefa_selector != null}
          selector: ${var.karpenter_odcr_cudaefa_selector != null ? jsonencode(var.karpenter_odcr_cudaefa_selector.value) : "{}"}
    EOT
  ]

  depends_on = [helm_release.karpenter]
}
```

---

## 4. Usage Examples

### Example 1: ID-Based ODCR Selection

```hcl
# terraform.tfvars
karpenter_odcr_enabled = true
karpenter_capacity_types = ["reserved", "on-demand"]

karpenter_odcr_neuron_selector = {
  type = "id"
  value = {
    id = "cr-0abc123def456789"
  }
}
```

### Example 2: Tag-Based ODCR Selection

```hcl
# terraform.tfvars
karpenter_odcr_enabled = true
karpenter_capacity_types = ["reserved", "on-demand"]

karpenter_odcr_cudaefa_selector = {
  type = "tags"
  value = {
    tags = {
      "purpose" = "ml-training"
      "team"    = "data-science"
    }
  }
}
```

### Example 3: Reserved-Only (No Fallback)

```hcl
# terraform.tfvars - Only use reserved capacity, fail if unavailable
karpenter_odcr_enabled = true
karpenter_capacity_types = ["reserved"]  # No fallback!

karpenter_odcr_cudaefa_selector = {
  type = "id"
  value = {
    id = "cr-p5-training-cluster"
  }
}
```

---

## 5. Pricing Impact Analysis

### Cost Comparison

| Scenario | Pricing Model | Cost Behavior |
|----------|--------------|---------------|
| **Without ODCR** | On-Demand | Pay hourly rate when instance running |
| **With ODCR** | Pre-committed | Pay for reserved capacity whether used or not |
| **ODCR + Fallback** | Hybrid | Reserved when available, on-demand otherwise |

### Cost Optimization with Karpenter ODCR

1. **Capacity Guarantee**: ODCR ensures capacity availability for critical workloads
2. **No Discount**: Unlike Reserved Instances, ODCRs don't provide a discount - you pay on-demand rates
3. **Effective Savings**: The value comes from:
   - Guaranteed capacity during shortage periods
   - Avoiding spot interruptions for long-running training jobs
   - Eliminating manual capacity management

### Pricing Priority in Karpenter

When multiple capacity types are allowed, Karpenter prioritizes:

```
Reserved (ODCR) → Spot → On-Demand
```

This means:
- ODCRs are used first (since you're already paying for them)
- Spot is used if no ODCR capacity available
- On-Demand is the last resort

### Recommendation

| Use Case | Recommended Capacity Types |
|----------|---------------------------|
| **Training jobs** (interruptible) | `["reserved", "spot", "on-demand"]` |
| **Training jobs** (checkpointing) | `["reserved", "on-demand"]` |
| **Inference** (always available) | `["reserved", "on-demand"]` |
| **Development/Testing** | `["spot", "on-demand"]` (no ODCR) |

---

## 6. Workload Disruption Assessment

### Will Existing Workloads Be Affected?

**No** - Enabling ODCR is an additive change:

1. **Existing nodes**: Continue running on on-demand capacity
2. **New nodes**: May use ODCR if available and NodePool allows `reserved`
3. **No pod evictions**: Adding `reserved` to capacity types doesn't evict existing pods
4. **Gradual migration**: Consolidation will naturally move workloads to ODCR over time

### Migration Path (Zero Disruption)

```
Step 1: Enable ODCR feature gate (no workload impact)
Step 2: Add IAM permissions (no workload impact)
Step 3: Configure capacityReservationSelectorTerms (no workload impact)
Step 4: Add "reserved" to capacity_types (new nodes prefer ODCR)
Step 5: Wait for natural consolidation or trigger it manually
```

### Consolidation Behavior

When ODCR is enabled and consolidation runs:

1. Karpenter sees ODCR capacity as "cheaper" (near-zero calculated price)
2. Consolidation prefers moving workloads INTO reserved capacity
3. Existing on-demand nodes are drained and replaced with ODCR nodes

To prevent aggressive consolidation:

```yaml
# Increase consolidate_after
consolidate_after: "3600s"  # 1 hour instead of 10 minutes
```

---

## 7. Testing and Validation

### Pre-Deployment Validation

1. **Verify ODCR exists and is active**:
   ```bash
   aws ec2 describe-capacity-reservations \
     --capacity-reservation-ids cr-0abc123def456789
   ```

2. **Check ODCR matches instance types in NodePool**:
   ```bash
   # ODCR instance type must match NodePool requirements
   aws ec2 describe-capacity-reservations \
     --query 'CapacityReservations[].{Id:CapacityReservationId,Type:InstanceType,AZ:AvailabilityZone,Available:AvailableInstanceCount}'
   ```

3. **Verify subnet is in same AZ as ODCR**:
   ```bash
   # ODCR is AZ-specific, subnet must match
   aws ec2 describe-subnets \
     --filters "Name=tag:karpenter.sh/discovery/neuron,Values=${CLUSTER_ID}" \
     --query 'Subnets[].{SubnetId:SubnetId,AZ:AvailabilityZone}'
   ```

### Post-Deployment Validation

1. **Check EC2NodeClass status**:
   ```bash
   kubectl describe ec2nodeclass neuron
   # Look for status.capacityReservations
   ```

2. **Verify NodePool can schedule reserved**:
   ```bash
   kubectl get nodepool neuron -o yaml | grep -A5 "capacity-type"
   ```

3. **Create test pod and verify node labels**:
   ```bash
   kubectl run test-gpu --image=nvidia/cuda:12.0-base \
     --overrides='{"spec":{"tolerations":[{"key":"nvidia.com/gpu","operator":"Exists","effect":"NoSchedule"}],"nodeSelector":{"karpenter.sh/capacity-type":"reserved"}}}'

   # Check node was launched with ODCR
   kubectl get node -l karpenter.k8s.aws/capacity-reservation-id
   ```

4. **Monitor Karpenter logs**:
   ```bash
   kubectl logs -n kube-system -l app.kubernetes.io/name=karpenter -f | grep -i "capacity"
   ```

### Rollback Procedure

If issues occur:

1. Remove `reserved` from `karpenter_capacity_types`:
   ```hcl
   karpenter_capacity_types = ["on-demand"]
   ```

2. Apply terraform:
   ```bash
   terraform apply
   ```

3. Existing ODCR nodes continue running but new nodes use on-demand

---

## 8. Limitations and Considerations

### Karpenter ODCR Limitations

| Limitation | Impact | Workaround |
|------------|--------|------------|
| No open matching | Must explicitly specify ODCR via selector | Use tag-based selection for flexibility |
| AZ-specific | ODCR capacity is tied to single AZ | Create ODCRs in all required AZs |
| Instance type specific | One ODCR = one instance type | Create multiple ODCRs for different sizes |
| No automatic attachment | Instances must be launched INTO ODCR | Karpenter handles this automatically |

### Capacity Blocks (Not Yet Supported in This Project)

Capacity Blocks are time-limited ODCRs (up to 28 days) that became supported in Karpenter v1.6. Key differences:

- Capacity Blocks have expiration times
- Karpenter drains nodes 10 minutes before EC2 terminates them
- Useful for scheduled training jobs

**Future enhancement**: Add support for Capacity Blocks with automatic job scheduling.

### Multi-AZ Considerations

ODCRs are AZ-specific. If you have:
- ODCR in `us-west-2a` for trn1.32xlarge
- Subnet tagged for Karpenter in `us-west-2b`

Karpenter will NOT be able to use the ODCR because the AZs don't match.

**Solution**: Ensure ODCR AZ matches the subnet AZ for that NodeClass:

```hcl
# main.tf - Create subnets with ODCR-aware tagging
resource "aws_subnet" "private" {
  # ...
  tags = {
    # Only tag subnet in ODCR AZ for neuron discovery
    "karpenter.sh/discovery/neuron" = var.azs[count.index] == var.odcr_neuron_az ? var.cluster_name : "nil"
  }
}
```

---

## 9. Implementation Checklist

- [x] **Phase 1: Prerequisites**
  - [x] Purchase/verify ODCR in AWS Console or via CLI:
    ```bash
    # Create an ODCR
    aws ec2 create-capacity-reservation \
      --instance-type g5.xlarge \
      --instance-platform Linux/UNIX \
      --availability-zone us-west-2a \
      --instance-count 1 \
      --instance-match-criteria targeted

    # Describe existing ODCRs
    aws ec2 describe-capacity-reservations \
      --query 'CapacityReservations[].{Id:CapacityReservationId,Type:InstanceType,AZ:AvailabilityZone,State:State,Available:AvailableInstanceCount,Total:TotalInstanceCount}'
    ```
  - [x] Note ODCR ID, instance type, and AZ
  - [x] Verify subnet AZ matches ODCR AZ

- [x] **Phase 2: Terraform Changes**
  - [x] Add new variables to `variables.tf`
  - [x] Add IAM permissions for `ec2:DescribeCapacityReservations` AND `ec2:RunInstances`/`ec2:CreateFleet` on `capacity-reservation/*`
  - [x] Update `helm_release.karpenter` with feature gate
  - [x] Update `helm_release.karpenter_components` with ODCR config

- [x] **Phase 3: Helm Chart Changes**
  - [x] Update `charts/karpenter-components/values.yaml`
  - [x] Update `charts/karpenter-components/templates/node-class.yaml`
  - [x] Update `charts/karpenter-components/templates/node-pool.yaml`
  - [ ] Bump chart version (skipped - kept at 1.0.7)

- [x] **Phase 4: Testing**
  - [x] Apply terraform changes
  - [x] Verify EC2NodeClass shows ODCR in status
  - [x] Create test workload (RayService)
  - [x] Verify node has `karpenter.sh/capacity-type: reserved` label
  - [x] Test fallback behavior with `["reserved", "on-demand"]`

- [x] **Phase 5: Documentation**
  - [x] Update README with ODCR configuration section
  - [x] Add example terraform apply command with ODCR variables

---

## 10. Lessons Learned from Testing

### IAM Policy Gap in terraform-aws-modules/eks

The `terraform-aws-modules/eks//modules/karpenter` module (v20.37.0) uses an IAM policy based on Karpenter v0.33. This older policy version does **not** include `capacity-reservation/*` in the `ec2:RunInstances` resource list.

**Symptom**: Karpenter controller logs show:
```
UnauthorizedOperation: You are not authorized to perform: ec2:RunInstances on resource:
arn:aws:ec2:us-west-2:ACCOUNT_ID:capacity-reservation/cr-0cd766e74d14184fd
```

**Root Cause**: The module's policy includes RunInstances for instances, volumes, network interfaces, etc., but not capacity reservations.

**Solution**: Add both permissions via `iam_policy_statements`:
1. `ec2:DescribeCapacityReservations` on `*`
2. `ec2:RunInstances` and `ec2:CreateFleet` on `capacity-reservation/*`

### ODCR AZ Matching

ODCRs are AZ-specific. If you create an ODCR in `us-west-2a` but Karpenter launches in `us-west-2c`, the ODCR won't be used. Ensure:
- Multiple ODCRs in different AZs, or
- NodePool/NodeClass subnet selector limits to ODCR's AZ

---

## 11. Files Modified

| File | Change Type | Description | Status |
|------|-------------|-------------|--------|
| `variables.tf` | Add | 8 new ODCR variables | Done |
| `main.tf` | Modify | Feature gate, IAM policies (both DescribeCapacityReservations AND RunInstances), helm values | Done |
| `charts/karpenter-components/values.yaml` | Modify | Add ODCR config structure | Done |
| `charts/karpenter-components/templates/node-class.yaml` | Modify | Add capacityReservationSelectorTerms | Done |
| `charts/karpenter-components/templates/node-pool.yaml` | Modify | Support multiple capacity types | Done |
| `charts/karpenter-components/Chart.yaml` | Skip | Kept at 1.0.7 | Skipped |
| `README.md` | Modify | Document ODCR configuration and variables | Done |

---

## 12. References

- [Karpenter ODCR Documentation](https://karpenter.sh/docs/tasks/odcrs/)
- [Karpenter ODCR Design Document](https://github.com/aws/karpenter-provider-aws/blob/main/designs/odcr.md)
- [AWS ODCR User Guide](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-reservations.html)
- [AWS Capacity Blocks](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/capacity-blocks-using.html)
