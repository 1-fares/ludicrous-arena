# FC execution role. The function assumes this role to access Tablestore and SLS.
data "alicloud_account" "current" {}

resource "alicloud_ram_role" "fc" {
  role_name   = "${local.prefix}-fc-role"
  description = "Allows Function Compute to access Tablestore and SLS"
  assume_role_policy_document = jsonencode({
    Version = "1"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = ["fc.aliyuncs.com"]
      }
    }]
  })
  force = true
}

resource "alicloud_ram_policy" "fc_ots" {
  policy_name = "${local.prefix}-fc-ots-access"
  policy_document = jsonencode({
    Version = "1"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ots:Get*",
        "ots:List*",
        "ots:BatchGet*",
        "ots:Describe*",
        "ots:PutRow",
        "ots:UpdateRow",
        "ots:DeleteRow",
        "ots:BatchWrite*",
        "ots:GetRange",
      ]
      Resource = [
        "acs:ots:${var.region}:${data.alicloud_account.current.id}:instance/${local.ots_instance}*",
      ]
    }]
  })
}

resource "alicloud_ram_policy" "fc_sls" {
  policy_name = "${local.prefix}-fc-sls-access"
  policy_document = jsonencode({
    Version = "1"
    Statement = [{
      Effect = "Allow"
      Action = [
        "log:PostLogStoreLogs",
        "log:CreateLogStore",
        "log:GetLogStore",
      ]
      Resource = [
        "acs:log:${var.region}:${data.alicloud_account.current.id}:project/${local.prefix}-logs*",
      ]
    }]
  })
}

resource "alicloud_ram_role_policy_attachment" "fc_ots" {
  role_name   = alicloud_ram_role.fc.role_name
  policy_name = alicloud_ram_policy.fc_ots.policy_name
  policy_type = "Custom"
}

resource "alicloud_ram_role_policy_attachment" "fc_sls" {
  role_name   = alicloud_ram_role.fc.role_name
  policy_name = alicloud_ram_policy.fc_sls.policy_name
  policy_type = "Custom"
}
