terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = ">= 8.7, < 8.8"
    }
  }
}

provider "google" {}
