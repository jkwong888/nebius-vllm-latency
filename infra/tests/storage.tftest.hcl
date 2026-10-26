mock_provider "nebius" {}

variables {
  project_id     = "project-test"
  network_id     = "vpcnetwork-test"
  subnet_id      = "vpcsubnet-test"
  ssh_public_key = "ssh-ed25519 test"
}

run "new_vm_storage" {
  command = plan

  assert {
    condition     = nebius_compute_v1_disk.boot.type == "NETWORK_SSD" && nebius_compute_v1_disk.boot.size_gibibytes == 100
    error_message = "The boot disk must default to 100 GiB Network SSD."
  }

  assert {
    condition     = nebius_compute_v1_disk.data.type == "NETWORK_SSD_NON_REPLICATED" && nebius_compute_v1_disk.data.size_gibibytes == 279
    error_message = "The data disk must default to 279 GiB non-replicated Network SSD."
  }

  assert {
    condition     = nebius_compute_v1_instance.gpu.secondary_disks[0].device_id == "llm-data" && nebius_compute_v1_instance.gpu.secondary_disks[0].attach_mode == "READ_WRITE"
    error_message = "The secondary disk must use the stable llm-data device ID."
  }
}

run "invalid_data_disk_size" {
  command = plan
  variables {
    data_disk_size_gib = 100
  }
  expect_failures = [var.data_disk_size_gib]
}
