output "instance_id" { value = nebius_compute_v1_instance.gpu.id }
output "boot_disk_id" { value = nebius_compute_v1_disk.boot.id }
output "data_disk_id" { value = nebius_compute_v1_disk.data.id }
output "public_ip" { value = nebius_compute_v1_instance.gpu.status.network_interfaces[0].public_ip_address.address }
output "private_ip" { value = nebius_compute_v1_instance.gpu.status.network_interfaces[0].ip_address.address }
output "image_id" { value = nebius_compute_v1_disk.boot.status.source_image_id }
output "tunnel_id" { value = nebius_tunnel_v1_tunnel.llm.id }
