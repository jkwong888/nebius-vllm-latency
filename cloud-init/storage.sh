#!/usr/bin/env bash
# Prepare storage on a newly created VM. Existing workload migrations are manual.
set -Eeuo pipefail

DATA_DEVICE=/dev/disk/by-id/virtio-llm-data
for attempt in $(seq 1 60); do
  if test -b "$DATA_DEVICE"; then
    break
  fi
  sleep 2
done
test -b "$DATA_DEVICE"

mkdir -p /srv/llm
if ! mountpoint -q /srv/llm; then
  # First-boot initialization expects the blank data disk created by Terraform.
  test "$(lsblk -nr -o TYPE "$DATA_DEVICE" | wc -l)" -eq 1
  test -z "$(wipefs --noheadings --output TYPE "$DATA_DEVICE")"
  mkfs.ext4 -L llm-data "$DATA_DEVICE"

  DATA_UUID=$(blkid -s UUID -o value "$DATA_DEVICE")
  test -n "$DATA_UUID"
  if ! awk '$2 == "/srv/llm" { found=1 } END { exit !found }' /etc/fstab; then
    printf 'UUID=%s /srv/llm ext4 defaults 0 2\n' "$DATA_UUID" >> /etc/fstab
  fi
  systemctl daemon-reload
  mount /srv/llm
fi
test "$(findmnt -n -o UUID --target /srv/llm)" = "$(blkid -s UUID -o value "$DATA_DEVICE")"

# Bind mounts preserve the GPU image's Docker and host containerd configuration.
# K3s uses its own containerd under /srv/llm/k3s.
if ! mountpoint -q /var/lib/docker || ! mountpoint -q /var/lib/containerd; then
  systemctl stop docker.service docker.socket containerd.service
  for directory in docker containerd; do
    source_directory="/var/lib/$directory"
    destination_directory="/srv/llm/$directory"
    if mountpoint -q "$source_directory"; then
      continue
    fi
    mkdir -p "$source_directory" "$destination_directory"
    if test -n "$(find "$source_directory" -mindepth 1 -maxdepth 1 -print -quit)"; then
      if test -n "$(find "$destination_directory" -mindepth 1 -maxdepth 1 -print -quit)"; then
        echo "Both $source_directory and $destination_directory contain data. Migrate manually." >&2
        exit 1
      fi
      cp -a "$source_directory/." "$destination_directory/"
      mv "$source_directory" "$source_directory.boot-backup"
      mkdir -p "$source_directory"
    fi
    if ! awk -v target="$source_directory" '$2 == target { found=1 } END { exit !found }' /etc/fstab; then
      printf '%s %s none bind,x-systemd.requires-mounts-for=/srv/llm 0 0\n' \
        "$destination_directory" "$source_directory" >> /etc/fstab
    fi
    systemctl daemon-reload
    mount "$source_directory"
  done
fi

for directory in docker containerd; do
  test "$(findmnt -n -o UUID --target "/var/lib/$directory")" = "$(blkid -s UUID -o value "$DATA_DEVICE")"
done

for service in docker containerd k3s; do
  mkdir -p "/etc/systemd/system/$service.service.d"
  cat > "/etc/systemd/system/$service.service.d/storage.conf" <<EOF
[Unit]
RequiresMountsFor=/srv/llm /var/lib/docker /var/lib/containerd
EOF
done
systemctl daemon-reload
systemctl start containerd.service docker.service
