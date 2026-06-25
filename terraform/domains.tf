# Custom-domain wiring (Phase 2). api.* is an API Gateway regional custom domain in
# Switzerland; the apex and www point at the viewer CloudFront distribution.

resource "aws_apigatewayv2_domain_name" "api" {
  count       = local.has_domain ? 1 : 0
  domain_name = local.api_domain

  domain_name_configuration {
    certificate_arn = aws_acm_certificate_validation.api[0].certificate_arn
    endpoint_type   = "REGIONAL"
    security_policy = "TLS_1_2"
  }

  tags = local.tags
}

resource "aws_apigatewayv2_api_mapping" "api" {
  count       = local.has_domain ? 1 : 0
  api_id      = aws_apigatewayv2_api.main.id
  domain_name = aws_apigatewayv2_domain_name.api[0].id
  stage       = aws_apigatewayv2_stage.default.id
}

# api.* -> the regional API Gateway domain target.
resource "aws_route53_record" "api_a" {
  count   = local.has_domain ? 1 : 0
  zone_id = aws_route53_zone.main[0].zone_id
  name    = local.api_domain
  type    = "A"
  alias {
    name                   = aws_apigatewayv2_domain_name.api[0].domain_name_configuration[0].target_domain_name
    zone_id                = aws_apigatewayv2_domain_name.api[0].domain_name_configuration[0].hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "api_aaaa" {
  count   = local.has_domain ? 1 : 0
  zone_id = aws_route53_zone.main[0].zone_id
  name    = local.api_domain
  type    = "AAAA"
  alias {
    name                   = aws_apigatewayv2_domain_name.api[0].domain_name_configuration[0].target_domain_name
    zone_id                = aws_apigatewayv2_domain_name.api[0].domain_name_configuration[0].hosted_zone_id
    evaluate_target_health = false
  }
}

# apex + www -> the viewer CloudFront distribution.
resource "aws_route53_record" "viewer_a" {
  for_each = local.has_domain ? toset([var.domain_name, local.www_domain]) : []
  zone_id  = aws_route53_zone.main[0].zone_id
  name     = each.value
  type     = "A"
  alias {
    name                   = aws_cloudfront_distribution.frontend.domain_name
    zone_id                = local.cloudfront_zone_id
    evaluate_target_health = false
  }
}

resource "aws_route53_record" "viewer_aaaa" {
  for_each = local.has_domain ? toset([var.domain_name, local.www_domain]) : []
  zone_id  = aws_route53_zone.main[0].zone_id
  name     = each.value
  type     = "AAAA"
  alias {
    name                   = aws_cloudfront_distribution.frontend.domain_name
    zone_id                = local.cloudfront_zone_id
    evaluate_target_health = false
  }
}
