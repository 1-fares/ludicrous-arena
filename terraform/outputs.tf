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
