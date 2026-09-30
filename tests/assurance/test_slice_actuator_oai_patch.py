"""Static and executable harness checks for the immutable-source OAI patch."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "oai_patches" / "e2sm_rc_style2_action6_slice_prb.patch"
# Host-specific checkout paths are supplied by the environment, never hard-coded,
# so the tests stay portable (tests/test_portability.py B2). When the pinned OAI
# and FlexRIC source trees are not configured, the executable compile checks below
# skip via ``skipUnless(...is_dir())``; the static patch checks still run.
OAI_SOURCE = Path(os.environ.get("AIC_OAI_SOURCE", str(ROOT / ".oai-source-not-configured")))
FLEXRIC_SOURCE = Path(os.environ.get("AIC_FLEXRIC_SOURCE", str(ROOT / ".flexric-source-not-configured")))
OAI_BUILD = OAI_SOURCE / "cmake_targets" / "ran_build" / "build"
HARNESS = ROOT / "src" / "xapp" / "flexric_adapter" / "tests" / "slice_prb_control_harness.cc"
OAI_HARNESS = ROOT / "src" / "oai" / "tests" / "slice_prb_quota_harness.c"
PORT = ROOT / "src" / "xapp" / "flexric_adapter" / "src" / "rc_control_port.cc"
FLEXRIC_ENCODER = ROOT / "src" / "xapp" / "flexric_adapter" / "src" / "flexric_style2_action6_encoder.c"
FLEXRIC_ADAPTER = ROOT / "src" / "xapp" / "flexric_adapter" / "src" / "flexric_control_adapter.cc"
NATIVE_HARNESS = ROOT / "src" / "xapp" / "flexric_adapter" / "tests" / "native_flexric_control_harness.cc"
INCLUDE = ROOT / "src" / "xapp" / "flexric_adapter" / "include"


class OaiPatchContract(unittest.TestCase):
    def test_patch_contains_advertisement_parser_apply_and_previous_readback(self) -> None:
        text = PATCH.read_text(encoding="utf-8")
        for needle in (
            "RRM Policy Ratio List", "RRM Policy Ratio Group", "RRM Policy Member List",
            "S-NSSAI", "Min PRB Policy Ratio", "Max PRB Policy Ratio",
            "Dedicated PRB Policy Ratio", "RC_STYLE_2_RADIO_RESOURCE_ALLOCATION",
            "nr_mac_set_slice_prb_quotas", "nr_mac_get_slice_prb_quotas",
            "nr_mac_get_previous_slice_prb_quotas", "previous", "Action 6",
            "CTRL_FAILURE_SM_AG_IF_ANS_V0", "CONTROL FAILURE tx",
            "if (max_rbs[j] < min_rbSize)",
            "member->sz_ran_param_struct, quota, &why",
            "no restorable slice quota baseline",
            "OAI_RC_STYLE2_BASELINE_BOOTSTRAP",
            "rc_style2_matches_bootstrap(quotas, quota_count, configured_bootstrap)",
            "setup-only baseline bootstrap",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    def test_forked_control_port_harness_compiles_and_runs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="slice-act-harness-") as temp:
            binary = Path(temp) / "slice_prb_control_harness"
            subprocess.run([
                "c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                "-I", str(INCLUDE), str(PORT), str(HARNESS), "-o", str(binary),
            ], check=True, cwd=ROOT)
            completed = subprocess.run([str(binary)], check=True, text=True, capture_output=True)
            self.assertIn("normal:PASS", completed.stdout)
            self.assertIn("missing-snssai:PASS", completed.stdout)
            self.assertIn("invalid-sst:PASS", completed.stdout)
            self.assertIn("rollback:PASS", completed.stdout)

    def test_oai_quota_harness_exercises_atomic_apply_and_exact_restore(self) -> None:
        text = OAI_HARNESS.read_text(encoding="utf-8")
        for needle in (
            "nr_mac_set_slice_prb_quotas", "nr_mac_get_slice_prb_quotas",
            "nr_mac_get_previous_slice_prb_quotas", "apply:PASS",
            "malformed:PASS", "rollback:PASS",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, text)

    @unittest.skipUnless(
        OAI_SOURCE.is_dir() and (OAI_BUILD / "build.ninja").is_file(),
        "pinned OAI source/build graph is unavailable",
    )
    def test_patched_oai_quota_unit_applies_rejects_and_rolls_back(self) -> None:
        scheduler = Path("openair2/LAYER2/NR_MAC_gNB/gNB_scheduler_dlsch_default_policies.c")
        copied_paths = (
            scheduler,
            scheduler.with_suffix(".h"),
            Path("openair2/LAYER2/NR_MAC_gNB/nr_mac_gNB.h"),
        )
        with tempfile.TemporaryDirectory(prefix="slice-act-oai-unit-") as temp_name:
            temp = Path(temp_name)
            for relative in copied_paths:
                destination = temp / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(OAI_SOURCE / relative, destination)
            apply_command = ["git", "apply"]
            for relative in copied_paths:
                apply_command.append(f"--include={relative}")
            subprocess.run(apply_command + [str(PATCH)], check=True, cwd=temp)

            ninja = subprocess.run(
                ["ninja", "-C", str(OAI_BUILD), "-t", "commands"],
                check=True, text=True, capture_output=True,
            ).stdout
            command = next(
                line for line in ninja.splitlines()
                if line.endswith(str(OAI_SOURCE / scheduler))
            )

            def compile_unit(source: Path, output: Path) -> None:
                arguments = shlex.split(command)
                if Path(arguments[0]).name == "ccache":
                    arguments.pop(0)
                arguments[arguments.index("-o") + 1] = str(output)
                arguments[arguments.index("-MT") + 1] = str(output)
                arguments[arguments.index("-MF") + 1] = str(output.with_suffix(".d"))
                arguments[-1] = str(source)
                arguments[1:1] = [
                    "-ffunction-sections", "-fdata-sections",
                    "-I", str(temp / "openair2"),
                    "-I", str(OAI_SOURCE / "openair2/LAYER2/NR_MAC_gNB"),
                ]
                subprocess.run(arguments, check=True, cwd=ROOT)

            scheduler_object = temp / "scheduler.o"
            harness_object = temp / "harness.o"
            compile_unit(temp / scheduler, scheduler_object)
            compile_unit(OAI_HARNESS, harness_object)
            binary = temp / "slice_prb_quota_harness"
            subprocess.run([
                "cc", "-Wl,--gc-sections", str(scheduler_object),
                str(harness_object), "-lpthread", "-o", str(binary),
            ], check=True, cwd=ROOT)
            completed = subprocess.run(
                [str(binary)], check=True, text=True, capture_output=True,
            )
            self.assertEqual(
                completed.stdout.splitlines(),
                ["apply:PASS", "malformed:PASS", "rollback:PASS"],
            )

    @unittest.skipUnless(FLEXRIC_SOURCE.is_dir(), "pinned FlexRIC source is unavailable")
    def test_native_flexric_encoder_compiles_against_the_pinned_ir(self) -> None:
        subprocess.run([
            "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
            "-I", str(FLEXRIC_SOURCE), "-I", str(INCLUDE),
            "-fsyntax-only", str(FLEXRIC_ENCODER),
        ], check=True, cwd=ROOT)

    @unittest.skipUnless(FLEXRIC_SOURCE.is_dir(), "pinned FlexRIC source is unavailable")
    def test_control_port_invokes_native_flexric_builder_and_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory(prefix="slice-act-native-") as temp:
            encoder_object = Path(temp) / "encoder.o"
            binary = Path(temp) / "native_flexric_control_harness"
            subprocess.run([
                "cc", "-std=c11", "-Wall", "-Wextra", "-Werror",
                "-I", str(FLEXRIC_SOURCE), "-I", str(INCLUDE),
                "-c", str(FLEXRIC_ENCODER), "-o", str(encoder_object),
            ], check=True, cwd=ROOT)
            subprocess.run([
                "c++", "-std=c++17", "-Wall", "-Wextra", "-Werror",
                "-I", str(FLEXRIC_SOURCE), "-I", str(INCLUDE),
                str(PORT), str(FLEXRIC_ADAPTER), str(NATIVE_HARNESS),
                str(encoder_object), "-o", str(binary),
            ], check=True, cwd=ROOT)
            completed = subprocess.run(
                [str(binary)], check=True, text=True, capture_output=True,
            )
            self.assertEqual(completed.stdout.strip(), "native-chain:PASS")

    @unittest.skipUnless(OAI_SOURCE.is_dir(), "pinned OAI source is unavailable")
    def test_patch_applies_cleanly_to_the_immutable_oai_source(self) -> None:
        subprocess.run([
            "git", "apply", "--check", str(PATCH),
        ], check=True, cwd=OAI_SOURCE)


if __name__ == "__main__":
    unittest.main()
