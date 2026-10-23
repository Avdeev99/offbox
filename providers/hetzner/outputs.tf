output "ip" {
  value = hcloud_server.this.ipv4_address
}

output "ssh_user" {
  value = "offbox"
}
