# Custom domains (Phase 2). The FC function serves both the API and the bundled
# spectator viewer, so both the apex and api.* route to the same function.
#
# Prerequisites (manual, at the registrar / DNS provider):
#   1. CNAME  ludicrous-arena.com      -> <account-id>.<region>.fc.aliyuncs.com
#   2. CNAME  api.ludicrous-arena.com  -> <account-id>.<region>.fc.aliyuncs.com
#   3. Issue a SAN cert covering both names (acme.sh, RSA 2048+, not ECC).
#   4. Set cert_path / key_path in terraform.tfvars and re-apply.
#
# FC rejects ECC/ECDSA private keys — use RSA 2048+.

resource "alicloud_fcv3_custom_domain" "viewer" {
  count              = local.has_domain ? 1 : 0
  custom_domain_name = var.domain_name
  protocol           = var.cert_path != "" ? "HTTP,HTTPS" : "HTTP"

  dynamic "cert_config" {
    for_each = var.cert_path != "" ? [1] : []
    content {
      cert_name   = replace(var.domain_name, ".", "-")
      certificate = file(var.cert_path)
      private_key = file(var.key_path)
    }
  }

  route_config {
    routes {
      function_name = alicloud_fcv3_function.api.function_name
      path          = "/*"
      qualifier     = "LATEST"
      methods       = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
    }
  }
}

resource "alicloud_fcv3_custom_domain" "api" {
  count              = local.has_domain ? 1 : 0
  custom_domain_name = local.api_domain
  protocol           = var.cert_path != "" ? "HTTP,HTTPS" : "HTTP"

  dynamic "cert_config" {
    for_each = var.cert_path != "" ? [1] : []
    content {
      cert_name   = replace(local.api_domain, ".", "-")
      certificate = file(var.cert_path)
      private_key = file(var.key_path)
    }
  }

  route_config {
    routes {
      function_name = alicloud_fcv3_function.api.function_name
      path          = "/*"
      qualifier     = "LATEST"
      methods       = ["GET", "POST", "PUT", "DELETE", "OPTIONS"]
    }
  }
}
