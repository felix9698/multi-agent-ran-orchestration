"""v5 (2026-09-26): one RF write per 15 s on a cell's transmit power.

The gNB's E2SM-RC Action 104 refuses a write within 15 000 ms of the previous one
(oai_patches/e2sm_rc_power_action104.patch, NR_RU_TX_ATT_COOLDOWN).  Every writer --
a board's apply and its rollback, the producer's expiry restore, live_run -- ends in the
Campaign 5 live worker, which stamps each Action-104 write here; a client about to send a
power CREATE/UPDATE/DELETE waits out the rest of the interval *before* its HTTP call, so
the wait never eats the R1 request timeout.  Wall clock, one host (producer and boards
both run on PC1).
"""
from __future__ import annotations

import os
import time
from pathlib import Path

POWER_POLICY_TYPE = "AIC_CellDlTxPower_1.0.0"
#: gNB minimum RF write interval plus one second of margin.
POWER_WRITE_INTERVAL_S = 16.0


def stamp_path() -> Path | None:
    """The shared stamp; an empty ``AIC_POWER_WRITE_STAMP`` disables spacing (hermetic tests)."""
    value = os.environ.get("AIC_POWER_WRITE_STAMP", "/tmp/aic-power-last-write")
    return Path(value) if value else None


def mark_power_write(now: float | None = None) -> None:
    path = stamp_path()
    if path is None:
        return
    try:
        # Atomic (Codex review 2026-09-26): a reader never sees a truncated stamp.
        tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
        tmp.write_text(f"{time.time() if now is None else now:.3f}\n")
        os.replace(tmp, path)
    except OSError:
        pass          # a missing stamp only costs the next writer its wait


def seconds_since_power_write(now: float | None = None) -> float | None:
    """Seconds since the last recorded power write, or None when none is recorded."""
    path = stamp_path()
    if path is None:
        return None
    try:
        last = float(path.read_text().strip())
    except (OSError, ValueError):
        return None
    return (time.time() if now is None else now) - last


def power_write_wait_s(now: float | None = None) -> float:
    """Seconds until the next power write may be sent (0 when none is recorded)."""
    path = stamp_path()
    if path is None:
        return 0.0
    try:
        last = float(path.read_text().strip())
    except (OSError, ValueError):
        return 0.0
    return max(0.0, last + POWER_WRITE_INTERVAL_S - (time.time() if now is None else now))
