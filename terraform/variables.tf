variable "region" {
  description = "Aliyun region for all compute and data."
  type        = string
  default     = "ap-southeast-1"
}

variable "project_prefix" {
  type    = string
  default = "arena"
}

variable "domain_name" {
  description = "Apex domain (e.g. ludicrous-arena.com). Empty leaves custom-domain wiring (Phase 2) off."
  type        = string
  default     = ""
}

variable "cert_path" {
  description = "Path to the fullchain PEM certificate for the custom domains (RSA, not ECC)."
  type        = string
  default     = ""
}

variable "key_path" {
  description = "Path to the PEM private key (PKCS#8, RSA) matching cert_path."
  type        = string
  default     = ""
}

variable "function_zip" {
  description = "Path to the built FC deployment package. Build it with scripts/package-fc.sh before the first apply."
  type        = string
  default     = "../backend/build/fc.zip"
}

variable "function_memory" {
  description = "FC function memory (MB). CPU scales with memory; 512 is ample for the simulation."
  type        = number
  default     = 512
}

variable "function_timeout" {
  description = "FC function timeout (seconds). Bounds a single catch-up."
  type        = number
  default     = 60
}

variable "api_enabled" {
  description = <<-DESC
    Master on/off switch for the public API. When false, the FC function's
    environment carries API_ENABLED=false and the handler returns 503 for every
    API call, so abusive/wrong-token traffic runs no game code and touches no
    Tablestore, keeping cost near zero while the arena is idle. Set to true and
    re-apply to re-enable play. The committed value lives in api-switch.auto.tfvars.
  DESC
  type    = bool
  default = true
}
