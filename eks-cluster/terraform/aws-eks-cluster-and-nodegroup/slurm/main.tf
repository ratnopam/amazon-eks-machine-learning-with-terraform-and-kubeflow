resource "kubernetes_namespace" "slurm" {
  metadata {
    labels = {
      istio-injection = "disabled"
    }

    name = "${var.slurm_namespace}"
  }
}

resource "random_password" "db" {
  length           = 16
  special          = false
}

data "aws_vpc" "db_vpc" {
  id = var.db_vpc_id
}

resource "helm_release" "slurm_ebs_sc" {
  name       = "slurm-ebs-sc"
  chart      = "${var.local_helm_repo}/ebs-sc"
  version    = "1.0.2"
  wait       = "false"

  values = [
    <<-EOT
      class_name: slurm-ebs-sc
      class_name_wait:  slurm-ebs-sc-wait
    EOT
  ]
  
}

resource "aws_security_group" "db_sg" {
  name = "${var.eks_cluster_id}-slurm-db-sg"
  description = "Security group for Slurm DB in vpc"
  vpc_id      = var.db_vpc_id

  ingress {
    from_port   = var.db_port 
    to_port     = var.db_port 
    protocol    = "tcp"
    cidr_blocks = [data.aws_vpc.db_vpc.cidr_block] 
  }
}

resource "aws_rds_cluster" "db" {
  engine             = "aurora-mysql"
  engine_mode        = "provisioned"
  database_name      = "slurm"
  master_username = "slurm"
  master_password = "${random_password.db.result}"
  storage_encrypted  = true
  skip_final_snapshot = true
  db_subnet_group_name = aws_db_subnet_group.this.name
  enable_http_endpoint = true
  port = var.db_port
  vpc_security_group_ids = [aws_security_group.db_sg.id]

  serverlessv2_scaling_configuration {
    max_capacity             = var.db_max_capacity
    min_capacity             = 0.5
  }
}

resource "aws_rds_cluster_instance" "db" {
  cluster_identifier = aws_rds_cluster.db.id
  instance_class     = "db.serverless"
  engine             = aws_rds_cluster.db.engine
  engine_version     = aws_rds_cluster.db.engine_version
  db_subnet_group_name = aws_db_subnet_group.this.name
}

resource "aws_db_subnet_group" "this" {
  name_prefix       = "slurm"
  subnet_ids = var.db_subnet_ids
}

resource "helm_release" "pv_efs" {
  count = var.storage_type == "efs" ? 1 : 0

  chart = "${var.local_helm_repo}/pv-efs"
  name = "pv-efs"
  version = "1.0.0"
  namespace = kubernetes_namespace.slurm.metadata[0].name
  
  values = [
    <<-EOT
      namespace: ${kubernetes_namespace.slurm.metadata[0].name}
      efs:
        volume_name: slurm-efs-pv
        claim_name: slurm-efs-pvc
        class_name: slurm-efs-sc
        fs_id: ${var.efs_fs_id}
        storage: ${var.storage_capacity}
    EOT
  ]
}

resource "helm_release" "pv_fsx" {
  count = var.storage_type == "fsx" ? 1 : 0

  chart = "${var.local_helm_repo}/pv-fsx"
  name = "pv-fsx"
  version = "1.1.0"
  namespace = kubernetes_namespace.slurm.metadata[0].name
  
  values = [
    <<-EOT
      namespace: ${kubernetes_namespace.slurm.metadata[0].name}
      fsx:
        volume_name: slurm-fsx-pv
        claim_name: slurm-fsx-pvc
        class_name: slurm-fsx-sc
        fs_id: ${var.fsx.fs_id}
        mount_name: ${var.fsx.mount_name}
        dns_name: ${var.fsx.dns_name}
        storage: ${var.storage_capacity}
    EOT
  ]
}

locals {
  pvc_release = var.storage_type == "efs" ? helm_release.pv_efs : helm_release.pv_fsx
  pvc_name = var.storage_type == "efs" ? "slurm-efs-pvc" : "slurm-fsx-pvc"
  pvc_path = var.storage_type == "efs" ? "/efs/home" : "/fsx/home"
}

