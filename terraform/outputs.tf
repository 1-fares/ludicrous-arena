output "fc_url" {
  description = "Function Compute HTTP trigger URL (the API base agents target)."
  value       = alicloud_fcv3_trigger.http.http_trigger[0].url_internet
}

output "ots_instance" {
  description = "Tablestore instance name."
  value       = alicloud_ots_instance.main.name
}

output "ots_table" {
  description = "Tablestore table name (pass as ARENA_TABLE to scripts)."
  value       = alicloud_ots_table.arena.table_name
}

output "deploy_bucket" {
  description = "OSS bucket holding the FC function code zip."
  value       = alicloud_oss_bucket.deploy.bucket
}

output "sls_project" {
  description = "SLS log project name."
  value       = alicloud_log_project.main.project_name
}

output "ram_role_arn" {
  description = "FC service role ARN."
  value       = alicloud_ram_role.fc.arn
}

output "site_urls" {
  description = "Public custom-domain URLs once DNS + certs are live."
  value = local.has_domain ? {
    viewer = "https://${var.domain_name}"
    api    = "https://${local.api_domain}"
  } : {}
}
