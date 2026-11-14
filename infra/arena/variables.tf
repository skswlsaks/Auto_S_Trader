variable "region" {
  description = "Region of the Arena run/site bucket"
  type        = string
  default     = "ap-northeast-2"
}

variable "aws_profile" {
  description = "Local AWS CLI profile used by Terraform (null in CI, which uses OIDC credentials)"
  type        = string
  default     = null
}

variable "name" {
  description = "Resource name prefix"
  type        = string
  default     = "s-trader-arena"
}

variable "team_users" {
  description = "Existing IAM user names to add to the Arena team group"
  type        = list(string)
  default     = []
}

variable "price_class" {
  description = "CloudFront price class (PriceClass_200 includes Korea/Japan edges)"
  type        = string
  default     = "PriceClass_200"
}