resource "kubernetes_secret" "slurm_db_password" {                                                                                                                  
  metadata {                                                                                                                                                        
    name      = "slurm-db-password"                                                                                                                                 
    namespace = kubernetes_namespace.slurm.metadata[0].name                                                                                                         
  }                                                                                                                                                                 
                                                                                                                                                                    
  data = {                                                                                                                                                          
    password = random_password.db.result                                                                                                                            
  }                                                                                                                                                                 
}

resource "helm_release" "slurm" {
  chart = "oci://ghcr.io/slinkyproject/charts/slurm"
  name = "slurm"
  namespace = kubernetes_namespace.slurm.metadata[0].name
  version = "0.4.1"
  wait = true
  timeout = 600

  values = [
    <<-EOT
      # Login node configuration - port-forward access
      loginsets:
        default:
          enabled: ${var.login_enabled}
          replicas: 1
          login:
            volumeMounts:
              - name: shared-storage
                mountPath: ${local.pvc_path}
          rootSshAuthorizedKeys: |
            ${indent(12, join("\n", var.root_ssh_authorized_keys))}
          podSpec:
            volumes:
              - name: shared-storage
                persistentVolumeClaim:
                  claimName: ${local.pvc_name}
          service:
            spec:
              type: ClusterIP
            port: 22

      # Accounting - Aurora MySQL Serverless
      accounting:
        enabled: true
        storageConfig:
          enabled: true
          host: "${aws_rds_cluster.db.endpoint}"
          port: ${aws_rds_cluster.db.port}
          database: "${aws_rds_cluster.db.database_name}"
          username: "slurm"
          passwordKeyRef:
            name: slurm-db-password
            key: password


      # Enable Slurm exporter for metrics
      slurm-exporter:
        enabled: true

      # REST API
      restapi:
        service:
          type: ClusterIP

      # Controller
      controller:
        persistence:
          storageClass: "slurm-ebs-sc-wait"
          size: 100Gi
        service:
          type: ClusterIP

      # Compute NodeSet - Karpenter managed GPU nodes
      nodesets:
        slinky:
          enabled: true
          replicas: ${var.compute_nodeset_replicas}
          slurmd:
            image:
              repository: ${var.slurmd_image_repository}
              tag: ${var.slurmd_image_tag}
            resources:
              limits:
                nvidia.com/gpu: "${var.compute_gpu_per_node}"
                vpc.amazonaws.com/efa: ${var.compute_efa_per_node}
              requests:
                nvidia.com/gpu: "${var.compute_gpu_per_node}"
                vpc.amazonaws.com/efa: ${var.compute_efa_per_node}
            volumeMounts:
              - name: shared-storage
                mountPath: ${local.pvc_path}
              - name: shmem
                mountPath: /dev/shm
          podSpec:
            nodeSelector:
              karpenter.sh/nodepool: ${var.karpenter_nodepool_name}
              kubernetes.io/os: linux
            tolerations:
              - key: "nvidia.com/gpu"
                operator: "Exists"
                effect: "NoSchedule"
            volumes:
              - name: shared-storage
                persistentVolumeClaim:
                  claimName: ${local.pvc_name}
              - name: shmem
                hostPath:
                  path: /dev/shm
    EOT
  ]

  depends_on = [
    local.pvc_release,
    aws_rds_cluster.db,
    aws_rds_cluster_instance.db,
    kubernetes_secret.slurm_db_password,
    helm_release.slurm_operator,
    helm_release.slurm_ebs_sc
  ]
}


resource "helm_release" "slurm_operator" {
  chart = "oci://ghcr.io/slinkyproject/charts/slurm-operator"
  name = "slurm-operator"
  namespace = kubernetes_namespace.slurm.metadata[0].name
  version = "0.4.1"

  set {                                                                                                                                                             
    name  = "crds.enabled"                                                                                                                                          
    value = "true"                                                                                                                                                  
  }
}