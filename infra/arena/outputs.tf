output "arena_remote" {
  description = "Set as ARENA_REMOTE for arena submit/pull/publish"
  value       = "s3://${aws_s3_bucket.arena.bucket}"
}

output "dashboard_url" {
  value = "https://${aws_cloudfront_distribution.site.domain_name}"
}

output "basic_auth_user" {
  value = var.basic_auth_user
}

output "basic_auth_password" {
  value     = random_password.basic_auth.result
  sensitive = true
}

output "team_group" {
  description = "Add teammates' IAM users here (var.team_users) to grant store access"
  value       = aws_iam_group.team.name
}
