# Single-table store. In the serverless design this holds everything that must
# survive between requests: tokens, users, match metadata, AND the serialized
# live match state (there is no process to keep it in memory). See arena/store.py.
#
# Tablestore (OTS) replaces DynamoDB. The pk/sk composite key maps directly.
# Conditional version checks use OTS SingleColumnCondition (see OTSStore._put).

resource "alicloud_ots_instance" "main" {
  name          = local.ots_instance
  description   = "Ludicrous Arena serverless tables"
  instance_type = "HighPerformance"
  accessed_by   = "Any"
}

resource "alicloud_ots_table" "arena" {
  instance_name = alicloud_ots_instance.main.name
  table_name    = local.prefix

  primary_key {
    name = "pk"
    type = "String"
  }

  primary_key {
    name = "sk"
    type = "String"
  }

  # No table-level TTL: tokens/users must never expire, and match cleanup is
  # handled at the application level (the ttl attribute is still written for
  # future GC). Tablestore TTL is table-wide, so it cannot differentiate.
  time_to_live = -1
  max_version  = 1
}
