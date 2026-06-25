variable "aws_region" {
  # eu-central-2 (Zurich): all compute and data live in Switzerland. The only
  # us-east-1 resource is the CloudFront cert (a public cert, required there), via
  # the aws.us_east_1 provider alias.
  type    = string
  default = "eu-central-2"
}

variable "project_prefix" {
  type    = string
  default = "arena"
}

variable "domain_name" {
  description = "Apex domain. Empty leaves all custom-domain/ACM/Route53 wiring (Phase 2) off."
  type        = string
  default     = ""
}

variable "alert_email" {
  description = "Email for the CloudWatch billing alarm. Empty disables the alarm."
  type        = string
  default     = ""
}

variable "lambda_zip" {
  description = "Path to the built Lambda deployment package. Build it with scripts/package-lambda.sh before the first apply."
  type        = string
  default     = "../backend/build/lambda.zip"
}

variable "lambda_memory" {
  description = "Lambda memory (MB). CPU scales with memory; 512 is ample for the simulation."
  type        = number
  default     = 512
}

variable "lambda_timeout" {
  description = "Lambda timeout (seconds). Bounds a single catch-up; matches' time limits keep work well under this."
  type        = number
  default     = 15
}
