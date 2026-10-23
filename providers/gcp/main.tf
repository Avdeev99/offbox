locals {
  region = replace(var.region, "/-[a-z]$/", "")
}

resource "google_compute_network" "this" {
  name                    = var.name
  description             = "Offbox host ${var.name}"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
}

resource "google_compute_subnetwork" "this" {
  name          = var.name
  region        = local.region
  network       = google_compute_network.this.id
  ip_cidr_range = "10.90.0.0/24"
}

resource "google_compute_firewall" "this" {
  name          = "${var.name}-ssh"
  description   = "Offbox SSH and ICMP for ${var.name}"
  network       = google_compute_network.this.name
  direction     = "INGRESS"
  source_ranges = ["0.0.0.0/0"]
  target_tags   = [var.name]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }

  allow {
    protocol = "icmp"
  }
}

resource "google_compute_instance" "this" {
  name                = var.name
  machine_type        = var.size
  zone                = var.region
  labels              = var.labels
  tags                = [var.name]
  deletion_protection = false

  boot_disk {
    auto_delete = true

    initialize_params {
      image  = var.image
      size   = 50
      type   = "pd-balanced"
      labels = var.labels
    }
  }

  network_interface {
    subnetwork = google_compute_subnetwork.this.id

    access_config {}
  }

  metadata = {
    user-data              = var.user_data
    enable-oslogin         = "FALSE"
    block-project-ssh-keys = "TRUE"
  }
}
