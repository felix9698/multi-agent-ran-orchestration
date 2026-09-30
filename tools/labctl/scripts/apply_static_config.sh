#!/usr/bin/env bash
set -euo pipefail

test -r configs/oai/gnb.sa.band78.fr1.24PRB.pc1.conf
test -r configs/oai/gnb.sa.band78.fr1.24PRB.pc2.conf
test -r configs/nrue.conf
printf 'STATIC_CONFIG_STAGED_NOT_RADIO_APPLIED source=repository-configs\n'
