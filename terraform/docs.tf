# Static docs site at docs.* : its own private S3 bucket behind its own CloudFront
# distribution, sharing the cloudfront cert (which includes the docs name) and the
# S3 origin-access-control. Content is rendered from the markdown docs by
# scripts/build-docs.sh and synced with scripts/deploy-docs.sh.

resource "aws_s3_bucket" "docs" {
  count  = local.has_domain ? 1 : 0
  bucket = "${local.name}-docs-${random_id.suffix.hex}"
  tags   = local.tags
}

resource "aws_s3_bucket_public_access_block" "docs" {
  count                   = local.has_domain ? 1 : 0
  bucket                  = aws_s3_bucket.docs[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_cloudfront_distribution" "docs" {
  count               = local.has_domain ? 1 : 0
  enabled             = true
  default_root_object = "index.html"
  comment             = "${local.name} docs site"
  price_class         = "PriceClass_100"
  aliases             = [local.docs_domain]

  origin {
    domain_name              = aws_s3_bucket.docs[0].bucket_regional_domain_name
    origin_id                = "docs"
    origin_access_control_id = aws_cloudfront_origin_access_control.s3.id
  }

  default_cache_behavior {
    target_origin_id       = "docs"
    viewer_protocol_policy = "redirect-to-https"
    allowed_methods        = ["GET", "HEAD", "OPTIONS"]
    cached_methods         = ["GET", "HEAD"]

    forwarded_values {
      query_string = false
      cookies {
        forward = "none"
      }
    }

    min_ttl     = 0
    default_ttl = 300
    max_ttl     = 3600
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate_validation.cloudfront[0].certificate_arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }

  tags = local.tags
}

resource "aws_s3_bucket_policy" "docs" {
  count  = local.has_domain ? 1 : 0
  bucket = aws_s3_bucket.docs[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowCloudFront"
      Effect    = "Allow"
      Principal = { Service = "cloudfront.amazonaws.com" }
      Action    = "s3:GetObject"
      Resource  = "${aws_s3_bucket.docs[0].arn}/*"
      Condition = {
        StringEquals = { "AWS:SourceArn" = aws_cloudfront_distribution.docs[0].arn }
      }
    }]
  })
}

resource "aws_route53_record" "docs_a" {
  count   = local.has_domain ? 1 : 0
  zone_id = aws_route53_zone.main[0].zone_id
  name    = local.docs_domain
  type    = "A"
  alias {
    name                   = aws_cloudfront_distribution.docs[0].domain_name
    zone_id                = local.cloudfront_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "docs_aaaa" {
  count   = local.has_domain ? 1 : 0
  zone_id = aws_route53_zone.main[0].zone_id
  name    = local.docs_domain
  type    = "AAAA"
  alias {
    name                   = aws_cloudfront_distribution.docs[0].domain_name
    zone_id                = local.cloudfront_zone_id
    evaluate_target_health = false
  }
}
