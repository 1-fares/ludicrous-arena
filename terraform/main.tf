terraform {
  required_version = ">= 1.7"

  # Remote state in a private, versioned bucket in Switzerland (eu-central-2),
  # bootstrapped once by hand (see scripts/bootstrap-state.sh).
  backend "s3" {
    bucket  = "arena-tfstate-ACCOUNT_ID"
    key     = "terraform/terraform.tfstate"
    region  = "eu-central-2"
    encrypt = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.82"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# CloudFront certs must be requested in us-east-1, regardless of where everything
# else runs. Used only for the viewer/docs distribution certificate.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
}

resource "random_id" "suffix" {
  byte_length = 4
}

locals {
  name = var.project_prefix
  tags = {
    Project   = "ludicrous-arena"
    ManagedBy = "terraform"
  }

  # Domain wiring (Phase 2) is on only when domain_name is set.
  has_domain  = var.domain_name != ""
  www_domain  = "www.${var.domain_name}"
  api_domain  = "api.${var.domain_name}"
  docs_domain = "docs.${var.domain_name}"
  # CloudFront's fixed alias hosted-zone id (same for every distribution).
  cloudfront_zone_id = "Z2FDTNDATAQYW2"
}
