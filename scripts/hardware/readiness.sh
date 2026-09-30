#!/bin/bash
# Write a read-only, secret-free LIVE readiness receipt.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -f "$HERE/env.sh" ] && source "$HERE/env.sh" || source "$HERE/env.sh.example"
exec python3 "$HERE/readiness.py" "$@"
