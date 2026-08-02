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

output "ssh_command" {
  description = "Copy-paste SSH into the node."
  value       = "ssh ubuntu@${oci_core_instance.k3s.public_ip}"
}

output "fetch_kubeconfig_command" {
  description = "Copy-paste to pull a kubeconfig that points at the public API server."
  value       = "scp ubuntu@${oci_core_instance.k3s.public_ip}:~/kubeconfig-public.yaml ~/.kube/evalgate.yaml && KUBECONFIG=~/.kube/evalgate.yaml kubectl get nodes -o wide"
}

output "bootstrap_status_command" {
  description = "Cloud-init runs for a few minutes after apply returns. This says whether k3s is up."
  value       = "ssh ubuntu@${oci_core_instance.k3s.public_ip} 'cat /var/lib/evalgate/bootstrap.done 2>/dev/null || tail -20 /var/log/evalgate-bootstrap.log'"
}
