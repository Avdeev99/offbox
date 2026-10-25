terraform {
  required_version = ">= 1.6"

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = ">= 1.68, < 1.71"
    }
  }
}

provider "hcloud" {}
