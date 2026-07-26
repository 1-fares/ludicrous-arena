# Deploy bucket: holds the FC function code zip. Terraform uploads the package
# here; the FC function references it by bucket + key.
resource "alicloud_oss_bucket" "deploy" {
  bucket = "${local.prefix}-deploy-${local.suffix}"
}

resource "alicloud_oss_bucket_object" "function_code" {
  bucket       = alicloud_oss_bucket.deploy.bucket
  key          = "code/${local.prefix}-api-${filebase64sha256(var.function_zip)}.zip"
  source       = var.function_zip
  content_type = "application/zip"
}
