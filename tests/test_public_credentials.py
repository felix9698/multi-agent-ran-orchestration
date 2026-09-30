"""Publication credential boundaries; all hardware/DB operations are faked."""

import ast
import os
from pathlib import Path
import re
import subprocess
import unittest
from unittest.mock import patch

from config import Config, NetworkConfig, UEConfig
from executor.system_controller import ComponentStatus, SystemController


ROOT = Path(__file__).resolve().parents[1]
DEMO_KEY = "12" * 16
DEMO_OPC = "aB" * 16


def controller():
    network = NetworkConfig(ues={
        "ue1": UEConfig(
            id="ue1", hostname="ue.example", ip="192.0.2.10",
            ssh_user="operator", usrp_type="b206mini",
            initial_serving_gnb="gnb1", imsi="001010000000001"),
    })
    return SystemController(network_config=Config(network=network))


class PublicUiccCredentialsTest(unittest.TestCase):
    def test_missing_credentials_do_not_block_config_but_refuse_launch(self):
        with patch.dict(os.environ, {}, clear=True):
            control = controller()
            control.set_prb_config(51)
            control.set_scope(False)
            with patch("executor.system_controller.subprocess.run") as run, \
                    patch("executor.system_controller.threading.Thread") as thread:
                run.return_value = subprocess.CompletedProcess([], 1, "", "offline")
                self.assertFalse(control.start_component("ue1", async_start=False))
                self.assertFalse(control.start_component("ue1", async_start=True))
                self.assertEqual(len(run.call_args_list), 0)
                self.assertEqual(len(thread.call_args_list), 0)
            self.assertEqual(control.status["ue1"], ComponentStatus.ERROR)

    def test_malformed_or_all_zero_credentials_refuse_before_hardware(self):
        for variable in ("AIC_UICC_KEY", "AIC_UICC_OPC"):
            for invalid in ("", "0" * 32, "1" * 31, "g" * 32,
                            DEMO_KEY + "\n", '"; exit 0; #'):
                with self.subTest(variable=variable, length=len(invalid)):
                    env = {"AIC_UICC_KEY": DEMO_KEY, "AIC_UICC_OPC": DEMO_OPC}
                    env[variable] = invalid
                    with patch.dict(os.environ, env, clear=True):
                        control = controller()
                        with patch("executor.system_controller.subprocess.run") as run:
                            run.return_value = subprocess.CompletedProcess([], 1, "", "offline")
                            self.assertFalse(control.start_component("ue1", async_start=False))
                            self.assertEqual(len(run.call_args_list), 0)

    def test_credentials_are_resolved_at_launch_not_stored_in_config(self):
        with patch.dict(os.environ, {}, clear=True):
            control = controller()
        env = {"AIC_UICC_KEY": DEMO_KEY, "AIC_UICC_OPC": DEMO_OPC}
        with patch.dict(os.environ, env, clear=True), \
                patch("executor.system_controller.subprocess.run") as run, \
                patch("executor.system_controller.time.sleep"):
            run.side_effect = [subprocess.CompletedProcess([], 0, "", ""),
                               subprocess.CompletedProcess([], 0, "42", "")]
            self.assertTrue(control.start_component("ue1", async_start=False))
            command = run.call_args_list[0].args[0][-1]
            self.assertTrue(f'key = "{DEMO_KEY}";' in command,
                            "launch must use the operator's key")
            self.assertTrue(f'opc = "{DEMO_OPC}";' in command,
                            "launch must use the operator's OPc")
            self.assertTrue("-r 24 --numerology 1 --band 78 -C 3349920000 --ssb 24" in command)
            self.assertFalse(DEMO_KEY in control.config.ues["ue1"].start_cmd)
            self.assertFalse(DEMO_OPC in control.config.ues["ue1"].start_cmd)

    def test_launch_error_does_not_log_credentials(self):
        env = {"AIC_UICC_KEY": DEMO_KEY, "AIC_UICC_OPC": DEMO_OPC}
        with patch.dict(os.environ, env, clear=True):
            control = controller()
            messages = []
            control.on_log = lambda name, message: messages.append(message)
            with patch("executor.system_controller.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess(
                    [], 1, "", f"fake diagnostic {DEMO_KEY} {DEMO_OPC}")
                with self.assertLogs("SystemController", level="INFO") as logs:
                    self.assertFalse(control.start_component("ue1", async_start=False))
            output = "\n".join(messages + logs.output)
            self.assertFalse(DEMO_KEY in output)
            self.assertFalse(DEMO_OPC in output)

    def test_non_ue_start_does_not_require_uicc_credentials(self):
        with patch.dict(os.environ, {}, clear=True), \
                patch("executor.system_controller.subprocess.run") as run, \
                patch("executor.system_controller.time.sleep"):
            run.side_effect = [subprocess.CompletedProcess([], 0, "", ""),
                               subprocess.CompletedProcess([], 0, "42", "")]
            self.assertTrue(controller().start_component("gnb1", async_start=False))

    def test_published_controller_and_example_embed_no_uicc_key_material(self):
        # Structural assertion: never record the original credential in a test.
        tree = ast.parse((ROOT / "executor/system_controller.py").read_text())
        values = (node.value for node in ast.walk(tree)
                  if isinstance(node, ast.Constant) and isinstance(node.value, str))
        self.assertFalse(any(re.fullmatch(r"[0-9a-fA-F]{32}", value) for value in values),
                         "controller contains embedded 128-bit key material")
        example = (ROOT / "configs/nrue.conf").read_text()
        self.assertFalse(bool(re.search(r'\b(?:key|opc)\s*=\s*"[0-9a-fA-F]{32}"', example)),
                         "example contains deployable UICC credentials")


class PublicMysqlCredentialsTest(unittest.TestCase):
    def run_provisioner(self, password=None, *, trace=False):
        # Real shell logic, with Docker and sleep replaced at the process boundary.
        # Descriptor 3 bypasses the script's command redirection, without secrets.
        shell = r'''
exec 3>&1
docker() {
    printf 'DOCKER_CALLED\n' >&3
    [ "${MYSQL_PWD-}" = "$EXPECTED_PASSWORD" ] || return 91
    for arg in "$@"; do
        case "$arg" in
            *"$EXPECTED_PASSWORD"*|-p*) return 92 ;;
        esac
    done
    [ "$1" = exec ] && [ "$2" = --env ] && [ "$3" = MYSQL_PWD ] || return 93
    printf 'SAFE_DOCKER_ENV\n' >&3
}
sleep() { :; }
source "$1"
'''
        env = {"PATH": "/usr/bin:/bin", "EXPECTED_PASSWORD": "demo-db-'$-password"}
        if password is not None:
            env["AIC_MYSQL_PASSWORD"] = password
        argv = ["/bin/bash"] + (["-x"] if trace else []) + ["-c", shell, "test",
                  str(ROOT / "scripts/provision_subscribers.sh")]
        return subprocess.run(argv, env=env, capture_output=True, text=True, timeout=5)

    def test_missing_or_empty_mysql_password_refuses_before_docker(self):
        for password in (None, ""):
            with self.subTest(missing=password is None):
                result = self.run_provisioner(password)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse("DOCKER_CALLED" in result.stdout)

    def test_mysql_password_uses_docker_environment_not_arguments_or_logs(self):
        password = "demo-db-'$-password"
        result = self.run_provisioner(password, trace=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.count("SAFE_DOCKER_ENV"), 4)
        self.assertFalse(password in result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
