resource "nebius_compute_v1_disk" "boot" {
  parent_id       = var.project_id
  name            = "${var.name}-boot"
  type            = "NETWORK_SSD"
  size_gibibytes  = var.boot_disk_size_gib
  source_image_id = var.image_id
  source_image_family = var.image_id == null ? {
    image_family = "ubuntu24.04-cuda13.0"
  } : null
}

resource "nebius_compute_v1_disk" "data" {
  parent_id      = var.project_id
  name           = "${var.name}-data"
  type           = "NETWORK_SSD_NON_REPLICATED"
  size_gibibytes = var.data_disk_size_gib
}

resource "nebius_tunnel_v1_tunnel" "llm" {
  parent_id = var.project_id
  title     = "Gemma latency experiment"
}

resource "nebius_iam_v1_service_account" "agent" {
  parent_id = var.project_id
  name      = "${var.name}-agent"
}

resource "nebius_iam_v1_group" "agents" {
  parent_id = var.project_id
  name      = "${var.name}-agents"
}

resource "nebius_iam_v1_group_membership" "agent" {
  parent_id = nebius_iam_v1_group.agents.id
  member_id = nebius_iam_v1_service_account.agent.id
}

resource "nebius_iam_v1_access_permit" "agent" {
  parent_id   = nebius_iam_v1_group.agents.id
  resource_id = nebius_tunnel_v1_tunnel.llm.id
  role        = "applicationtunnel.agent"
}

resource "nebius_compute_v1_instance" "gpu" {
  parent_id = var.project_id
  name      = var.name
  resources = { platform = var.platform, preset = var.preset }
  boot_disk = {
    attach_mode   = "READ_WRITE"
    device_id     = "boot"
    existing_disk = { id = nebius_compute_v1_disk.boot.id }
  }
  secondary_disks = [{
    attach_mode   = "READ_WRITE"
    device_id     = "llm-data"
    existing_disk = { id = nebius_compute_v1_disk.data.id }
  }]
  network_interfaces = [{
    name              = "eth0"
    subnet_id         = var.subnet_id
    ip_address        = {}
    public_ip_address = { static = true }
  }]
  preemptible         = { on_preemption = "STOP" }
  follows_spot_price  = var.spot_pricing_policy_id == null ? {} : null
  spot_pricing_policy = var.spot_pricing_policy_id == null ? null : { id = var.spot_pricing_policy_id }
  recovery_policy     = "FAIL"
  service_account_id  = nebius_iam_v1_service_account.agent.id
  cloud_init_user_data = "#cloud-config\n${yamlencode({
    users = [{ name = "ubuntu", groups = ["sudo"], sudo = "ALL=(ALL) NOPASSWD:ALL", shell = "/bin/bash", ssh_authorized_keys = [var.ssh_public_key] }]
    write_files = [
      { path = "/usr/local/sbin/llm-storage", permissions = "0750", content = file("${path.module}/../cloud-init/storage.sh") },
      { path = "/usr/local/sbin/llm-bootstrap", permissions = "0750", content = file("${path.module}/../cloud-init/bootstrap.sh") },
      { path = "/etc/llm-bootstrap/nvidia-device-plugin.yaml", permissions = "0644", content = file("${path.module}/../cloud-init/nvidia-device-plugin.yaml") },
      { path = "/etc/llm-bootstrap.json", permissions = "0644", content = jsonencode({ k3s_version = var.k3s_version, agent_version = var.agent_version, tunnel_id = nebius_tunnel_v1_tunnel.llm.id, image_id = nebius_compute_v1_disk.boot.status.source_image_id }) }
    ]
    runcmd = [["/usr/local/sbin/llm-bootstrap"]]
  })}"
  depends_on = [nebius_iam_v1_access_permit.agent, nebius_iam_v1_group_membership.agent]
}
