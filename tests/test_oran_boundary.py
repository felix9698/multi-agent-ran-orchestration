import ast
import unittest
from pathlib import Path


def _module_file(name):
    """The repository file a dotted module name resolves to, if any."""
    candidate = Path(*name.split(".")).with_suffix(".py")
    if candidate.is_file():
        return candidate
    package = Path(*name.split(".")) / "__init__.py"
    return package if package.is_file() else None


def walk_imports(entries):
    """Return (visited files, imported dotted names, importer edges).

    ``from package import module`` is resolved to the submodule as well as to
    the package, which the older walk below does not do.  Without it a module
    reached only through ``from oran.rapp import status_projection`` would be
    invisible to the boundary proof - and a transport hidden behind exactly that
    form is the one worth finding.
    """
    pending = [Path(entry) for entry in entries]
    visited, imported = set(), set()
    #: importer path -> the dotted names that file itself imports.  The flat
    #: ``imported`` set says *whether* a name is reachable; a bounded exemption
    #: needs *who* reaches it, or the exemption is a blanket one.
    edges: dict = {}
    while pending:
        path = pending.pop()
        if path in visited or not path.is_file():
            continue
        visited.add(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        package = list(path.with_suffix("").parts[:-1])
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    prefix = package[:len(package) - node.level + 1]
                    module = ".".join(prefix + ([node.module] if node.module else []))
                else:
                    module = node.module or ""
                names = [module]
                if module:
                    names += [f"{module}.{alias.name}" for alias in node.names]
            for name in names:
                if not name:
                    continue
                imported.add(name)
                edges.setdefault(str(path), set()).add(name)
                resolved = _module_file(name)
                if resolved is not None:
                    pending.append(resolved)
    return visited, imported, edges


#: Spelled in two halves so this gate never matches itself when it scans the
#: tree for the name.  A gate that has to exempt its own file has a hole the
#: width of that exemption.
NORTHBOUND = "ts_" + "northbound"


class OranBoundaryTests(unittest.TestCase):
    BANNED_MODULES = {
        "executor.oai_executor", "collectors.multi_ue_collector",
        "executor.system_controller", "telnetlib", "paramiko",
    }

    #: The entries an operator intent can actually be submitted through in the
    #: final composition.  The development mock profile and the offline
    #: experiment runners are deliberately *not* here: they are separate
    #: surfaces, and including them would let their dependencies excuse a
    #: transport in the Live path.
    FINAL_COMPOSITION_ENTRIES = (
        "oran/rapp/headless.py",
        "oran/rapp/gui_entry.py",
        "gui/operator/app.py",
    )

    #: Anything that could reach a RAN element without traversing R1.  The list
    #: is by prefix, so ``executor.anything`` and ``oran.mocks.anything`` are
    #: covered without having to enumerate future modules.
    FORBIDDEN_IN_FINAL_COMPOSITION = (
        # process and remote-shell control of a radio
        "subprocess", "telnetlib", "paramiko", "pty", "socket", "asyncio",
        # the legacy direct-to-OAI boundary
        "executor", "collectors",
        # the superseded custom rApp-to-xApp HTTP portal
        NORTHBOUND, f"src.xapp.{NORTHBOUND}",
        # lower-side and development stacks the console must not host or call
        "oran.mocks", "oran.profiles", "oran.nonrt", "oran.conformance",
        "oran.release",
        # offline experiment surfaces
        "experiments.runner", "experiments.synthetic", "experiments.emulation",
        "experiments.paired_runner", "gui.legacy_tools",
    )

    #: 측정면이 쓰는 범용 원시 도구의 **파일 단위** 면제 (2026-09-23).
    #:
    #: `subprocess` 와 `socket` 은 "R1 을 거치지 않고 RAN 요소에 닿을 수 있는 것" 으로
    #: 금지돼 있지만, 두 부류를 한 이름으로 묶고 있었다 -- 라디오를 **조종**하는 전송
    #: (telnet·paramiko·executor·collectors)과, 아래 세 모듈이 **재는 데** 쓰는 범용
    #: 도구.  Cockpit 이 에이전트 판을 직접 호스팅하게 되면서(B-01 cutover) 측정면이
    #: 최종 조립의 import 폐포에 들어왔고, 이 가드가 그때부터 빨갛다.
    #:
    #: 이름이 아니라 **파일**로 면제하는 이유: `"subprocess" 를 통째로 빼면` 내일
    #: 제어 경로가 `subprocess.run(["telnet", ...])` 을 해도 이 가드가 조용하다.
    #: 아래 목록에 없는 파일이 이 둘 중 하나를 import 하면 여전히 빨개진다.
    #:
    #: - kpi_observer: UE 호스트에 ssh 로 붙어 tun 의 `rx_bytes` 를 읽는다 (측정)
    #: - flow_goodput / tagged_echo: UE 위에서 도는 수신기·에코 송신원 (측정·부하)
    #:
    #: 셋 다 **KPI 를 읽을 뿐 액추에이터에 닿지 않는다**; 쓰기는 Write Gateway 가
    #: R1 으로만 한다.
    MEASUREMENT_PRIMITIVES = {
        "tools/liveconsole/kpi_observer.py": ("subprocess",),
        "tools/liveconsole/flow_goodput.py": ("socket",),
        "tools/liveconsole/tagged_echo.py": ("socket",),
    }

    def test_the_final_composition_reaches_no_direct_ran_transport(self):
        """No path from GUI or headless submit to anything below R1.

        The composition's only sanctioned transport is
        ``oran/rapp/r1_client.py``.  Everything the paper claims about the
        boundary - no direct xApp call, no FlexRIC, no E2, no gNB or UE control,
        no telnet or SSH, no surviving custom HTTP northbound - reduces to this
        import closure containing exactly one transport and none of the modules
        that could be a second one.
        """
        visited, imported, edges = walk_imports(self.FINAL_COMPOSITION_ENTRIES)

        violations = sorted(
            f"{importer} imports {name}"
            for importer, names in edges.items() for name in names
            if any(name == banned or name.startswith(banned + ".")
                   for banned in self.FORBIDDEN_IN_FINAL_COMPOSITION)
            and name.split(".")[0] not in self.MEASUREMENT_PRIMITIVES.get(importer, ()))
        self.assertEqual(violations, [],
                         "the final composition can reach a non-R1 boundary: "
                         + ", ".join(violations))

        # 면제는 **쓰이고 있을 때만** 면제다.  안 쓰는 줄이 남아 있으면 다음 사람이
        # "여기는 원래 되는 자리" 로 읽고 진짜 제어 전송을 얹는다.
        for importer, primitives in self.MEASUREMENT_PRIMITIVES.items():
            self.assertIn(Path(importer), visited,
                          f"{importer} is exempted but the composition never reaches it")
            for primitive in primitives:
                self.assertTrue(
                    any(name == primitive or name.startswith(primitive + ".")
                        for name in edges.get(importer, ())),
                    f"{importer} no longer imports {primitive}; drop the exemption")

        # Non-vacuity: the closure must actually contain the composition it is
        # supposed to be constraining, or an empty graph would pass.
        for required in (
                Path("oran/rapp/r1_client.py"),
                Path("oran/rapp/policy_translator.py"),
                Path("oran/rapp/coordinator_adapter.py"),
                Path("oran/integration/objectives.py"),
                Path("oran/integration/lower_release.py"),
                Path("coordinator/intent_coordinator.py"),
                Path("coordinator/fsm.py"),
                Path("coordinator/history.py"),
                Path("decision/llm_backend.py")):
            self.assertIn(required, visited,
                          f"the final composition does not reach {required}")

    def test_the_superseded_custom_http_northbound_is_absent_entirely(self):
        """The custom northbound portal is migration-only and must not exist here.

        The Lower release states it plainly: the custom direct rApp-to-xApp HTTP
        portal is not A1-P and must not be used by the Upper integration.  An
        import gate alone would not catch a copy of it living in this tree, so
        the name is checked against every Python module in the repository.
        """
        offenders = []
        for path in Path(".").rglob("*.py"):
            parts = set(path.parts)
            if "__pycache__" in parts or ".git" in parts:
                continue
            if NORTHBOUND in path.read_text(encoding="utf-8", errors="ignore"):
                offenders.append(str(path))
        self.assertEqual(sorted(offenders), [],
                         f"{NORTHBOUND} is referenced by Upper Python code: "
                         + ", ".join(sorted(offenders)))

    def test_oran_profile_import_graph_has_no_legacy_boundary(self):
        pending = [
            Path("oran/rapp/headless.py"),
            Path("oran/rapp/gui_entry.py"),
            Path("gui/operator/app.py"),
            Path("oran/profiles/local_mock.py"),
            Path("gui/dashboard.py"),
            Path("experiments/runner.py"),
            Path("coordinator/offered_load.py"),
        ]
        visited = set()
        violations = []
        while pending:
            path = pending.pop()
            if path in visited or not path.is_file():
                continue
            visited.add(path)
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            package = list(path.with_suffix("").parts[:-1])
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level:
                        prefix = package[:len(package) - node.level + 1]
                        module = ".".join(prefix + ([node.module] if node.module else []))
                    else:
                        module = node.module or ""
                    names = [module]
                for name in names:
                    if any(name == banned or name.startswith(banned + ".")
                           for banned in self.BANNED_MODULES):
                        violations.append(f"{path}:{node.lineno}:{name}")
                    candidate = Path(*name.split(".")).with_suffix(".py")
                    package_init = Path(*name.split(".")) / "__init__.py"
                    if candidate.is_file():
                        pending.append(candidate)
                    elif package_init.is_file():
                        pending.append(package_init)
        self.assertEqual(violations, [], "legacy imports reachable: " +
                         ", ".join(violations))
        # A vacuous graph that skips the coordinator is not a boundary proof.
        # The O-RAN entry must transitively reach the real FSM/model/history
        # engine while the banned hardware transports remain unreachable.
        for required in (
                Path("coordinator/intent_coordinator.py"),
                Path("coordinator/fsm.py"),
                Path("coordinator/history.py"),
                Path("decision/llm_backend.py")):
            self.assertIn(required, visited,
                          f"real coordinator dependency not reached: {required}")
        self.assertNotIn(Path("gui/legacy_tools.py"), visited)
        self.assertNotIn(Path("experiments/emulation.py"), visited)
        operator_app = Path("gui/operator/app.py")
        if operator_app.is_file():
            self.assertIn(operator_app, visited,
                          "operator console entry was not included in boundary proof")
        adapter = Path("oran/rapp/coordinator_adapter.py").read_text(
            encoding="utf-8")
        self.assertIn("collector=collector", adapter)
        self.assertIn("executor=executor", adapter)
        self.assertIn("llm_manager=llm_manager", adapter)


if __name__ == "__main__":
    unittest.main()
