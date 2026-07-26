# The whole API runs in one FC 3.0 function: the FastAPI app behind an ASGI
# adapter. It scales to zero (no cost when nobody is playing) and scales out
# per concurrent request. Build the package first: scripts/package-fc.sh.

resource "alicloud_fcv3_function" "api" {
  function_name = "${local.prefix}-api"
  description   = "Ludicrous Arena: API-controlled multiplayer game server"
  runtime       = "python3.12"
  handler       = "arena.server.handler"
  memory_size   = var.function_memory
  timeout       = var.function_timeout
  role          = alicloud_ram_role.fc.arn

  code {
    oss_bucket_name = alicloud_oss_bucket.deploy.bucket
    oss_object_name = alicloud_oss_bucket_object.function_code.key
  }

  environment_variables = {
    ARENA_STORE  = "ots"
    ARENA_TABLE  = alicloud_ots_table.arena.table_name
    OTS_INSTANCE = alicloud_ots_instance.main.name
    OTS_ENDPOINT = "https://${local.ots_instance}.${var.region}.ots.aliyuncs.com"
    API_ENABLED  = tostring(var.api_enabled)
  }

  log_config {
    project  = alicloud_log_project.main.project_name
    logstore = alicloud_log_store.fc.logstore_name
  }
}

# HTTP trigger: the FC equivalent of API Gateway. Proxies every path to the
# single FastAPI function, which does its own routing.
resource "alicloud_fcv3_trigger" "http" {
  function_name = alicloud_fcv3_function.api.function_name
  trigger_name  = "http"
  trigger_type  = "http"
  qualifier     = "LATEST"

  trigger_config = jsonencode({
    authType = "anonymous"
    methods  = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
  })
}
