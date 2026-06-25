# Public hosted zone for the apex domain. Created in Phase 1 so the nameservers
# are known and can be delegated at the registrar while the rest is built. The
# A/alias records for the viewer, api, and docs are added in Phase 2 (domains.tf),
# once the cert is issued.
resource "aws_route53_zone" "main" {
  count = var.domain_name == "" ? 0 : 1
  name  = var.domain_name
  tags  = local.tags
}
