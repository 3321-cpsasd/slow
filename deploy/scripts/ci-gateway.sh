#!/bin/sh
set -eu
set -f

deploy_root=${DEPLOY_ROOT:-/opt/slow}
incoming_root="$deploy_root/incoming"
original_command=${SSH_ORIGINAL_COMMAND:-}

set -- $original_command
if [ "$#" -ne 2 ] || [ "$1" != deploy ]; then
  echo "Only 'deploy <commit-sha>' is permitted." >&2
  exit 64
fi

version=$2
case "$version" in
  *[!0-9a-f]* | "")
    echo "The release version must be a hexadecimal commit SHA." >&2
    exit 64
    ;;
esac
if [ "${#version}" -ne 40 ]; then
  echo "The release version must contain exactly 40 characters." >&2
  exit 64
fi

main_version=$(
  curl --fail --silent --show-error --location \
    --connect-timeout 10 --max-time 30 \
    -H "Accept: application/vnd.github+json" \
    https://api.github.com/repos/3321-cpsasd/slow/commits/main |
    python3 -c "import json, sys; print(json.load(sys.stdin)['sha'])"
)
if [ "$version" != "$main_version" ]; then
  echo "Refusing to deploy a commit that is not the current main revision." >&2
  exit 65
fi

mkdir -p "$incoming_root"
source_archive="$incoming_root/slow-$version.tar.gz"
source_archive_next=$(mktemp "$incoming_root/slow-$version.next.XXXXXX")
runtime_root=$(mktemp -d "$incoming_root/runtime-$version.XXXXXX")

cleanup() {
  rm -f "$source_archive_next"
  rm -rf "$runtime_root"
}
trap cleanup EXIT HUP INT TERM

curl --fail --silent --show-error --location --retry 3 \
  --connect-timeout 10 --max-time 120 \
  "https://codeload.github.com/3321-cpsasd/slow/tar.gz/$version" \
  --output "$source_archive_next"
tar -tzf "$source_archive_next" >/dev/null
mv "$source_archive_next" "$source_archive"

tar -xzf "$source_archive" -C "$runtime_root"
source_root=$(find "$runtime_root" -mindepth 1 -maxdepth 1 -type d -print -quit)
if [ -z "$source_root" ] || \
   [ ! -f "$source_root/deploy/compose.prod.yml" ] || \
   [ ! -f "$source_root/deploy/compose.demo.yml" ] || \
   [ ! -f "$source_root/deploy/scripts/remote-build-deploy.sh" ]; then
  echo "The verified release is missing required deployment files." >&2
  exit 66
fi

install -m 600 "$source_root/deploy/compose.prod.yml" "$deploy_root/compose.prod.yml"
install -m 600 "$source_root/deploy/compose.demo.yml" "$deploy_root/compose.demo.yml"
install -m 700 \
  "$source_root/deploy/scripts/remote-build-deploy.sh" \
  "$deploy_root/remote-build-deploy.sh"

APP_VERSION=$version \
REGISTRY=ghcr.io \
IMAGE_NAME=3321-cpsasd/slow \
WEB_ORIGIN=http://slow.net.cn \
SOURCE_ARCHIVE=$source_archive \
  "$deploy_root/remote-build-deploy.sh"
