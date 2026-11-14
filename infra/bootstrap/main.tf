# One-time foundation for CI deploys: remote Terraform state + GitHub OIDC deploy role.
# Apply manually once by an account admin (see README). CI never modifies this stack.
terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.80" }
  }
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile
  default_tags {
    tags = { Project = "s-trader", Component = "bootstrap", ManagedBy = "terraform" }
  }
}

variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "aws_profile" {
  type    = string
  default = "default"
}

variable "github_sub_prefix" {
  description = "OIDC sub prefix of the repo. This repo uses GitHub immutable subjects (owner@id/repo@id); check with: gh api repos/OWNER/REPO/actions/oidc/customization/sub"
  type        = string
  default     = "repo:skswlsaks@13283962/Auto_S_Trader@1438464209"
}

variable "deploy_branch" {
  type    = string
  default = "main"
}

data "aws_caller_identity" "current" {}

locals {
  account     = data.aws_caller_identity.current.account_id
  state_name  = "s-trader-tfstate-${local.account}-${var.region}"
  arena       = "s-trader-arena"
  arena_arn_b = "arn:aws:s3:::${local.arena}-${local.account}-${var.region}"
}

# ---------- remote state ----------
resource "aws_s3_bucket" "state" {
  bucket = local.state_name
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket     = aws_s3_bucket.state.id
  depends_on = [aws_s3_bucket_public_access_block.state]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
}

# ---------- GitHub Actions OIDC ----------
resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"] # AWS validates GitHub via its own trust store; kept for API compatibility
}

resource "aws_iam_role" "deploy" {
  name = "${local.arena}-github-deploy"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "${var.github_sub_prefix}:ref:refs/heads/${var.deploy_branch}"
        }
      }
    }]
  })
  max_session_duration = 3600
}

# Scoped to what infra/arena manages (names prefixed s-trader-arena) plus its state object.
resource "aws_iam_role_policy" "deploy" {
  name = "arena-deploy"
  role = aws_iam_role.deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "State"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/arena/*"]
      },
      {
        Sid      = "ArenaBucket"
        Effect   = "Allow"
        Action   = ["s3:*"]
        Resource = [local.arena_arn_b, "${local.arena_arn_b}/*"]
      },
      {
        Sid    = "EdgeAndAuth"
        Effect = "Allow"
        Action = ["cloudfront:*", "cognito-idp:*", "lambda:*", "logs:*",
        "ssm:GetParameter", "ssm:GetParameters", "ssm:PutParameter", "ssm:DeleteParameter", "ssm:DescribeParameters", "ssm:AddTagsToResource", "ssm:ListTagsForResource", "ssm:RemoveTagsFromResource"]
        Resource = "*"
      },
      {
        Sid    = "ArenaIam"
        Effect = "Allow"
        Action = ["iam:*Role*", "iam:*Group*", "iam:PassRole", "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole", "iam:ListEntitiesForPolicy"]
        Resource = [
          "arn:aws:iam::${local.account}:role/${local.arena}-*",
          "arn:aws:iam::${local.account}:group/${local.arena}-*",
        ]
      },
      {
        Sid       = "EdgeReplicator"
        Effect    = "Allow"
        Action    = ["iam:CreateServiceLinkedRole"]
        Resource  = "*"
        Condition = { StringEquals = { "iam:AWSServiceName" = ["replicator.lambda.amazonaws.com", "logger.cloudfront.amazonaws.com"] } }
      },
      {
        Sid      = "McpRegistry"
        Effect   = "Allow"
        Action   = ["ecr:*"]
        Resource = "arn:aws:ecr:${var.region}:${local.account}:repository/${local.arena}-*"
      },
      {
        Sid      = "McpRegistryLogin"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid      = "McpRuntime"
        Effect   = "Allow"
        Action   = ["bedrock-agentcore:*"]
        Resource = "*"
      },
      {
        Sid       = "McpServiceLinkedRoles"
        Effect    = "Allow"
        Action    = ["iam:CreateServiceLinkedRole"]
        Resource  = "*"
        Condition = { StringLike = { "iam:AWSServiceName" = ["*bedrock-agentcore*"] } }
      },
      {
        Sid      = "Identity"
        Effect   = "Allow"
        Action   = ["sts:GetCallerIdentity"]
        Resource = "*"
      }
    ]
  })
}

output "state_bucket" {
  value = aws_s3_bucket.state.bucket
}

output "deploy_role_arn" {
  value = aws_iam_role.deploy.arn
}
