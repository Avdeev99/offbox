variable "name" {
  type = string
}

variable "size" {
  type = string
}

variable "region" {
  type = string
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
