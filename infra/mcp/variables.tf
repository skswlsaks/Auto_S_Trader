variable "region" {
  type    = string
  default = "ap-northeast-2"
}

variable "aws_profile" {
  description = "Local AWS CLI profile (null in CI, which uses OIDC credentials)"
  type        = string
  default     = null
}

variable "name" {
  type    = string
  default = "s-trader-arena"
}

variable "image_tag" {
  description = "Tag of the MCP server image in ECR (CI passes the git SHA). Empty = create only the registry, roles and Cognito client."
  type        = string
  default     = ""
}

variable "state_bucket" {
  description = "Terraform state bucket (same as infra/arena)"
  type        = string
}
