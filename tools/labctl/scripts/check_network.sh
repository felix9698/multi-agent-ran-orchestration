#!/usr/bin/env bash
set -euo pipefail

# A configured witness is not a network observation.  Only the live probe
# path may report readiness after an actual connectivity check.
printf 'NETWORK_UNKNOWN reason=no-live-probe-in-hardware-free-lane\n'
exit 3
