#!/usr/bin/env bash
set -euo pipefail

# A configured witness is not a hardware observation.  The live-only probe
# path may replace this script with a device query; this distributed default
# must remain honest when no such query is available.
printf 'USRP_UNKNOWN reason=no-live-probe-in-hardware-free-lane\n'
exit 3
