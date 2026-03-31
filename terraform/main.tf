###############################################################################
# ACRE — AWS Infrastructure (Terraform)
# Provisions: EKS, RDS (Postgres), ElastiCache (Redis), S3, ECR, VPC
###############################################################################

terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.0"
    }
  }
  backend "s3" {
    bucket = "acre-terraform-state"
    key    = "prod/terraform.tfstate"
    region = "us-east-1"
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_availability_zones" "available" {}

###############################################################################
# VPC
###############################################################################
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = "${var.project_name}-vpc"
  cidr = "10.0.0.0/16"

  azs             = slice(data.aws_availability_zones.available.names, 0, 3)
  private_subnets = ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]
  public_subnets  = ["10.0.101.0/24", "10.0.102.0/24", "10.0.103.0/24"]

  enable_nat_gateway     = true
  single_nat_gateway     = false  # one per AZ for HA
  enable_dns_hostnames   = true
  enable_dns_support     = true

  public_subnet_tags = {
    "kubernetes.io/role/elb" = "1"
  }
  private_subnet_tags = {
    "kubernetes.io/role/internal-elb" = "1"
  }
}

###############################################################################
# EKS Cluster
###############################################################################
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = "${var.project_name}-eks"
  cluster_version = "1.29"

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  cluster_endpoint_public_access = true

  # Managed node groups
  eks_managed_node_groups = {
    # General purpose (API, ingestion, workers)
    general = {
      min_size     = 2
      max_size     = 10
      desired_size = 3
      instance_types = ["t3.xlarge"]
      capacity_type  = "ON_DEMAND"

      labels = { role = "general" }
    }

    # GPU nodes for fine-tuning jobs
    gpu = {
      min_size     = 0
      max_size     = 2
      desired_size = 0
      instance_types = ["g4dn.xlarge"]
      capacity_type  = "ON_DEMAND"

      labels = { role = "gpu" }
      taints = [{
        key    = "nvidia.com/gpu"
        value  = "true"
        effect = "NO_SCHEDULE"
      }]

      # Bootstrap NVIDIA device plugin
      pre_bootstrap_user_data = <<-EOT
        yum install -y amazon-ssm-agent
        amazon-linux-extras install -y lustre2.10
      EOT
    }
  }

  # Enable IRSA (IAM Roles for Service Accounts)
  enable_irsa = true

  tags = var.tags
}

###############################################################################
# RDS (PostgreSQL)
###############################################################################
resource "aws_db_subnet_group" "acre" {
  name       = "${var.project_name}-db-subnet"
  subnet_ids = module.vpc.private_subnets
}

resource "aws_security_group" "rds" {
  name   = "${var.project_name}-rds-sg"
  vpc_id = module.vpc.vpc_id

  ingress {
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [module.eks.cluster_security_group_id]
  }
}

resource "aws_db_instance" "acre" {
  identifier             = "${var.project_name}-postgres"
  engine                 = "postgres"
  engine_version         = "16.2"
  instance_class         = "db.t3.medium"
  allocated_storage      = 50
  max_allocated_storage  = 200
  storage_encrypted      = true
  db_name                = "acre"
  username               = "acre"
  password               = var.db_password
  db_subnet_group_name   = aws_db_subnet_group.acre.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  multi_az               = true
  backup_retention_period = 7
  deletion_protection    = true
  skip_final_snapshot    = false
  final_snapshot_identifier = "${var.project_name}-final"

  tags = var.tags
}

###############################################################################
# ElastiCache (Redis)
###############################################################################
resource "aws_elasticache_subnet_group" "acre" {
  name       = "${var.project_name}-cache-subnet"
  subnet_ids = module.vpc.private_subnets
}

resource "aws_elasticache_replication_group" "acre" {
  replication_group_id = "${var.project_name}-redis"
  description          = "ACRE Redis cluster"
  node_type            = "cache.t3.medium"
  num_cache_clusters   = 2    # primary + 1 replica
  port                 = 6379
  subnet_group_name    = aws_elasticache_subnet_group.acre.name
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  automatic_failover_enabled = true

  tags = var.tags
}

###############################################################################
# S3 Buckets
###############################################################################
resource "aws_s3_bucket" "artifacts" {
  bucket = "${var.project_name}-artifacts-${var.aws_account_id}"
  tags   = var.tags
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "repo-snapshots"
    status = "Enabled"
    filter { prefix = "snapshots/" }
    expiration { days = 90 }
  }

  rule {
    id     = "model-artifacts"
    status = "Enabled"
    filter { prefix = "models/" }
    noncurrent_version_expiration { noncurrent_days = 30 }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

###############################################################################
# ECR Repositories
###############################################################################
locals {
  ecr_repos = ["ingestion", "agents", "api", "finetuning", "dashboard"]
}

resource "aws_ecr_repository" "acre" {
  for_each = toset(local.ecr_repos)

  name                 = "${var.project_name}-${each.value}"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "acre" {
  for_each   = aws_ecr_repository.acre
  repository = each.value.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

###############################################################################
# SQS (for async task overflow)
###############################################################################
resource "aws_sqs_queue" "analysis_queue" {
  name                       = "${var.project_name}-analysis.fifo"
  fifo_queue                 = true
  content_based_deduplication = true
  visibility_timeout_seconds = 900  # 15 min — long enough for one analysis
  message_retention_seconds  = 86400

  tags = var.tags
}

###############################################################################
# CloudWatch Log Groups
###############################################################################
resource "aws_cloudwatch_log_group" "eks_logs" {
  name              = "/aws/eks/${var.project_name}/cluster"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "app_logs" {
  for_each          = toset(["ingestion", "agents", "api", "finetuning"])
  name              = "/acre/${each.value}"
  retention_in_days = 14
}

###############################################################################
# Outputs
###############################################################################
output "eks_cluster_endpoint" {
  value = module.eks.cluster_endpoint
}

output "rds_endpoint" {
  value     = aws_db_instance.acre.endpoint
  sensitive = true
}

output "redis_endpoint" {
  value     = aws_elasticache_replication_group.acre.primary_endpoint_address
  sensitive = true
}

output "s3_bucket" {
  value = aws_s3_bucket.artifacts.id
}

output "ecr_urls" {
  value = { for k, v in aws_ecr_repository.acre : k => v.repository_url }
}
