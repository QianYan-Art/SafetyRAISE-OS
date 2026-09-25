#!/bin/sh
set -eu

# Certbot deploy hook：安装到 /etc/letsencrypt/renewal-hooks/deploy/ 后，把 DOMAIN 改为站点的证书名。
DOMAIN="example.com"

if [ "${RENEWED_LINEAGE:-}" != "/etc/letsencrypt/live/${DOMAIN}" ]; then
    exit 0
fi

nginx -t
systemctl reload nginx
