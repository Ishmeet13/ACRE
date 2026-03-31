variable "project_name" {
  description = "Project prefix for all resources"
  type        = string
  default     = "acre"
}

variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "us-east-1"
}

variable "aws_account_id" {
  description = "AWS account ID (used for globally unique S3 bucket names)"
  type        = string
}

variable "db_password" {
  description = "RDS master password"
  type        = string
  sensitive   = true
}

variable "tags" {
  description = "Common resource tags"
  type        = map(string)
  default = {
    Project     = "ACRE"
    ManagedBy   = "Terraform"
    Environment = "production"
  }
}
