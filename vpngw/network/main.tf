terraform {
  required_version = ">= 1.6.0"
  required_providers {
    nebius = {
      source  = "nebius/nebius"
      version = "0.6.73"
    }
  }
}

provider "nebius" {}

variable "project_id" {
  type    = string
  default = "project-e03ag8xalc00xd6zaxpqgy"
}

# External peer: home pfSense, protected LAN 192.168.0.0/24.
variable "sites" {
  type = map(object({
    cidr          = string
    workload_cidr = string
  }))
  default = {
    gateway = { cidr = "10.80.0.0/16", workload_cidr = "10.80.1.0/24" }
  }
}

resource "nebius_vpc_v1_pool" "private" {
  for_each   = var.sites
  parent_id  = var.project_id
  name       = "vpngw-${each.key}-private"
  version    = "IPV4"
  visibility = "PRIVATE"
  cidrs = [{
    cidr            = each.value.cidr
    state           = "AVAILABLE"
    max_mask_length = 32
  }]
}

resource "nebius_vpc_v1_network" "site" {
  for_each  = var.sites
  parent_id = var.project_id
  name      = "vpngw-${each.key}"
  ipv4_private_pools = {
    pools = [{ id = nebius_vpc_v1_pool.private[each.key].id }]
  }
}

# VPNGW prep-network owns the separate gateway subnet and its route table.
resource "nebius_vpc_v1_subnet" "workload" {
  for_each   = var.sites
  parent_id  = var.project_id
  name       = "vpngw-${each.key}-workload"
  network_id = nebius_vpc_v1_network.site[each.key].id
  ipv4_private_pools = {
    use_network_pools = false
    pools             = [{ cidrs = [{ cidr = each.value.workload_cidr }] }]
  }
  ipv4_public_pools = { use_network_pools = true }
}

output "sites" {
  value = {
    for name, site in var.sites : name => {
      network_id      = nebius_vpc_v1_network.site[name].id
      workload_subnet = nebius_vpc_v1_subnet.workload[name].id
      workload_cidr   = site.workload_cidr
      gateway_cidr    = cidrsubnet(site.cidr, 8, 0)
    }
  }
}

# Pass this file after the existing tfvars when planning the Gemma deployment.
output "gemma_tfvars" {
  value = join("\n", [
    "project_id = ${jsonencode(var.project_id)}",
    "network_id = ${jsonencode(nebius_vpc_v1_network.site["gateway"].id)}",
    "subnet_id = ${jsonencode(nebius_vpc_v1_subnet.workload["gateway"].id)}",
    "",
  ])
}
