"""Hermetic checks for the hardware-lane readiness receipt."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "hardware" / "readiness.py"
SPEC = importlib.util.spec_from_file_location("hardware_readiness", MODULE)
readiness = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = readiness
SPEC.loader.exec_module(readiness)


class HardwareReadinessTests(unittest.TestCase):
    def test_ready_requires_every_required_item(self) -> None:
        items = {"one": {"required": True, "state": readiness.READY},
                 "optional": {"required": False, "state": readiness.UNKNOWN}}
        self.assertEqual(readiness.READY, readiness.overall_state(items))
        items["one"]["state"] = readiness.NOT_READY
        self.assertEqual(readiness.NOT_READY, readiness.overall_state(items))
        items["one"]["state"] = readiness.UNKNOWN
        self.assertEqual(readiness.NOT_READY, readiness.overall_state(items))

    def test_fake_probe_reports_reason_and_never_claims_ready_without_witness(self) -> None:
        def fake(argv: list[str]) -> readiness.CommandResult:
            if argv[:3] == ["docker", "inspect", "oran-aic-nearrt-ric"]:
                return readiness.CommandResult(0, json.dumps([{"State": {"Running": True}, "Config": {"Env": []}}]))
            if argv == ["ss", "-S", "-a", "-n"]:
                return readiness.CommandResult(0, "LISTEN 0 5 192.168.50.1:36421\nESTAB 0 0 192.168.50.1:36421 peer")
            if argv[:3] == ["docker", "exec", "oran-aic-nearrt-ric"]:
                return readiness.CommandResult(1, "", "witness absent")
            return readiness.CommandResult(1, "", "not available")

        receipt = readiness.collect_readiness(command=fake, tcp_probe=lambda _h, _p: False,
                                              binding_loader=lambda _path: (_ for _ in ()).throw(RuntimeError("fake binding unavailable")),
                                              settings=readiness.Settings(live_artifact_root=""))
        item = receipt["readiness"]["items"]["ric_e2"]
        self.assertEqual(readiness.NOT_READY, item["state"])
        self.assertIn("witness", item["reason"])
        self.assertEqual(readiness.NOT_READY, receipt["readiness"]["state"])

    def test_receipt_secret_scan(self) -> None:
        value = readiness._secret_free({"reason": "Ki=abc OPc=def IMSI=123 password=hunter2 token=x",
                                        "nested": [{"ok": "safe"}], "oauthToken": "must vanish"})
        text = json.dumps(value)
        for forbidden in ("Ki=", "OPc=", "IMSI=", "password=", "token="):
            self.assertNotIn(forbidden.lower(), text.lower())
        self.assertNotIn("oauthToken", text)
        self.assertTrue(readiness._secret_free({"secretFree": True})["secretFree"])

    def test_live_artifact_root_prefers_current_name_and_reads_legacy_once(self) -> None:
        with patch.dict(os.environ, {"HW_LIVE_ARTIFACT_ROOT": "/current",
                                    "LOWER_LIVE": "/legacy"}, clear=True):
            self.assertEqual("/current", readiness.Settings.from_environment().live_artifact_root)
        with patch.dict(os.environ, {"LOWER_LIVE": "/legacy"}, clear=True):
            self.assertEqual("/legacy", readiness.Settings.from_environment().live_artifact_root)

    def test_producer_log_is_scoped_to_rfc3339_started_at(self) -> None:
        started = "2026-09-04T09:47:37.911655861Z"
        producer = {"State": {"Running": True, "StartedAt": started}, "Config": {"Env": []}, "Mounts": []}
        seen: list[list[str]] = []

        def fake(argv: list[str]) -> readiness.CommandResult:
            seen.append(list(argv))
            if argv[:2] == ["docker", "inspect"]:
                return readiness.CommandResult(0, json.dumps([producer]))
            if argv[:3] == ["docker", "logs", "--since"]:
                self.assertEqual(started, argv[3])
                return readiness.CommandResult(0, "xApp mode is live FlexRIC\n")
            return readiness.CommandResult(1, "", "unavailable")

        receipt = readiness.collect_readiness(command=fake, tcp_probe=lambda _h, _p: True,
                                              binding_loader=lambda _path: (_ for _ in ()).throw(RuntimeError("fake")),
                                              settings=readiness.Settings(live_artifact_root=""))
        self.assertEqual(readiness.READY, receipt["readiness"]["items"]["a1p_r1"]["state"])
        self.assertTrue(any(call[:3] == ["docker", "logs", "--since"] for call in seen))

    def test_sctp_listener_requires_associations(self) -> None:
        witness = {"producerPid": os.getpid(), "connections": [
            {"active": True, "connectionEpoch": 272, "globalE2NodeId": {"nbId": 3584}},
            {"active": True, "connectionEpoch": 273, "globalE2NodeId": {"nbId": 2816}},
        ]}

        def fake(argv: list[str]) -> readiness.CommandResult:
            if argv[:2] == ["docker", "inspect"]:
                return readiness.CommandResult(0, json.dumps([{"State": {"Running": True}, "Config": {"Env": []}}]))
            if argv[:3] == ["docker", "exec", "oran-aic-nearrt-ric"]:
                return readiness.CommandResult(0, json.dumps(witness))
            if argv == ["ss", "-S", "-a", "-n"]:
                return readiness.CommandResult(0, "LISTEN 0 32 192.168.50.1:36421\nESTAB 0 0 192.168.50.1:36421 one\nESTAB 0 0 192.168.50.1:36421 two")
            return readiness.CommandResult(1, "", "unavailable")

        receipt = readiness.collect_readiness(command=fake, tcp_probe=lambda _h, _p: False,
                                              binding_loader=lambda _path: (_ for _ in ()).throw(RuntimeError("fake")),
                                              settings=readiness.Settings(live_artifact_root=""))
        self.assertEqual(readiness.READY, receipt["readiness"]["items"]["ric_e2"]["state"])

    def test_repin_rotates_kpm_stream_before_head_validation(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "scripts" / "hardware" / "repin_a1p.sh").read_text()
        self.assertIn('KPM_ROTATED_BACKUP="$KPM_JSONL.pre-rotate-$RUN_ID"', script)
        self.assertLess(script.index('run mv "$KPM_JSONL" "$KPM_ROTATED_BACKUP"'),
                        script.index('run docker run -d --name "$GATE"'))
        self.assertIn('if i >= 16: break', script)
        self.assertIn('FRESH_STREAM_TS=', script)

    def test_kpm_ue_attribution_counts_only_fresh_attributed_indications(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            jsonl = Path(directory) / "kpm.jsonl"
            jsonl.write_text("\n".join((
                json.dumps({"event": "kpm_indication", "ues": []}),
                json.dumps({"event": "other", "ues": [{"ue": "one"}]}),
                json.dumps({"event": "kpm_indication", "ues": [{"ue": "one"}]}),
                json.dumps({"event": "kpm_indication", "ues": [{"ue": "two"}]}),
            )), encoding="utf-8")
            self.assertEqual((2, None), readiness._kpm_ue_attribution(str(jsonl), 4))
            self.assertEqual((1, None), readiness._kpm_ue_attribution(str(jsonl), 1))

    def test_ue_dl_liveness_uses_external_dn_tun_ping(self) -> None:
        commands: list[list[str]] = []

        def fake(argv: list[str]) -> readiness.CommandResult:
            commands.append(list(argv))
            remote = argv[-1] if argv and argv[0] == "ssh" else ""
            if "ip -4 -o addr show up dev oaitun_ue1" in remote:
                return readiness.CommandResult(0, "50: oaitun_ue1    inet 12.1.1.132/24 scope global\n")
            if "pgrep -x nr-uesoftmodem" in remote:
                return readiness.CommandResult(0, "100\n")
            if "ping -I oaitun_ue1" in remote:
                return readiness.CommandResult(0)
            if argv == ["docker", "exec", "oai-ext-dn", "ping", "-c", "1", "-W", "2", "12.1.1.132"]:
                return readiness.CommandResult(0)
            return readiness.CommandResult(1, "", "not available")

        receipt = readiness.collect_readiness(
            command=fake,
            tcp_probe=lambda _host, _port: False,
            binding_loader=lambda _path: (_ for _ in ()).throw(RuntimeError("fake")),
            settings=readiness.Settings(
                live_artifact_root="", ue_hosts={"ue1": "ue1"}, ue_prime_target="192.168.70.135",
                ext_dn_container="oai-ext-dn", required_ues=frozenset(("ue1",)), gnb={},
            ),
        )
        self.assertEqual(readiness.READY, receipt["readiness"]["items"]["ue_dl_liveness"]["state"])
        self.assertIn(["docker", "exec", "oai-ext-dn", "ping", "-c", "1", "-W", "2", "12.1.1.132"], commands)

    def _worker_receipt(self, log: str, command_state: str | None = None) -> dict[str, object]:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "a1p.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute("create table policies (policy_id text, policy_json text, status_json text)")
            connection.execute("create table commands (sequence integer, state text, created_at text, updated_at text)")
            if command_state:
                connection.execute("insert into commands values (?, ?, ?, ?)", (7, command_state, "900", "939"))
            connection.commit()
            connection.close()
            producer = {
                "State": {"Running": True, "StartedAt": "2026-09-04T10:00:00Z"},
                "Config": {"Cmd": ["python3", "--db", "/state/a1p.sqlite3"], "Env": []},
                "Mounts": [{"Source": directory, "Destination": "/state"}],
            }

            def fake(argv: list[str]) -> readiness.CommandResult:
                if argv == ["docker", "inspect", "oran-aic-a1p-producer"]:
                    return readiness.CommandResult(0, json.dumps([producer]))
                if argv[:3] == ["docker", "logs", "--since"]:
                    return readiness.CommandResult(0, log)
                return readiness.CommandResult(1, "", "not available")

            return readiness.collect_readiness(
                command=fake, tcp_probe=lambda _host, _port: True,
                binding_loader=lambda _path: (_ for _ in ()).throw(RuntimeError("fake")),
                settings=readiness.Settings(live_artifact_root=""), now=1000,
            )

    def test_a1p_worker_detects_scoped_worker_stopped_log(self) -> None:
        receipt = self._worker_receipt("xApp mode is live FlexRIC\nxApp worker stopped\n")
        item = receipt["readiness"]["items"]["a1p_worker"]
        self.assertEqual(readiness.NOT_READY, item["state"])
        self.assertIn("xApp worker stopped", item["reason"])
        self.assertIn("restart the producer (docker restart oran-aic-a1p-producer) and re-run readiness", item["reason"])

    def test_a1p_worker_detects_stale_recovery_or_sent_command(self) -> None:
        for state in ("RECOVERY", "SENT"):
            with self.subTest(state=state):
                receipt = self._worker_receipt("xApp mode is live FlexRIC\n", state)
                item = receipt["readiness"]["items"]["a1p_worker"]
                self.assertEqual(readiness.NOT_READY, item["state"])
                self.assertIn(f"stale {state}", item["reason"])
                self.assertIn("restart the producer (docker restart oran-aic-a1p-producer) and re-run readiness", item["reason"])


if __name__ == "__main__":
    unittest.main()
