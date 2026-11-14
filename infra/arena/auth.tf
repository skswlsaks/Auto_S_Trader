# ---------- Cognito user pool (admin-created users only) ----------
resource "aws_cognito_user_pool" "arena" {
  name                     = "${var.name}-users"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  deletion_protection      = "ACTIVE"
  mfa_configuration        = "OPTIONAL"

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length                   = 8
    require_lowercase                = false
    require_uppercase                = false
    require_numbers                  = false
    require_symbols                  = false
    temporary_password_validity_days = 7
  }

  software_token_mfa_configuration {
    enabled = true
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_pool_domain" "arena" {
  domain       = "${var.name}-${substr(data.aws_caller_identity.current.account_id, 6, 6)}"
  user_pool_id = aws_cognito_user_pool.arena.id
}

resource "aws_cognito_user_pool_client" "web" {
  name                                 = "${var.name}-dashboard"
  user_pool_id                         = aws_cognito_user_pool.arena.id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email", "profile"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["https://${aws_cloudfront_distribution.site.domain_name}/_auth/callback"]
  logout_urls                          = ["https://${aws_cloudfront_distribution.site.domain_name}/_auth/signed-out"]
  explicit_auth_flows                  = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors        = "ENABLED"
  id_token_validity                    = 8
  access_token_validity                = 8
  refresh_token_validity               = 7
  token_validity_units {
    id_token      = "hours"
    access_token  = "hours"
    refresh_token = "days"
  }
}

# Lambda@Edge cannot use env vars; the function reads this at cold start. Breaks the
# CloudFront -> Lambda -> app client -> CloudFront dependency cycle.
resource "aws_ssm_parameter" "auth_config" {
  provider = aws.us_east_1
  name     = "/${var.name}/auth-config"
  type     = "String"
  value = jsonencode({
    region     = var.region
    userPoolId = aws_cognito_user_pool.arena.id
    clientId   = aws_cognito_user_pool_client.web.id
    domain     = "${aws_cognito_user_pool_domain.arena.domain}.auth.${var.region}.amazoncognito.com"
  })
}

# ---------- Lambda@Edge viewer-request auth ----------
data "archive_file" "auth" {
  type        = "zip"
  source_file = "${path.module}/edge/index.mjs"
  output_path = "${path.module}/.build/auth.zip"
}

resource "aws_iam_role" "edge" {
  name = "${var.name}-edge-auth"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = ["lambda.amazonaws.com", "edgelambda.amazonaws.com"] }
    }]
  })
}

resource "aws_iam_role_policy" "edge" {
  name = "edge-auth"
  role = aws_iam_role.edge.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = aws_ssm_parameter.auth_config.arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/us-east-1.${var.name}-edge-auth*"
      }
    ]
  })
}

resource "aws_lambda_function" "auth" {
  provider         = aws.us_east_1
  function_name    = "${var.name}-edge-auth"
  role             = aws_iam_role.edge.arn
  runtime          = "nodejs22.x"
  handler          = "index.handler"
  filename         = data.archive_file.auth.output_path
  source_code_hash = data.archive_file.auth.output_base64sha256
  memory_size      = 128
  timeout          = 5
  publish          = true
}
