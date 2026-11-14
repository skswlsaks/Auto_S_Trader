data "aws_caller_identity" "current" {}

# infra/arena outputs: Cognito user pool, login domain, shared store
data "terraform_remote_state" "arena" {
  backend = "s3"
  config = {
    bucket = var.state_bucket
    key    = "arena/terraform.tfstate"
    region = var.region
  }
}

locals {
  account     = data.aws_caller_identity.current.account_id
  pool_id     = data.terraform_remote_state.arena.outputs.cognito_user_pool_id
  login_host  = trimprefix(data.terraform_remote_state.arena.outputs.login_domain, "https://")
  store       = data.terraform_remote_state.arena.outputs.arena_remote
  bucket      = trimprefix(local.store, "s3://")
  bucket_arn  = "arn:aws:s3:::${local.bucket}"
  runtime_arn = one(aws_bedrockagentcore_agent_runtime.mcp[*].agent_runtime_arn)
}

# ---------- container registry ----------
resource "aws_ecr_repository" "mcp" {
  name                 = "${var.name}-mcp"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "mcp" {
  repository = aws_ecr_repository.mcp.name
  policy     = jsonencode({ rules = [{ rulePriority = 1, description = "keep 20 images", selection = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 }, action = { type = "expire" } }] })
}

# ---------- Cognito client for AI tools (browser login, PKCE, localhost callback) ----------
resource "aws_cognito_user_pool_client" "mcp" {
  name                                 = "${var.name}-mcp"
  user_pool_id                         = local.pool_id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["http://localhost:8976/callback"]
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH", "ALLOW_USER_SRP_AUTH"]
  prevent_user_existence_errors        = "ENABLED"
  id_token_validity                    = 1
  access_token_validity                = 1
  refresh_token_validity               = 30
  token_validity_units {
    id_token      = "hours"
    access_token  = "hours"
    refresh_token = "days"
  }
}

# ---------- runtime execution role ----------
resource "aws_iam_role" "runtime" {
  name = "${var.name}-mcp-runtime"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account }
        ArnLike      = { "aws:SourceArn" = "arn:aws:bedrock-agentcore:${var.region}:${local.account}:*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "runtime" {
  name = "mcp-runtime"
  role = aws_iam_role.runtime.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
      { Effect = "Allow", Action = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"], Resource = aws_ecr_repository.mcp.arn },
      { Effect = "Allow", Action = ["logs:DescribeLogGroups"], Resource = "arn:aws:logs:${var.region}:${local.account}:log-group:*" },
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:DescribeLogStreams"], Resource = "arn:aws:logs:${var.region}:${local.account}:log-group:/aws/bedrock-agentcore/runtimes/*" },
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "arn:aws:logs:${var.region}:${local.account}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*" },
      { Effect = "Allow", Action = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"], Resource = "*" },
      { Effect = "Allow", Action = ["cloudwatch:PutMetricData"], Resource = "*", Condition = { StringEquals = { "cloudwatch:namespace" = "bedrock-agentcore" } } },
      { Effect = "Allow", Action = ["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT", "bedrock-agentcore:GetWorkloadAccessTokenForUserId"],
      Resource = ["arn:aws:bedrock-agentcore:${var.region}:${local.account}:workload-identity-directory/default", "arn:aws:bedrock-agentcore:${var.region}:${local.account}:workload-identity-directory/default/workload-identity/*"] },
      # shared store: read the built dashboard, read/write runs, studies and per-user drafts (no delete)
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = local.bucket_arn },
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = ["${local.bucket_arn}/site/*", "${local.bucket_arn}/runs/*", "${local.bucket_arn}/studies/*", "${local.bucket_arn}/drafts/*", "${local.bucket_arn}/conditions/*"] },
      { Effect = "Allow", Action = ["s3:PutObject"], Resource = ["${local.bucket_arn}/runs/*", "${local.bucket_arn}/studies/*", "${local.bucket_arn}/drafts/*"] },
    ]
  })
}

# ---------- the hosted MCP server ----------
resource "aws_bedrockagentcore_agent_runtime" "mcp" {
  count              = var.image_tag == "" ? 0 : 1
  agent_runtime_name = replace("${var.name}_mcp", "-", "_")
  description        = "Model Arena MCP server (team results: read, combine, submit)"
  role_arn           = aws_iam_role.runtime.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${aws_ecr_repository.mcp.repository_url}:${var.image_tag}"
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  protocol_configuration {
    server_protocol = "MCP"
  }

  authorizer_configuration {
    custom_jwt_authorizer {
      discovery_url    = "https://cognito-idp.${var.region}.amazonaws.com/${local.pool_id}/.well-known/openid-configuration"
      allowed_audience = [aws_cognito_user_pool_client.mcp.id]
    }
  }

  request_header_configuration {
    request_header_allowlist = ["Authorization"]
  }

  environment_variables = {
    ARENA_MODE      = "remote"
    ARENA_REMOTE    = local.store
    ARENA_OUTPUTS   = "/tmp/arena"
    ARENA_S3_CLIENT = "boto3"
  }

  depends_on = [aws_iam_role_policy.runtime]
}
