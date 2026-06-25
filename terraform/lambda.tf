# The whole API runs in one Lambda: the FastAPI app behind Mangum. It scales to
# zero (no cost when nobody is playing) and scales out per concurrent request.
# Build the package first: scripts/package-lambda.sh.

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${local.name}-api"
  retention_in_days = 14
  tags              = local.tags
}

resource "aws_lambda_function" "api" {
  function_name    = "${local.name}-api"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.13"
  handler          = "arena.server.handler"
  filename         = var.lambda_zip
  source_code_hash = filebase64sha256(var.lambda_zip)
  memory_size      = var.lambda_memory
  timeout          = var.lambda_timeout
  # Pinned so it can never silently diverge from the wheel platform the package
  # is built for (scripts/package-lambda.sh uses x86_64-manylinux2014).
  architectures = ["x86_64"]

  environment {
    variables = {
      ARENA_STORE = "dynamo"
      ARENA_TABLE = aws_dynamodb_table.arena.name
    }
  }

  depends_on = [aws_cloudwatch_log_group.api]
  tags       = local.tags
}
