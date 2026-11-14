terraform {
  required_version = ">= 1.7"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.80" }
    archive = { source = "hashicorp/archive", version = "~> 2.7" }
    http    = { source = "hashicorp/http", version = "~> 3.4" }
  }
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile
  default_tags {
    tags = { Project = "s-trader", Component = "model-arena", ManagedBy = "terraform" }
  }
}

provider "aws" {
  alias   = "us_east_1"
  region  = "us-east-1"
  profile = var.aws_profile
  default_tags {
    tags = { Project = "s-trader", Component = "model-arena", ManagedBy = "terraform" }
  }
}
