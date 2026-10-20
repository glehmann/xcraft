#!/bin/bash
# A local Garage server, as the remote storage of xcraft.toml, for development: like the
# storage of XCP-ng, with authenticated S3 writes, on localhost:3900, and public reads,
# through its website endpoint, on http://xcraft.web.garage.localhost:3902.
#
#   ./local-s3.sh up       set it up, or start it again, and show the credentials to export
#   ./local-s3.sh env      only show them, as in: eval "$(./local-s3.sh env)"
#   ./local-s3.sh status   show whether it runs, and what it stores; fails when it does not run
#   ./local-s3.sh down     destroy it, with its data
set -euo pipefail

NAME=xcraft-garage
IMAGE=docker.io/dxflrs/garage:v2.1.0
CONFIG=/run/secrets/$NAME
BUCKET=xcraft
KEY=xcraft-dev

garage() { podman exec -e RUST_LOG=warn "$NAME" /garage -c "$CONFIG" "$@"; }

up() {
    if podman container exists "$NAME"; then
        podman start "$NAME" >/dev/null
    else
        # the image has no shell: its configuration is a secret
        podman secret exists "$NAME" || cat <<CONF | podman secret create "$NAME" - >/dev/null
metadata_dir = "/var/lib/garage/meta"
data_dir = "/var/lib/garage/data"
db_engine = "sqlite"
replication_factor = 1
rpc_bind_addr = "[::]:3901"
rpc_secret = "$(od -An -tx1 -N32 /dev/urandom | tr -d ' \n')"

[s3_api]
s3_region = "garage"
api_bind_addr = "[::]:3900"
root_domain = ".s3.garage.localhost"

[s3_web]
bind_addr = "[::]:3902"
root_domain = ".web.garage.localhost"
index = "index.html"
CONF
        podman run -d --name "$NAME" --secret "$NAME" -v "$NAME:/var/lib/garage" \
            -p 127.0.0.1:3900:3900 -p 127.0.0.1:3902:3902 "$IMAGE" /garage -c "$CONFIG" server >/dev/null
    fi
    for _ in $(seq 50); do garage status >/dev/null 2>&1 && break; sleep 0.2; done
    garage status >/dev/null || { echo "Garage did not start, see: podman logs $NAME" >&2; exit 1; }
    # each step, for a setup interrupted before
    if garage layout show | grep -q '^Current cluster layout version: 0$'; then
        garage layout assign -z local -c 10G "$(garage node id -q | cut -d@ -f1)" >/dev/null
        garage layout apply --version 1 >/dev/null
    fi
    garage bucket info "$BUCKET" >/dev/null 2>&1 || garage bucket create "$BUCKET" >/dev/null
    garage key info "$KEY" >/dev/null 2>&1 || garage key create "$KEY" >/dev/null
    garage bucket allow --read --write --owner "$BUCKET" --key "$KEY" >/dev/null
    garage bucket website --allow "$BUCKET" >/dev/null
    echo "Garage is up: S3 on http://localhost:3900, public reads on http://$BUCKET.web.garage.localhost:3902" >&2
    echo "to push to it, export its credentials:" >&2
    env
}

env() {
    local info
    info=$(garage key info --show-secret "$KEY")
    echo "export AWS_ACCESS_KEY_ID=$(sed -n 's/^Key ID: *//p' <<<"$info")"
    echo "export AWS_SECRET_ACCESS_KEY=$(sed -n 's/^Secret key: *//p' <<<"$info")"
}

status() {
    if ! podman container exists "$NAME"; then
        echo "Garage is not set up: ./local-s3.sh up sets it up"
        return 1
    fi
    if [ "$(podman inspect --format '{{.State.Running}}' "$NAME")" != true ]; then
        echo "Garage is stopped, with its data: ./local-s3.sh up starts it again"
        return 1
    fi
    local info
    info=$(garage bucket info "$BUCKET")
    echo "Garage is running: S3 on http://localhost:3900, public reads on http://$BUCKET.web.garage.localhost:3902"
    echo "bucket $BUCKET: $(sed -n 's/^Objects: *//p' <<<"$info") objects, $(sed -n 's/^Size: *\([^(]*[^( ]\) *(.*/\1/p' <<<"$info")"
}

down() {
    podman rm --force --time 0 "$NAME" >/dev/null 2>&1 || true
    podman volume rm --force "$NAME" >/dev/null 2>&1 || true
    podman secret rm "$NAME" >/dev/null 2>&1 || true
    echo "Garage is down, and its data removed" >&2
}

case "${1:-}" in
    up | env | status | down) "$1" ;;
    *) sed -n 's/^#   //p' "$0" >&2; exit 2 ;;
esac
