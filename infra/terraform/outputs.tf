output "instance_id" {
  description = "OCID of the k3s instance."
  value       = oci_core_instance.k3s.id
}

output "public_ip" {
  description = "Public IP of the k3s node."
  value       = oci_core_instance.k3s.public_ip
}

output "private_ip" {
  description = "Private IP of the k3s node inside the VCN."
  value       = oci_core_instance.k3s.private_ip
}

output "availability_domain" {
  description = "AD the instance actually landed in. The one the capacity lottery finally paid out on."
  value       = oci_core_instance.k3s.availability_domain
}

output "availability_domain_count" {
  description = "How many ADs this region reports. retry-apply.sh reads this to size its cycle."
  value       = local.ad_count
}

output "image_name" {
  description = "Canonical Ubuntu image the instance booted from. Record this; the OCID rotates."
  value       = local.image_name
}

output "shape_summary" {
  description = "Shape as actually applied, for the DECISIONS.md record."
  value       = "VM.Standard.A1.Flex ${var.ocpus} OCPU / ${var.memory_in_gbs} GB / ${var.boot_volume_size_in_gbs} GB boot"
}

locals {
  # The key is project-scoped, not the machine default, so every command below
  # has to name it explicitly with -i.
  ssh_private_key = trimsuffix(var.ssh_public_key_path, ".pub")
  ssh_base        = "ssh -i ${local.ssh_private_key} ubuntu@${oci_core_instance.k3s.public_ip}"
}

output "ssh_command" {
  description = "Copy-paste SSH into the node."
  value       = local.ssh_base
}

output "fetch_kubeconfig_command" {
  description = "Pull the node's kubeconfig. It points at 127.0.0.1:6443, which is what the tunnel forwards."
  value       = "scp -i ${local.ssh_private_key} ubuntu@${oci_core_instance.k3s.public_ip}:~/.kube/config ~/.kube/evalgate.yaml"
}

output "kubectl_tunnel_command" {
  description = <<-EOT
    Port 6443 is closed to the internet, so kubectl reaches the API through
    this tunnel. Run it in one terminal and leave it open, then use the
    kubeconfig from fetch_kubeconfig_command unmodified in another.
  EOT
  value       = "ssh -i ${local.ssh_private_key} -N -L 6443:127.0.0.1:6443 ubuntu@${oci_core_instance.k3s.public_ip}"
}

output "kubectl_usage" {
  description = "What to run once the tunnel is up."
  value       = "KUBECONFIG=~/.kube/evalgate.yaml kubectl get nodes -o wide"
}

output "bootstrap_status_command" {
  description = "Cloud-init runs for a few minutes after apply returns. This says whether k3s is up."
  value       = "${local.ssh_base} 'cat /var/lib/evalgate/bootstrap.done 2>/dev/null || tail -20 /var/log/evalgate-bootstrap.log'"
}
