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
  description = "Lambda timeout (seconds). Bounds a single catch-up. The engine caps catch-up at _MAX_CATCHUP ticks; a full cap of the heaviest game (skirmish) must simulate well under this, with margin for cold start, decode, and DynamoDB IO."
  type        = number
  default     = 30
}

variable "api_enabled" {
  description = <<-DESC
    Master on/off switch for the public API. When false, the Lambda's reserved
    concurrency is pinned to 0, so API Gateway cannot invoke it: every request
    (including wrong-token and random internet probes) is throttled at the edge
    and runs no code and touches no DynamoDB, keeping cost near zero while the
    arena is idle. Set to true and re-apply to re-enable play. The committed
    value lives in api-switch.auto.tfvars.
  DESC
  type    = bool
  default = true
}
