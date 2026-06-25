# Single-table store. In the serverless design this holds everything that must
# survive between requests: tokens, users, match metadata, AND the serialized live
# match state (there is no process to keep it in memory). See arena/store.py.
resource "aws_dynamodb_table" "arena" {
  name         = local.name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  tags = local.tags
}
