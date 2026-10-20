#!/bin/bash
# Map ugc.test -> your own machine so you can open http://ugc.test:1895
# Run once with:   sudo bash setup_ugc_test.sh
set -e

HOSTS=/etc/hosts
if grep -qE "^[^#]*[[:space:]]ugc\.test([[:space:]]|\$)" "$HOSTS"; then
  echo "ugc.test is already in $HOSTS — nothing to do."
else
  printf "\n127.0.0.1\tugc.test\n" >> "$HOSTS"
  echo "Added '127.0.0.1 ugc.test' to $HOSTS"
fi

# Refresh the DNS cache so the change takes effect immediately.
dscacheutil -flushcache 2>/dev/null || true
killall -HUP mDNSResponder 2>/dev/null || true

echo "Done. Open  http://ugc.test:1895"
