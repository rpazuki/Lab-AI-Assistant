#!/bin/sh
set -eu
# Upstream contains active LocalSocket entries. Appending another entry leaves
# the first one effective, so replace them before the upstream init starts clamd.
sed -i '/^LocalSocket /d; /^LocalSocketMode /d' /etc/clamav/clamd.conf
printf '\nLocalSocket /run/clamav/clamd.sock\nLocalSocketMode 666\n' >> /etc/clamav/clamd.conf
chown clamav:clamav /run/clamav
exec /init
