#!/usr/bin/env bash
set -Eeuo pipefail
umask 027

mkdir -p /var/log/llm-bootstrap /var/lib/llm-bootstrap
exec > >(tee -a /var/log/llm-bootstrap/bootstrap.log) 2>&1
AGENT_TMP=""
on_exit() {
  EXIT_CODE=$?
  if test "$EXIT_CODE" -ne 0; then
    printf 'failed with exit code %s. See bootstrap.log\n' "$EXIT_CODE" > /var/lib/llm-bootstrap/status
  fi
  if test -n "$AGENT_TMP"; then
    rm -rf "$AGENT_TMP"
  fi
}
trap on_exit EXIT
echo preparing > /var/lib/llm-bootstrap/status

read_setting() {
  python3 -c 'import json,sys; print(json.load(open("/etc/llm-bootstrap.json"))[sys.argv[1]])' "$1"
}

K3S_VERSION=$(read_setting k3s_version)
AGENT_VERSION=$(read_setting agent_version)
TUNNEL_ID=$(read_setting tunnel_id)

# Mount storage before installing or starting any workload services.
/usr/local/sbin/llm-storage
mkdir -p /srv/llm/workloads/hf-cache /srv/llm/workloads/measurements /srv/llm/k3s

# Nebius's ubuntu24.04-cuda13.0 image supplies these components.
nvidia-smi
nvidia-container-cli --version
command -v nvidia-container-runtime
DRIVER_MAJOR=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1 | cut -d. -f1)
test "$DRIVER_MAJOR" -ge 580

export DEBIAN_FRONTEND=noninteractive
apt-get update
# K3s supplies its own containerd. Do not install Ubuntu's docker.io or
# containerd packages, which conflict with containerd.io in the GPU image.
apt-get install -y --no-install-recommends curl ca-certificates jq rsync

PRIVATE_IP=$(ip -4 -o addr show dev eth0 scope global | awk '{split($4, a, "/"); print a[1]; exit}')
test -n "$PRIVATE_IP"
mkdir -p /etc/rancher/k3s
cat > /etc/rancher/k3s/config.yaml <<EOF
node-ip: "$PRIVATE_IP"
advertise-address: "$PRIVATE_IP"
data-dir: /srv/llm/k3s
node-label:
  - latency.nebius.com/storage=true
write-kubeconfig-mode: "0600"
secrets-encryption: true
disable:
  - servicelb
EOF
curl -fsSL --retry 5 https://get.k3s.io -o /var/lib/llm-bootstrap/install-k3s.sh
INSTALL_K3S_VERSION="$K3S_VERSION" sh /var/lib/llm-bootstrap/install-k3s.sh
systemctl daemon-reload
systemctl restart k3s
for attempt in $(seq 1 120); do
  if k3s kubectl wait --for=condition=Ready node --all --timeout=5s; then
    break
  fi
  sleep 2
done
k3s kubectl wait --for=condition=Ready node --all --timeout=10s
grep -q nvidia /srv/llm/k3s/agent/etc/containerd/config.toml
k3s kubectl get runtimeclass nvidia

# Advertise the node's GPUs to Kubernetes before bootstrap completes.
k3s kubectl apply -f /etc/llm-bootstrap/nvidia-device-plugin.yaml
k3s kubectl -n kube-system rollout status daemonset/nvidia-device-plugin --timeout=300s

install -d -o ubuntu -g ubuntu -m0700 /home/ubuntu/.kube
install -o ubuntu -g ubuntu -m0600 /etc/rancher/k3s/k3s.yaml /home/ubuntu/.kube/config

AGENT_BASE=https://storage.eu-north1.nebius.cloud/products/releases/nebius-tunnel-agent
AGENT_ASSET=nebius-tunnel-agent-linux-x86_64.tar.gz
AGENT_TMP=$(mktemp -d)
curl -fsSL --retry 5 "$AGENT_BASE/$AGENT_VERSION/$AGENT_ASSET" -o "$AGENT_TMP/$AGENT_ASSET"
curl -fsSL --retry 5 "$AGENT_BASE/$AGENT_VERSION/checksums-sha256.txt" -o "$AGENT_TMP/checksums-sha256.txt"
(
  cd "$AGENT_TMP"
  grep " $AGENT_ASSET$" checksums-sha256.txt > selected.sha256
  test -s selected.sha256
  sha256sum -c selected.sha256
  tar -xzf "$AGENT_ASSET"
)
install -m0755 "$AGENT_TMP/nebius-tunnel-agent" /usr/local/bin/nebius-tunnel-agent
getent group tunna >/dev/null || groupadd --system tunna
id -u tunna >/dev/null 2>&1 || useradd --system --gid tunna --no-create-home --shell /usr/sbin/nologin tunna
install -d -o tunna -g tunna -m0750 /etc/nebius-tunnel-agent
cat > /etc/nebius-tunnel-agent/config.yaml <<EOF
tunnel_id: $TUNNEL_ID
iam:
  metadata:
    enabled: true
logging:
  level: info
  format: json
services:
  - name: llm
    type: http
    target: 127.0.0.1:8088
EOF
chown tunna:tunna /etc/nebius-tunnel-agent/config.yaml
chmod 0640 /etc/nebius-tunnel-agent/config.yaml
cat > /etc/systemd/system/nebius-tunnel-agent.service <<'EOF'
[Unit]
Description=Nebius tunnel agent for LLM latency test
After=network-online.target
Wants=network-online.target
[Service]
User=tunna
Group=tunna
ExecStart=/usr/local/bin/nebius-tunnel-agent --config /etc/nebius-tunnel-agent/config.yaml
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now nebius-tunnel-agent
systemctl is-active --quiet nebius-tunnel-agent
nvidia-smi --query-gpu=name,driver_version --format=csv > /var/log/llm-bootstrap/gpu.csv
cp /etc/llm-bootstrap.json /var/log/llm-bootstrap/versions.json
echo ready > /var/lib/llm-bootstrap/status
