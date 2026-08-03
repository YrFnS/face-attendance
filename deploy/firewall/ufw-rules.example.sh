#!/usr/bin/env bash
# Example only. This script adds narrowly scoped rules; it does not reset, enable,
# or change UFW defaults. Review the current firewall and keep an active SSH
# session before applying any rule.
set -euo pipefail

: "${ADMIN_CIDR:?Set ADMIN_CIDR, for example 10.20.0.0/24}"
: "${CAMERA_CIDR:?Set CAMERA_CIDR, for example 192.168.50.0/24}"
FTP_PORT="${FTP_PORT:-2121}"
PASSIVE_START="${PASSIVE_START:-30000}"
PASSIVE_END="${PASSIVE_END:-30009}"

sudo ufw allow from "$ADMIN_CIDR" to any port 22 proto tcp comment 'Face attendance SSH admin'
sudo ufw allow from "$ADMIN_CIDR" to any port 443 proto tcp comment 'Face attendance HTTPS admin'
sudo ufw allow from "$CAMERA_CIDR" to any port "$FTP_PORT" proto tcp comment 'Face attendance camera FTPS control'
sudo ufw allow from "$CAMERA_CIDR" to any port "$PASSIVE_START:$PASSIVE_END" proto tcp comment 'Face attendance camera FTPS passive'

sudo ufw status numbered
