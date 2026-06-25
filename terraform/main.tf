terraform {
  required_version = ">= 1.7"

  # Remote state. Create the bucket once, by hand, then uncomment. Until then
  # state is local (fine for a single operator bootstrapping the project).
  # backend "s3" {
  #   bucket  = "arena-tfstate-<account-id>"
  #   key     = "terraform/terraform.tfstate"
  #   region  = "eu-central-1"
  #   encrypt = true
  # }

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

resource "random_id" "suffix" {
  byte_length = 4
}

locals {
  name = var.project_prefix
  tags = {
    Project   = "ludicrous-arena"
    ManagedBy = "terraform"
  }
}
