resource "hcloud_firewall" "this" {
  name   = var.name
  labels = var.labels

  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "22"
    source_ips = ["0.0.0.0/0", "::/0"]
  }

  rule {
    direction  = "in"
    protocol   = "icmp"
    source_ips = ["0.0.0.0/0", "::/0"]
  }
}

resource "hcloud_server" "this" {
  name         = var.name
  image        = var.image
  server_type  = var.size
  location     = var.region
  user_data    = var.user_data
  labels       = var.labels
  firewall_ids = [hcloud_firewall.this.id]

  public_net {
    ipv4_enabled = true
    ipv6_enabled = true
  }
}
