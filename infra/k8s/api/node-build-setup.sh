#!/usr/bin/env bash
# Install a native arm64 image builder on the k3s node. Run once, over SSH.
#
#   scp -i ~/.ssh/evalgate_ed25519 infra/k8s/api/node-build-setup.sh ubuntu@<node>:/tmp/
#   ssh -i ~/.ssh/evalgate_ed25519 ubuntu@<node> 'sudo bash /tmp/node-build-setup.sh'
#
# Why not Docker: k3s already runs containerd, and `apt install docker.io` would
# add a second container runtime *and* a second containerd daemon permanently
# resident on a single node that also has to carry Prometheus, Airflow, and
# Kafka. It would also force a ~250 MB `docker save` tarball to disk and back on
# every build purely to cross a daemon boundary that does not need to exist.
#
# Why not nerdctl-full: that bundle ships its own containerd, runc, and CNI,
# which would collide with the ones k3s manages. Only the two binaries that are
# actually missing get installed.
#
# Everything here builds natively. The node is aarch64 and the image is aarch64,
# so there is no buildx, no QEMU, and no emulation anywhere in the path.

set -euo pipefail

NERDCTL_VERSION="${NERDCTL_VERSION:-2.3.5}"
BUILDKIT_VERSION="${BUILDKIT_VERSION:-0.32.0}"
ARCH=arm64
K3S_CONTAINERD_SOCK=/run/k3s/containerd/containerd.sock

log() { printf '[node-build-setup] %s\n' "$*"; }

[ "$(uname -m)" = "aarch64" ] || { log "FATAL: expected aarch64, got $(uname -m)."; exit 1; }
[ -S "$K3S_CONTAINERD_SOCK" ] || { log "FATAL: no k3s containerd socket at $K3S_CONTAINERD_SOCK."; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

# --- nerdctl -----------------------------------------------------------------
if command -v nerdctl >/dev/null 2>&1; then
  log "nerdctl already installed: $(nerdctl --version)"
else
  log "Installing nerdctl ${NERDCTL_VERSION} (${ARCH})."
  curl -fsSL -o "$tmp/nerdctl.tgz" \
    "https://github.com/containerd/nerdctl/releases/download/v${NERDCTL_VERSION}/nerdctl-${NERDCTL_VERSION}-linux-${ARCH}.tar.gz"
  # Only the nerdctl binary. The archive also carries containerd-rootless
  # helpers that this node has no use for.
  tar -C /usr/local/bin -xzf "$tmp/nerdctl.tgz" nerdctl
fi

# --- buildkit ----------------------------------------------------------------
if command -v buildkitd >/dev/null 2>&1; then
  log "buildkit already installed: $(buildkitd --version)"
else
  log "Installing buildkit ${BUILDKIT_VERSION} (${ARCH})."
  curl -fsSL -o "$tmp/buildkit.tgz" \
    "https://github.com/moby/buildkit/releases/download/v${BUILDKIT_VERSION}/buildkit-v${BUILDKIT_VERSION}.linux-${ARCH}.tar.gz"
  tar -C /usr/local -xzf "$tmp/buildkit.tgz" bin/buildkitd bin/buildctl
fi

# --- buildkitd as a service --------------------------------------------------
# The containerd worker writes finished images straight into k3s's own
# containerd, in the k8s.io namespace. That removes the export/import round trip
# entirely: when the build ends, kubelet can already see the image.
#
# --oci-worker=false because the two workers would otherwise both be enabled and
# a build could land in the wrong one.
log "Writing /etc/systemd/system/buildkitd.service."
cat > /etc/systemd/system/buildkitd.service <<EOF
[Unit]
Description=BuildKit (containerd worker against k3s, namespace k8s.io)
Requires=k3s.service
After=k3s.service

[Service]
ExecStart=/usr/local/bin/buildkitd \\
  --oci-worker=false \\
  --containerd-worker=true \\
  --containerd-worker-addr=${K3S_CONTAINERD_SOCK} \\
  --containerd-worker-namespace=k8s.io
Restart=always
RestartSec=5
Type=notify

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now buildkitd
sleep 2
systemctl is-active --quiet buildkitd || { log "FATAL: buildkitd did not start."; journalctl -u buildkitd -n 30 --no-pager; exit 1; }

log "Done."
nerdctl --version
buildkitd --version
buildctl debug workers
