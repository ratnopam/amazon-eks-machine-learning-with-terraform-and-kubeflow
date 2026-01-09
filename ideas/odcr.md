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

The Karpenter module's v1 IAM policy (`modules/karpenter/policy.tf`) already includes:

```hcl
# Line 357-371 - Already present
statement {
  resources = [
    "arn:${local.partition}:ec2:${local.region}:*:capacity-reservation/*",
  ]
  actions = [
    "ec2:RunInstances",
    "ec2:CreateFleet"
  ]
}
```

**Missing Permission**: `ec2:DescribeCapacityReservations` is NOT in the current policy and is required for ODCR discovery.

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

### 3.2 Add IAM Permission

Add `ec2:DescribeCapacityReservations` to the Karpenter controller IAM policy.

**Option A**: Use `iam_policy_statements` variable in the Karpenter module:

```hcl
# main.tf - module.karpenter
module "karpenter" {
  # ... existing config ...

  iam_policy_statements = [
    {
      sid       = "AllowDescribeCapacityReservations"
      effect    = "Allow"
      actions   = ["ec2:DescribeCapacityReservations"]
      resources = ["*"]
    }
  ]
}
```

**Option B**: Create a separate IAM policy and attach it:

```hcl
resource "aws_iam_role_policy" "karpenter_odcr" {
  count = var.karpenter_enabled && var.karpenter_odcr_enabled ? 1 : 0

  name = "${var.cluster_name}-karpenter-odcr"
  role = module.karpenter[0].iam_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AllowDescribeCapacityReservations"
        Effect   = "Allow"
        Action   = ["ec2:DescribeCapacityReservations"]
        Resource = ["*"]
      }
    ]
  })
}
```

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

- [ ] **Phase 1: Prerequisites**
  - [ ] Purchase/verify ODCR in AWS Console or via CLI
  - [ ] Note ODCR ID, instance type, and AZ
  - [ ] Verify subnet AZ matches ODCR AZ

- [ ] **Phase 2: Terraform Changes**
  - [ ] Add new variables to `variables.tf`
  - [ ] Add IAM permission for `ec2:DescribeCapacityReservations`
  - [ ] Update `helm_release.karpenter` with feature gate
  - [ ] Update `helm_release.karpenter_components` with ODCR config

- [ ] **Phase 3: Helm Chart Changes**
  - [ ] Update `charts/karpenter-components/values.yaml`
  - [ ] Update `charts/karpenter-components/templates/node-class.yaml`
  - [ ] Update `charts/karpenter-components/templates/node-pool.yaml`
  - [ ] Bump chart version to 1.1.0

- [ ] **Phase 4: Testing**
  - [ ] Apply terraform changes
  - [ ] Verify EC2NodeClass shows ODCR in status
  - [ ] Create test pod with `reserved` nodeSelector
  - [ ] Verify node has ODCR labels
  - [ ] Test fallback behavior (if capacity exhausted)

- [ ] **Phase 5: Documentation**
  - [ ] Update README with ODCR configuration section
  - [ ] Add example tfvars for ODCR configuration

---

## 10. Files to Modify

| File | Change Type | Description |
|------|-------------|-------------|
| `variables.tf` | Add | New ODCR variables |
| `main.tf` | Modify | Feature gate, IAM policy, helm values |
| `charts/karpenter-components/values.yaml` | Modify | Add ODCR config structure |
| `charts/karpenter-components/templates/node-class.yaml` | Modify | Add capacityReservationSelectorTerms |
| `charts/karpenter-components/templates/node-pool.yaml` | Modify | Support multiple capacity types |
| `charts/karpenter-components/Chart.yaml` | Modify | Bump version to 1.1.0 |
| `README.md` | Modify | Document ODCR configuration |

---

## References

- [Karpenter ODCR Documentation](https://karpenter.sh/docs/tasks/odcrs/)
- [Karpenter ODCR Design Document](https://github.com/aws/karpenter-provider-aws/blob/main/designs/odcr.md)
- [AWS ODCR User Guide](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/ec2-capacity-reservations.html)
- [AWS Capacity Blocks](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/capacity-blocks-using.html)
