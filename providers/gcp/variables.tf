variable "name" {
  type = string
}

variable "size" {
  type = string
}

variable "region" {
  type = string

  validation {
    condition     = can(regex("^[a-z]+-[a-z]+[0-9]+-[a-z]$", var.region))
    error_message = "The GCP region setting must be a zone, for example us-east4-c."
  }
}

variable "image" {
  type = string
}

variable "ssh_public_key" {
  type = string
}

variable "user_data" {
  type      = string
  sensitive = true
}

variable "labels" {
  type = map(string)
}
