#!/usr/bin/env bash
# Re-provision the two UE subscribers to the standard eMBB slice (SST=1,
# sd=0xFFFFFF, dnn "oai") in the running OAI CN mysql.
#
# Why: docker-compose-basic-nrf.yaml mounts database/oai_db2.sql, which ships
# these subscribers on an operator-specific slice (SST=222 sd=00007B dnn
# "default"). nr-uesoftmodem's conf validation rejects nssai_sst > 4, so a
# PDU session can't be requested on SST=222 -> no tun -> no user-plane data.
# SST=1 is what the paper uses and what OAI accepts.
#
# Idempotent. Invoked automatically after core start (SystemController) and
# safe to run by hand. Persists in the mysql named volume across normal
# down/up; only a fresh volume (down -v) reloads the original SQL, after which
# this restores the correct slice.

# Never trace credentials, even when invoked through bash -x.
set +x
set -u
: "${AIC_MYSQL_PASSWORD:?Set AIC_MYSQL_PASSWORD before provisioning subscribers}"
# Docker inherits this named variable; neither Docker nor mysql argv contains
# the password. The running database must already use this operator-set value.
export MYSQL_PWD="$AIC_MYSQL_PASSWORD"
IMSIS=("208950000000031" "208950000000032")
NSSAI='{"sd": "FFFFFF", "sst": 1}'
DNNCFG='{"oai": {"sscModes": {"defaultSscMode": "SSC_MODE_1"}, "sessionAmbr": {"uplink": "1000Mbps", "downlink": "1000Mbps"}, "5gQosProfile": {"5qi": 6, "arp": {"preemptCap": "NOT_PREEMPT", "preemptVuln": "NOT_PREEMPTABLE", "priorityLevel": 1}, "priorityLevel": 1}, "pduSessionTypes": {"defaultSessionType": "IPV4"}}}'

# wait for mysql to be ready (up to ~30s)
for i in $(seq 1 30); do
    docker exec --env MYSQL_PWD mysql mysqladmin -u root ping >/dev/null 2>&1 && break
    sleep 1
done

for imsi in "${IMSIS[@]}"; do
    docker exec --env MYSQL_PWD mysql mysql -u root -D oai_db -e "
        UPDATE SessionManagementSubscriptionData
        SET singleNssai='${NSSAI}', dnnConfigurations='${DNNCFG}'
        WHERE ueid='${imsi}';" 2>/dev/null
done

echo "provisioned subscribers: ${IMSIS[*]} -> SST=1 sd=FFFFFF dnn oai"
docker exec --env MYSQL_PWD mysql mysql -u root -D oai_db -N -e \
    "SELECT ueid, singleNssai FROM SessionManagementSubscriptionData WHERE ueid IN ('${IMSIS[0]}','${IMSIS[1]}');" 2>/dev/null
