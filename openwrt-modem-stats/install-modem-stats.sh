#!/bin/bash
# Install modem-stats service on OpenWrt router
# Usage: ./openwrt/install-modem-stats.sh [user@host]
#
# What this installs:
#   /usr/share/modem-stats/collect     — AT command collection script
#   /etc/init.d/modem-stats            — procd service (runs collect every 60s)
#   /usr/share/rpcd/acl.d/hass.json   — updated ACL: adds file.read for HA integration
#
# Modem assumed: Quectel RG502Q-EA on /dev/ttyUSB2 (MBIM mode)
# sms_tool must already be installed on the router

set -euo pipefail

REMOTE="${1:-root@openwrt.local}"
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "==> Deploying to $REMOTE"

# Use cat | ssh (OpenWrt dropbear lacks sftp-server, so scp won't work)

ssh "$REMOTE" 'mkdir -p /usr/share/modem-stats'

echo "  -> /usr/share/modem-stats/collect"
cat "$DIR/modem-stats/collect" \
  | ssh "$REMOTE" 'cat > /usr/share/modem-stats/collect && chmod +x /usr/share/modem-stats/collect'

echo "  -> /etc/init.d/modem-stats"
cat "$DIR/modem-stats/modem-stats.init" \
  | ssh "$REMOTE" 'cat > /etc/init.d/modem-stats && chmod +x /etc/init.d/modem-stats'

echo "  -> /usr/share/rpcd/acl.d/hass.json"
cat "$DIR/hass.json" \
  | ssh "$REMOTE" 'cat > /usr/share/rpcd/acl.d/hass.json'

echo "==> Adding installed files to /etc/sysupgrade.conf"
ssh "$REMOTE" '
  for f in /usr/share/modem-stats/collect /etc/init.d/modem-stats /usr/share/rpcd/acl.d/hass.json; do
    grep -qxF "$f" /etc/sysupgrade.conf 2>/dev/null || echo "$f" >> /etc/sysupgrade.conf
  done
'

echo "==> Enabling and starting modem-stats service"
ssh "$REMOTE" '/etc/init.d/modem-stats enable && /etc/init.d/modem-stats restart'

echo "==> Restarting rpcd so new ACL takes effect"
ssh "$REMOTE" '/etc/init.d/rpcd restart'

echo "==> Waiting 10s for first collection run..."
sleep 10

echo "==> Modem stats output:"
ssh "$REMOTE" 'cat /tmp/modem-stats.json'
