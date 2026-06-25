output "api_url" {
  description = "Base URL agents target (the HTTP API $default stage endpoint)."
  value       = aws_apigatewayv2_api.main.api_endpoint
}

output "viewer_url" {
  description = "Spectator viewer (CloudFront). Append ?api=<api_url> to point it at the API."
  value       = "https://${aws_cloudfront_distribution.frontend.domain_name}"
}

output "dynamodb_table" {
  value = aws_dynamodb_table.arena.name
}

output "lambda_function" {
  value = aws_lambda_function.api.function_name
}

output "frontend_bucket" {
  value = aws_s3_bucket.frontend.id
}

output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.frontend.id
}

output "route53_zone_id" {
  description = "Hosted zone id (empty if domain_name is unset)."
  value       = try(aws_route53_zone.main[0].zone_id, "")
}

output "nameservers" {
  description = "Set these as the domain's nameservers at the registrar to delegate DNS."
  value       = try(aws_route53_zone.main[0].name_servers, [])
}

output "docs_bucket" {
  value = try(aws_s3_bucket.docs[0].id, "")
}

output "docs_distribution_id" {
  value = try(aws_cloudfront_distribution.docs[0].id, "")
}

output "site_urls" {
  description = "Public custom-domain URLs once DNS + certs are live."
  value = local.has_domain ? {
    viewer = "https://${var.domain_name}"
    api    = "https://${local.api_domain}"
    docs   = "https://${local.docs_domain}"
  } : {}
}
