mock_provider "google" {}

variables {
  name           = "offbox-contract"
  size           = "e2-standard-4"
  region         = "us-east4-c"
  image          = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
  ssh_public_key = "ssh-ed25519 synthetic-test-key"
  user_data      = "#cloud-config\nsynthetic: true\n"
  labels         = { offbox = "1", offbox-host = "contract" }
}

run "isolated_host_contract" {
  command = plan

  assert {
    condition     = google_compute_instance.this.zone == "us-east4-c" && google_compute_subnetwork.this.region == "us-east4"
    error_message = "The selected zone must determine the subnet region."
  }

  assert {
    condition     = google_compute_instance.this.machine_type == var.size && google_compute_instance.this.boot_disk[0].initialize_params[0].image == var.image
    error_message = "Host size and OS image must use the common provider inputs."
  }

  assert {
    condition     = google_compute_instance.this.boot_disk[0].auto_delete && !google_compute_instance.this.deletion_protection
    error_message = "Destroy must remove the boot disk and must not require a separate deletion-protection update."
  }

  assert {
    condition     = length(google_compute_instance.this.service_account) == 0
    error_message = "Development workloads must not inherit a Google service account."
  }

  assert {
    condition     = google_compute_instance.this.metadata["user-data"] == var.user_data && google_compute_instance.this.metadata["enable-oslogin"] == "FALSE" && google_compute_instance.this.metadata["block-project-ssh-keys"] == "TRUE"
    error_message = "Bootstrap and SSH identity must use instance-scoped cloud-init and the pinned host key workflow."
  }

  assert {
    condition     = !google_compute_network.this.auto_create_subnetworks && google_compute_subnetwork.this.ip_cidr_range == "10.90.0.0/24"
    error_message = "Each host needs its own network without automatic subnets or overlap with the Docker bridge."
  }

  assert {
    condition     = length(google_compute_firewall.this.allow) == 2 && alltrue([for rule in google_compute_firewall.this.allow : rule.protocol == "icmp" || (rule.protocol == "tcp" && toset(rule.ports) == toset(["22"]))]) && google_compute_firewall.this.source_ranges == toset(["0.0.0.0/0"]) && google_compute_firewall.this.target_tags == toset([var.name])
    error_message = "Only SSH and ICMP may enter, and the rule must target this host."
  }

  assert {
    condition     = length(google_compute_instance.this.network_interface[0].access_config) == 1
    error_message = "SSH requires an ephemeral public address, released with the instance."
  }
}

run "region_requires_a_zone" {
  command = plan

  variables {
    region = "us-east4"
  }

  expect_failures = [var.region]
}
