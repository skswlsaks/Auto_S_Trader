data "aws_caller_identity" "current" {}

locals {
  bucket = "${var.name}-${data.aws_caller_identity.current.account_id}-${var.region}"
}

# ---------- shared run store + dashboard bucket (private) ----------
resource "aws_s3_bucket" "arena" {
  bucket        = local.bucket
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "arena" {
  bucket                  = aws_s3_bucket.arena.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "arena" {
  bucket = aws_s3_bucket.arena.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Versioning keeps every submitted run recoverable even if someone overwrites a key.
resource "aws_s3_bucket_versioning" "arena" {
  bucket = aws_s3_bucket.arena.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "arena" {
  bucket = aws_s3_bucket.arena.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "arena" {
  bucket = aws_s3_bucket.arena.id
  rule {
    id     = "expire-old-site-versions"
    status = "Enabled"
    filter {
      prefix = "site/"
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

resource "aws_s3_bucket_policy" "arena" {
  bucket     = aws_s3_bucket.arena.id
  depends_on = [aws_s3_bucket_public_access_block.arena]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.arena.arn, "${aws_s3_bucket.arena.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "CloudFrontReadsSiteOnly"
        Effect    = "Allow"
        Principal = { Service = "cloudfront.amazonaws.com" }
        Action    = "s3:GetObject"
        Resource  = "${aws_s3_bucket.arena.arn}/site/*"
        Condition = { StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.site.arn } }
      }
    ]
  })
}

# ---------- dashboard: CloudFront (OAC) + Cognito login at the edge ----------
resource "aws_cloudfront_origin_access_control" "site" {
  name                              = "${var.name}-oac"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

# Short edge cache so CloudFront can compress (gzip/brotli need a caching policy). Auth still runs on
# every request in the viewer-request Lambda@Edge, before the cache. New data shows within a minute.
resource "aws_cloudfront_cache_policy" "site" {
  name        = "${var.name}-site"
  min_ttl     = 0
  default_ttl = 60
  max_ttl     = 300
  parameters_in_cache_key_and_forwarded_to_origin {
    enable_accept_encoding_gzip   = true
    enable_accept_encoding_brotli = true
    cookies_config {
      cookie_behavior = "none"
    }
    headers_config {
      header_behavior = "none"
    }
    query_strings_config {
      query_string_behavior = "none"
    }
  }
}

resource "aws_cloudfront_distribution" "site" {
  enabled             = true
  comment             = "s_trader Model Arena dashboard"
  default_root_object = "index.html"
  price_class         = var.price_class
  http_version        = "http2and3"

  origin {
    origin_id                = "arena-site"
    domain_name              = aws_s3_bucket.arena.bucket_regional_domain_name
    origin_path              = "/site"
    origin_access_control_id = aws_cloudfront_origin_access_control.site.id
  }

  default_cache_behavior {
    target_origin_id       = "arena-site"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    compress               = true
    cache_policy_id        = aws_cloudfront_cache_policy.site.id

    lambda_function_association {
      event_type   = "viewer-request"
      lambda_arn   = aws_lambda_function.auth.qualified_arn
      include_body = false
    }
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }
}

# ---------- team access: submit runs, pull, publish dashboard ----------
resource "aws_iam_group" "team" {
  name = "${var.name}-team"
}

resource "aws_iam_group_policy" "team" {
  name  = "arena-store"
  group = aws_iam_group.team.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.arena.arn
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = ["${aws_s3_bucket.arena.arn}/runs/*", "${aws_s3_bucket.arena.arn}/site/*", "${aws_s3_bucket.arena.arn}/studies/*", "${aws_s3_bucket.arena.arn}/conditions/*"]
      }
    ]
  })
}

resource "aws_iam_user_group_membership" "team" {
  for_each = toset(var.team_users)
  user     = each.value
  groups   = [aws_iam_group.team.name]
}
