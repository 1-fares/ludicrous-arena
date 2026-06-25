# TLS certificates, DNS-validated through the Route53 zone.
#  - cloudfront cert: us-east-1 (CloudFront requires it), covers the apex, www, and
#    docs names that CloudFront serves.
#  - api cert: eu-central-2, for the regional API Gateway custom domain.
# Both validate only once the registrar has delegated the domain to this zone.

resource "aws_acm_certificate" "cloudfront" {
  count                     = local.has_domain ? 1 : 0
  provider                  = aws.us_east_1
  domain_name               = var.domain_name
  subject_alternative_names = [local.www_domain, local.docs_domain]
  validation_method         = "DNS"
  lifecycle { create_before_destroy = true }
  tags = local.tags
}

resource "aws_acm_certificate" "api" {
  count             = local.has_domain ? 1 : 0
  domain_name       = local.api_domain
  validation_method = "DNS"
  lifecycle { create_before_destroy = true }
  tags = local.tags
}

# Validation records. The for_each keys are the domain names (static, known at plan
# time); only the record name/value come from the cert (apply time). This avoids the
# "for_each keys unknown until apply" problem.
resource "aws_route53_record" "cf_validation" {
  for_each = local.has_domain ? toset([var.domain_name, local.www_domain, local.docs_domain]) : toset([])

  zone_id = aws_route53_zone.main[0].zone_id
  name    = one([for dvo in aws_acm_certificate.cloudfront[0].domain_validation_options : dvo.resource_record_name if dvo.domain_name == each.key])
  type    = one([for dvo in aws_acm_certificate.cloudfront[0].domain_validation_options : dvo.resource_record_type if dvo.domain_name == each.key])
  records = [one([for dvo in aws_acm_certificate.cloudfront[0].domain_validation_options : dvo.resource_record_value if dvo.domain_name == each.key])]
  ttl     = 60

  allow_overwrite = true
}

resource "aws_route53_record" "api_validation" {
  for_each = local.has_domain ? toset([local.api_domain]) : toset([])

  zone_id = aws_route53_zone.main[0].zone_id
  name    = one([for dvo in aws_acm_certificate.api[0].domain_validation_options : dvo.resource_record_name if dvo.domain_name == each.key])
  type    = one([for dvo in aws_acm_certificate.api[0].domain_validation_options : dvo.resource_record_type if dvo.domain_name == each.key])
  records = [one([for dvo in aws_acm_certificate.api[0].domain_validation_options : dvo.resource_record_value if dvo.domain_name == each.key])]
  ttl     = 60

  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "cloudfront" {
  count                   = local.has_domain ? 1 : 0
  provider                = aws.us_east_1
  certificate_arn         = aws_acm_certificate.cloudfront[0].arn
  validation_record_fqdns = [for r in aws_route53_record.cf_validation : r.fqdn]
}

resource "aws_acm_certificate_validation" "api" {
  count                   = local.has_domain ? 1 : 0
  certificate_arn         = aws_acm_certificate.api[0].arn
  validation_record_fqdns = [for r in aws_route53_record.api_validation : r.fqdn]
}
