variable "aws_region" {
  type    = string
  default = "eu-central-1"
}

variable "project_prefix" {
  type    = string
  default = "arena"
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
