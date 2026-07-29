#!/bin/sh
set -eu

deploy_root=${DEPLOY_ROOT:-/opt/slow}

if ! command -v docker >/dev/null 2>&1; then
  sudo dnf -y install wget
  sudo wget -O /etc/yum.repos.d/docker-ce.repo \
    http://mirrors.cloud.aliyuncs.com/docker-ce/linux/centos/docker-ce.repo
  sudo sed -i \
    's|https://mirrors.aliyun.com|http://mirrors.cloud.aliyuncs.com|g' \
    /etc/yum.repos.d/docker-ce.repo
  sudo dnf -y install dnf-plugin-releasever-adapter --repo alinux3-plus
  sudo dnf -y install \
    docker-ce \
    docker-ce-cli \
    containerd.io \
    docker-buildx-plugin \
    docker-compose-plugin
fi

sudo systemctl enable --now docker
sudo mkdir -p "$deploy_root/data/attachments" "$deploy_root/data/backups"
sudo chown -R "$(id -u):$(id -g)" "$deploy_root"
sudo chown -R 10001:10001 "$deploy_root/data"
if [ ! -f "$deploy_root/.env" ]; then
  (umask 077 && : > "$deploy_root/.env")
fi
sudo usermod -aG docker "$USER"

echo "ECS bootstrap completed."
echo "Reconnect the SSH session so the docker group membership takes effect."
echo "Then fill the server-only settings in $deploy_root/.env."
