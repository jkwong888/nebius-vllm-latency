variable "project_id" { type = string }
variable "network_id" { type = string }
variable "subnet_id" { type = string }
variable "ssh_public_key" { type = string }

variable "name" {
  type    = string
  default = "gemma-latency"
}

variable "boot_disk_size_gib" {
  description = "Network SSD boot disk size in GiB for the OS."
  type        = number
  default     = 100

  validation {
    condition     = var.boot_disk_size_gib > 0 && floor(var.boot_disk_size_gib) == var.boot_disk_size_gib
    error_message = "boot_disk_size_gib must be a positive whole number of GiB."
  }
}

variable "data_disk_size_gib" {
  description = "Non-replicated Network SSD size in GiB for model weights and container storage. Must be a multiple of 93."
  type        = number
  default     = 93 * 3

  validation {
    condition     = var.data_disk_size_gib > 0 && var.data_disk_size_gib % 93 == 0
    error_message = "data_disk_size_gib must be a positive multiple of 93 GiB."
  }
}

variable "platform" {
  description = "Nebius GPU platform. Choose a preset supported by this platform in the target region."
  type        = string
  default     = "gpu-b300-sxm"
}

variable "preset" {
  description = "VM resource preset supported by the selected platform."
  type        = string
  default     = "1gpu-24vcpu-346gb"
}
variable "image_id" {
  description = "Optional exact image ID from ubuntu24.04-cuda13.0 in uk-south1."
  type        = string
  default     = null
}
variable "spot_pricing_policy_id" {
  description = "Optional maximum-price policy. Null follows market spot price."
  type        = string
  default     = null
}
variable "k3s_version" {
  type    = string
  default = "v1.34.1+k3s1"
}
variable "agent_version" {
  type    = string
  default = "1.2.0"
}
