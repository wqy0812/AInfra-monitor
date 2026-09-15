#!/bin/sh
set -eu
IPT=/usr/sbin/iptables
CHAIN=PERSES_ACCESS
if [ "${1:-apply}" = remove ]; then
 while "$IPT" -w -C INPUT -p tcp --dport 18431 -j "$CHAIN" 2>/dev/null; do "$IPT" -w -D INPUT -p tcp --dport 18431 -j "$CHAIN"; done
 "$IPT" -w -F "$CHAIN"
 "$IPT" -w -X "$CHAIN"
 exit 0
fi
"$IPT" -w -N "$CHAIN" 2>/dev/null || "$IPT" -w -S "$CHAIN" >/dev/null
/usr/sbin/iptables-restore --wait 5 --noflush <<'RULES'
*filter
-F PERSES_ACCESS
-A PERSES_ACCESS -s 127.0.0.0/8 -j ACCEPT
-A PERSES_ACCESS -s 122.0.0.0/8 -j ACCEPT
-A PERSES_ACCESS -j DROP
COMMIT
RULES
"$IPT" -w -C INPUT -p tcp --dport 18431 -j "$CHAIN" 2>/dev/null || "$IPT" -w -I INPUT 1 -p tcp --dport 18431 -j "$CHAIN"
