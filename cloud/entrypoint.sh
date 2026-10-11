#!/bin/sh
set -eu
mkdir -p /var/data
chown cloud:cloud /var/data
exec runuser -u cloud -- python3 /app/cloud/server.py
