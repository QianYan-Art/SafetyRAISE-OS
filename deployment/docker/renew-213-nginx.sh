#!/bin/sh
set -eu

if [ "${RENEWED_LINEAGE:-}" != "/etc/letsencrypt/live/safetyraise.cn" ]; then
    exit 0
fi

nginx -t
systemctl reload nginx
