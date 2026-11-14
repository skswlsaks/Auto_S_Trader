output "arena_remote" {
  description = "Set as ARENA_REMOTE for arena submit/pull/publish"
  value       = "s3://${aws_s3_bucket.arena.bucket}"
}

output "dashboard_url" {
  value = "https://${aws_cloudfront_distribution.site.domain_name}"
}

output "team_group" {
  description = "Add teammates' IAM users here (var.team_users) to grant store access"
  value       = aws_iam_group.team.name
}

output "cognito_user_pool_id" {
  value = aws_cognito_user_pool.arena.id
}

output "login_domain" {
  value = "https://${aws_cognito_user_pool_domain.arena.domain}.auth.${var.region}.amazoncognito.com"
}
