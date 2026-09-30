"""steer_window_check 의 앵커 검증 — 이것이 깨지면 엉뚱한 창을 자르고도 "0 건" 을 답한다."""
import importlib.util, pathlib, unittest

_spec = importlib.util.spec_from_file_location(
    "swc", pathlib.Path(__file__).with_name("overnight") / "steer_window_check.py")
swc = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(swc)


class TheAnchorRefusesAClockItCannotTrust(unittest.TestCase):
    def test_a_matching_span_verifies(self):
        # 실측: 파일 생성 1789714030, mtime +33395s, gNB 시계도 33395s 흘렀다
        ok, why = swc.anchor(1789714030, 1789714030 + 33395, 4294821.6, 4294821.6 + 33395)
        self.assertTrue(ok, why)
        self.assertIn("33395", why)

    def test_a_nine_hour_disagreement_is_refused(self):
        # 파일명을 UTC 로 읽어 9시간 어긋났던 실제 실패. 조용히 통과하면 안 된다.
        ok, why = swc.anchor(1789714030, 1789714030 + 33395, 4294821.6, 4294821.6 + 33395 - 9 * 3600)
        self.assertFalse(ok)
        self.assertIn("앵커 불일치", why)

    def test_no_creation_time_is_refused_not_guessed(self):
        # stat %W = 0 (생성 시각 없음) 이면 mtime 으로 때우지 말고 거절한다.
        ok, why = swc.anchor(0, 1789714030, 4294821.6, 4294821.6 + 33395)
        self.assertFalse(ok)
        self.assertIn("stat %W", why)


class TheReadbackMustComeFromTheObservationNotTheRequest(unittest.TestCase):
    """요청(configuration)을 되읽기로 쓰면 항상 일치해 실패를 영원히 못 잡는다."""

    def _episode(self, kpis):
        trial = {"trialIndex": 0, "controlId": "C8-steer-ue1",
                 "configuration": {"servingCell@ue1": "12345678"},
                 "kpis": kpis,
                 "kernel": {"detail": "commit: applied", "terminalState": "SETTLED_SUCCESS"}}
        return {"trials": [trial], "hardwareDisconnects": [], "identityRebinds": []}, trial

    def test_an_unmoved_cell_is_reported_as_different(self):
        ep, t = self._episode({"servingCell@ue1": "87654321"})   # 요청 12345678, 실제로 안 옮겨감
        out = _capture(ep, t)
        self.assertIn("요청과 다르다", out)

    def test_a_moved_cell_is_reported_as_the_same(self):
        ep, t = self._episode({"servingCell@ue1": "12345678"})
        out = _capture(ep, t)
        self.assertIn("요청과 같다", out)

    def test_no_observation_is_not_silently_an_agreement(self):
        ep, t = self._episode({})
        out = _capture(ep, t)
        self.assertIn("관측이 없다", out)
        self.assertNotIn("요청과 같다", out)


def _capture(episode, trial):
    import contextlib, io
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        swc.verdict(episode, trial)
    return buf.getvalue()


if __name__ == "__main__":
    unittest.main()


class TheCountSeparatesATriggerFromAnActualMove(unittest.TestCase):
    """철자는 OAI 소스에서 가져왔다. 방아쇠를 이동으로 세면 실패를 성공으로 보고한다."""

    LINES = [
        "4327800.1 [NR_RRC] I HO LOG: Event A2 (Serving becomes worse than threshold)",
        "4327801.2 [NR_RRC] I HO LOG: Event A3 Report for UE 1 - Neighbour Becomes Better than Serving!",
        "4327802.3 [NR_RRC] I HO LOG: Serving Cell RSRP: -95 - Best Neighbor RSRP: -80 ! Trigger N2 HO",
        "4327803.4 [NR_RRC] I HO LOG: Handover Preparation for UE 1 Encoded (120 bytes)",
        "4327804.5 [NR_RRC] I HO LOG: Handover Command for UE 1 Encoded (96 bytes)",
        "4327805.6 [NR_MAC] W Received ACK for RRCReconfiguration, but nothing to apply!",
        "4327806.7 [NR_MAC] I Frame.Slot 512.0",
    ]

    def test_every_stage_is_counted_under_its_own_name(self):
        got = swc.count_kinds(self.LINES)
        self.assertEqual(got.get("EventA2"), 1)
        self.assertEqual(got.get("EventA3(방아쇠)"), 1)
        self.assertEqual(got.get("N2HO방아쇠"), 1)
        self.assertEqual(got.get("핸드오버준비"), 1)
        self.assertEqual(got.get("핸드오버명령"), 1)
        self.assertEqual(got.get("nothing-to-apply"), 1)

    def test_triggers_alone_do_not_count_as_a_move(self):
        triggers = [l for l in self.LINES if "Handover" not in l]
        got = swc.count_kinds(triggers)
        self.assertNotIn(swc.MOVE_KEY, got, "방아쇠만으로 이동을 세면 실패가 성공이 된다")

    def test_a_window_with_only_heartbeats_reports_no_move(self):
        got = swc.count_kinds(["4327806.7 [NR_MAC] I Frame.Slot 512.0"] * 22)
        self.assertEqual(got, {"Frame.Slot": 22})
        self.assertNotIn(swc.MOVE_KEY, got)
