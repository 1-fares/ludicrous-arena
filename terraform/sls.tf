# Log Service (SLS) replaces CloudWatch Logs. FC writes function logs here.
resource "alicloud_log_project" "main" {
  project_name = "${local.prefix}-logs-${local.suffix}"
  description  = "Ludicrous Arena application logs"
}

resource "alicloud_log_store" "fc" {
  project_name          = alicloud_log_project.main.project_name
  logstore_name         = "${local.prefix}-fc"
  shard_count           = 1
  auto_split            = true
  max_split_shard_count = 4
  retention_period      = 14
}
