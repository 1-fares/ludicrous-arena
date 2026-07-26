terraform {
  required_version = ">= 1.7"

  # Remote state in a private, versioned OSS bucket (bootstrapped once by hand,
  # see scripts/bootstrap-state.sh).
  backend "oss" {
    bucket = "arena-tfstate-aliyun"
    key    = "terraform/terraform.tfstate"
    region = "ap-southeast-1"
  }

  required_providers {
    alicloud = {
      source  = "aliyun/alicloud"
      version = "~> 1.230"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "alicloud" {
  region = var.region
}

resource "random_id" "suffix" {
  byte_length = 4
}

locals {
  prefix       = var.project_prefix
  suffix       = random_id.suffix.hex
  ots_instance = "${local.prefix}-${local.suffix}"

  # Domain wiring (Phase 2) is on only when domain_name is set.
  has_domain = var.domain_name != ""
  api_domain = "api.${var.domain_name}"
}
