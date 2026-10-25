mock_provider "hcloud" {
  mock_resource "hcloud_firewall" {
    defaults = {
      id = "1001"
    }
  }

  mock_resource "hcloud_server" {
    defaults = {
      ipv4_address = "192.0.2.10"
    }
  }
}

variables {
  name           = "offbox-contract"
  size           = "ccx33"
  region         = "fsn1"
  image          = "ubuntu-24.04"
  ssh_public_key = "ssh-ed25519 synthetic-test-key"
  user_data      = "#cloud-config\nsynthetic: true\n"
  labels         = { offbox = "1", offbox-host = "contract" }
}

run "host_contract" {
  command = apply

  assert {
    condition     = hcloud_server.this.server_type == var.size && hcloud_server.this.location == var.region && hcloud_server.this.image == var.image
    error_message = "Host size, location and OS image must use the common provider inputs."
  }

  assert {
    condition     = alltrue([for labels in [hcloud_server.this.labels, hcloud_firewall.this.labels] : labels == tomap({ offbox = "1", offbox-host = "contract" })])
    error_message = "Every billable resource must carry the offbox ownership labels."
  }

  assert {
    condition     = toset(hcloud_server.this.firewall_ids) == toset([1001])
    error_message = "The server must be attached to its own firewall."
  }

  assert {
    condition     = length(hcloud_firewall.this.rule) == 2 && alltrue([for rule in hcloud_firewall.this.rule : rule.direction == "in" && (rule.protocol == "icmp" || (rule.protocol == "tcp" && rule.port == "22")) && toset(rule.source_ips) == toset(["0.0.0.0/0", "::/0"])])
    error_message = "Only SSH and ICMP may enter."
  }

  assert {
    condition     = one(hcloud_server.this.public_net).ipv4_enabled
    error_message = "SSH requires a public IPv4 address."
  }

  assert {
    condition     = output.ip == "192.0.2.10" && output.ssh_user == "offbox"
    error_message = "Outputs must expose the public IPv4 address and the offbox SSH user."
  }
}
