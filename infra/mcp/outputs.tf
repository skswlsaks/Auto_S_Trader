output "ecr_repository_url" {
  value = aws_ecr_repository.mcp.repository_url
}

output "runtime_arn" {
  value = local.runtime_arn
}

output "mcp_url" {
  description = "Streamable HTTP endpoint for MCP clients (Authorization: Bearer <Cognito ID token>)"
  value       = local.runtime_arn == null ? null : "https://bedrock-agentcore.${var.region}.amazonaws.com/runtimes/${urlencode(local.runtime_arn)}/invocations?qualifier=DEFAULT"
}

output "mcp_client_id" {
  value = aws_cognito_user_pool_client.mcp.id
}

output "setup_string" {
  description = "Give to teammates: python3 tools/arena_mcp_proxy.py setup <this>"
  value = local.runtime_arn == null ? null : trimsuffix(replace(replace(base64encode(jsonencode({
    url       = "https://bedrock-agentcore.${var.region}.amazonaws.com/runtimes/${urlencode(local.runtime_arn)}/invocations?qualifier=DEFAULT"
    domain    = local.login_host
    client_id = aws_cognito_user_pool_client.mcp.id
  })), "+", "-"), "/", "_"), "=")
}
