#!/usr/bin/env bash
# Make the device answer at  http://proto.local  on every network it joins.
#
#   sudo bash scripts/setup_hostname.sh           # name "proto"
#   sudo bash scripts/setup_hostname.sh myname    # http://myname.local
#
# Nobody has to run this by hand for the name "proto": the protogen service
# checks the name at every start (pi/hostname_guard.py) and runs this script
# when it is off. Run it yourself only to give one device another name; the
# name is then remembered in saves/hostname and the service keeps to it.
#
# What it does:
#   1. sets the system hostname (it survives reboots and new Wi-Fi networks)
#   2. makes sure avahi (the service that announces <name>.local) is installed,
#      enabled and announces on Wi-Fi and Ethernet
#
# The web UI also listens on port 80 (see WEB_PORT_PLAIN in pi/config.py), so
# no ":5001" is needed after this.
#
# Two devices with the same name in one network: avahi renames the second one
# to <name>-2.local by itself.
set -eu

NAME="${1:-proto}"

if [ "$(id -u)" != "0" ]; then
  echo "run with sudo: sudo bash $0 $NAME" >&2
  exit 1
fi
case "$NAME" in
  *[!a-z0-9-]*|-*|*-|"") echo "name must be lower-case letters, digits and '-' only" >&2; exit 1 ;;
esac

# remember the choice for the service (saves/ survives updates)
SAVES="$(cd "$(dirname "$0")/.." && pwd)/saves"
if [ -d "$SAVES" ] && { [ "$NAME" != "proto" ] || [ -f "$SAVES/hostname" ]; }; then
  printf '%s\n' "$NAME" >"$SAVES/hostname"
fi

OLD="$(hostname)"
echo "hostname: $OLD -> $NAME"
hostnamectl set-hostname "$NAME"

# /etc/hosts: sudo complains (and gets slow) when the name does not resolve locally
if grep -q '^127\.0\.1\.1' /etc/hosts; then
  sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$NAME/" /etc/hosts
else
  printf '127.0.1.1\t%s\n' "$NAME" >>/etc/hosts
fi

if ! command -v avahi-daemon >/dev/null 2>&1; then
  echo "installing avahi-daemon"
  apt-get update -qq
  apt-get install -y avahi-daemon libnss-mdns
fi

# Announce the fixed name whatever DHCP says, on IPv4 and IPv6.
CONF=/etc/avahi/avahi-daemon.conf
if [ -f "$CONF" ]; then
  cp -n "$CONF" "$CONF.orig"
  if grep -q '^#\?host-name=' "$CONF"; then
    sed -i "s/^#\?host-name=.*/host-name=$NAME/" "$CONF"
  else
    sed -i "s/^\[server\]/[server]\nhost-name=$NAME/" "$CONF"
  fi
  sed -i 's/^#\?use-ipv4=.*/use-ipv4=yes/; s/^#\?use-ipv6=.*/use-ipv6=yes/' "$CONF"
fi

systemctl enable avahi-daemon >/dev/null 2>&1 || true
systemctl restart avahi-daemon

sleep 2
echo
if systemctl is-active --quiet avahi-daemon; then
  echo "done: open http://$NAME.local"
  echo "(an SSH session opened as $OLD.local keeps working until it is closed;"
  echo " from now on connect to $NAME.local)"
else
  echo "avahi-daemon is not running: sudo journalctl -u avahi-daemon -n 30" >&2
  exit 1
fi
