# Lambda execution role. Logs via the managed basic-execution policy; the only
# extra grant is access to the one DynamoDB table the arena uses.

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${local.name}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "lambda_dynamo" {
  # Exactly the actions the store uses (GetItem, PutItem with conditions, Query
  # for the match index). No UpdateItem/DeleteItem, no GSI, so neither is granted.
  statement {
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:Query",
    ]
    resources = [aws_dynamodb_table.arena.arn]
  }
}

resource "aws_iam_role_policy" "lambda_dynamo" {
  name   = "${local.name}-lambda-dynamo"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda_dynamo.json
}
