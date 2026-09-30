"""Display-language toggle: English (authored) or Korean, one corner control.

Scope is deliberately narrow.  The console is *authored* in English; this
module never becomes a second source of truth for what a control means.  It
holds one catalog of exact display strings and, while Korean is selected,
walks the widget tree replacing any text it recognises - labels, buttons,
notebook tabs, treeview headings.  Everything else is left alone:

* **Data is not display.**  Exports, session records, evidence, run
  directories, and anything a test or the lower researcher reads stay in
  their canonical form; only widget text on screen changes.
* **Semantic tokens stay canonical.**  Mode names (DISCONNECTED / LIVE /
  REPLAY), Eq.12 outcomes, S0-S6, check ids and other contract vocabulary
  are not translated - an operator reporting a problem must be able to
  quote the exact token the contract uses.
* **Unknown text stays as it is.**  A string not in the catalog is shown
  untouched, never guessed at.

Switching back to English restores every widget from a per-widget record of
what it said before translation, so the round trip is exact.  Dynamic labels
that the repaint loop rewrites in English while Korean is selected are simply
re-translated on the next tick.
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

from . import tokens

ENGLISH = "en"
KOREAN = "ko"

#: Exact display string -> Korean.  Keys must match widget text byte-for-byte;
#: composites assembled at runtime (e.g. "MODE: LIVE", "disposition IDLE") are
#: deliberately absent - their tokens are contract vocabulary.
CATALOG: Dict[str, str] = {
    # Workspace tabs
    "Live Operations": "라이브 운영",
    "Intent & Decision": "인텐트 · 판정",
    "Analysis & Results": "분석 · 결과",
    "Demo View": "데모 뷰",
    "Settings & Integration": "설정 · 통합",
    "Contract Studio": "계약 스튜디오",
    "Trial & Safety": "시행 · 안전",
    "Evidence Ledger": "증거 원장",
    "Batch Experiments": "배치 실험",
    "Objective Registry": "목표 레지스트리",
    # Header field labels
    "Run": "런",
    "Mode": "모드",
    "Profile": "프로파일",
    "Elapsed": "경과",
    "Health": "시스템 상태",
    "Recording": "기록",
    "Latest alert": "최근 알림",
    # Footer
    "Intent": "인텐트",
    "Run Preflight": "프리플라이트 실행",
    "Start Experiment": "실험 시작",
    "Submit Intent": "인텐트 제출",
    "Stop and Finalize": "중지 및 마무리",
    "Abort": "중단",
    # Live Operations - workflow row
    "Experiment workflow": "실험 워크플로",
    "New profile": "새 프로파일",
    "Load": "불러오기",
    "Save": "저장",
    "Preflight": "프리플라이트",
    "Stop and finalize": "중지 및 마무리",
    # Live Operations - session source row
    "Session source": "세션 소스",
    "Bind Live deployment": "라이브 배치 바인딩",
    "Select Live": "라이브 선택",
    "Load Replay capture/run": "리플레이 캡처/런 불러오기",
    "Select Replay": "리플레이 선택",
    "Disconnect": "연결 해제",
    "Replay source": "리플레이 소스",
    ("Bind names the deployment this profile would address and contacts "
     "nothing; only Preflight opens a connection. Start Experiment verifies "
     "readiness and opens a run directory. It does not start a Core, gNB, "
     "UE or USRP."):
        ("바인딩은 이 프로파일이 가리키는 배치의 이름만 지정하며 아무것도 "
         "접속하지 않습니다. 접속은 프리플라이트에서만 열립니다. 실험 시작은 "
         "준비 상태를 검증하고 런 디렉터리를 열 뿐, Core·gNB·UE·USRP를 "
         "기동하지 않습니다."),
    # Live Operations - panes
    "O-RAN topology and status": "O-RAN 토폴로지·상태",
    "Readiness ladder": "준비 단계",
    "Event timeline": "이벤트 타임라인",
    # Topology elements
    "Operator Console": "오퍼레이터 콘솔",
    "rApp / Agentic Intent Coordinator": "rApp / 에이전틱 인텐트 코디네이터",
    "Non-RT RIC Framework": "Non-RT RIC 프레임워크",
    "R1 interface": "R1 인터페이스",
    "A1 policy boundary": "A1 정책 경계",
    "xApp": "xApp 구성요소",
    "O1 Provider": "O1 프로바이더",
    "DME / assurance path": "DME / 보증 경로",
    "5G Core": "5G 코어",
    "USRP / RF front end": "USRP / RF 프런트엔드",
    # Table headings (topology / readiness / preflight / timeline)
    "Element": "요소",
    "Status": "상태",
    "Status source": "상태 출처",
    "Version / release / profile": "버전/릴리스/프로파일",
    "Detail": "상세",
    "Boundary": "경계",
    "Readiness": "준비 상태",
    "Availability": "가용성",
    "Reason": "사유",
    "Source": "출처",
    "Observed": "관측 시점",
    "Check": "점검 항목",
    "Result": "결과",
    "Severity": "심각도",
    "Time": "시각",
    "Event": "이벤트",
    "Kind": "종류",
    "Correlation": "상관 ID",
    "Origin": "발신원",
    "Lane": "레인",
    "Min severity": "최소 심각도",
    "Search": "검색",
    # Intent form
    "Operator intent": "오퍼레이터 인텐트",
    "Operator Intent": "오퍼레이터 인텐트",
    "Target": "대상",
    "Scope": "범위",
    "Priority": "우선순위",
    "Validity": "유효기간",
    "Goals": "목표",
    "Constraints": "제약",
    "Preview normalized intent": "정규화 인텐트 미리보기",
    "Submit intent": "인텐트 제출",
    "Normalized intent · coordinator validator":
        "정규화 인텐트 · 코디네이터 검증기",
    # Intent lifecycle table
    "Intent lifecycle · separate authority axes":
        "인텐트 수명주기 · 권한 축 분리",
    "Lifecycle": "수명주기",
    "Intent ID": "인텐트 ID",
    "Rev": "리비전",
    "Target / scope": "대상/범위",
    "Terminal outcome": "종료 결과",
    "A1 policy": "A1 정책",
    "Evidence": "증적",
    "Policy ID": "정책 ID",
    "Evidence freshness": "증적 신선도",
    "Last update": "최근 갱신",
    "Withdraw selected…": "선택 철회…",
    # Decision panel
    "LLM backend / model": "LLM 백엔드 / 모델",
    "Refresh": "새로고침",
    "Select for next episode": "다음 에피소드에 적용",
    "Calibration · threshold · budgets": "캘리브레이션 · 임계값 · 예산",
    "S0–S6 decision path": "S0–S6 판정 경로",
    "Three-stage LLM pipeline": "3단계 LLM 파이프라인",
    "Structured judgement": "구조화 판정",
    "Interpreted request": "해석된 요청",
    "Conflict": "충돌",
    "Feasible": "실행 가능",
    "Raw confidence": "원시 신뢰도",
    "Calibrated probability": "보정 확률",
    "Threshold applied to": "임계값 적용 대상",
    "Routed to": "라우팅 대상",
    "Agreement": "합의",
    "Success": "성공",
    "Eq.12 state": "Eq.12 상태",
    "Terminal reason": "종료 사유",
    "Candidates, negotiation and final choice": "후보·협상·최종 선택",
    "Model-authored summary": "모델 작성 요약",
    # Correlation trace panel
    "Correlation trace": "상관관계 추적",
    "Correlation id": "상관 ID",
    "1. Intent + revision": "1. 인텐트 + 리비전",
    "2. Three-stage judgement + verdict": "2. 3단계 판단 + 최종 판정",
    "3. rApp/R1 policy identity": "3. rApp/R1 정책 식별자",
    "4. A1-P lifecycle/status": "4. A1-P 생명주기/상태",
    "5. UE + target cell": "5. UE + 대상 셀",
    "6. E2 control attempt + write count": "6. E2 제어 시도 + 쓰기 횟수",
    "7. KPM readback + O1 assurance": "7. KPM 리드백 + O1 어슈어런스",
    "8. FSM terminal state + commit/rollback": "8. FSM 종료 상태 + 커밋/롤백",
    "? Unknown": "? 알 수 없음",
    "? Unknown · no observation": "? 알 수 없음 · 관측 없음",
    "? — · Unknown": "? — · 알 수 없음",
    # Analysis
    "Open stored run (view only)": "저장된 런 열기 (보기 전용)",
    "Export data and metadata": "데이터·메타데이터 내보내기",
    "Run directory": "런 디렉터리",
    ("Opening a run here only charts it; it does not become the session's "
     "source. Use Load Replay capture/run in Live Operations for that."):
        ("여기서 런을 여는 것은 차트 표시일 뿐 세션 소스가 되지 않습니다. "
         "세션 소스로 쓰려면 라이브 운영의 \"리플레이 캡처/런 불러오기\"를 "
         "사용하십시오."),
    "Before / After Outcome": "전/후 결과",
    "Metric": "메트릭",
    "Unit": "단위",
    "Quality": "품질",
    "Interval": "구간",
    # Demo view
    "Headline KPI": "핵심 KPI",
    "System Health": "시스템 상태",
    "Intent → policy / xApp": "인텐트 → 정책 / xApp",
    "S0–S6 / Decision": "S0–S6 / 판정",
    "Radio state": "라디오 상태",
    "No readiness evidence": "준비 증적 없음",
    "No submitted intent": "제출된 인텐트 없음",
    "intent -> policy -> evidence complete": "인텐트 → 정책 → 증적 완료",
    # Settings
    "Display and recording preferences": "표시·기록 설정",
    "Graph window (s; blank = full run)": "그래프 창(초; 공백 = 전체 런)",
    "Refresh (ms)": "갱신 주기(ms)",
    "Record session artifacts": "세션 아티팩트 기록",
    "Integration": "통합",
    "Read-only provenance": "읽기 전용 유래 정보",
    "Enter a positive graph window and refresh interval":
        "그래프 창과 갱신 주기는 양수로 입력하십시오",
    # Dialogs and one-off labels
    "Confirm": "확인",
    "Cancel": "취소",
    "Change LLM backend / model": "LLM 백엔드/모델 변경",
    "Withdraw intent": "인텐트 철회",
    "Default operator profile": "기본 오퍼레이터 프로파일",
    "a recorded source was attached": "기록된 소스가 부착됨",
    "action applied": "액션 적용됨",
    "readback": "리드백",
    "rolled back": "롤백됨",
    "⊘ Unsupported\nNo metric declared": "⊘ 미지원\n선언된 메트릭 없음",
    "not recording": "기록 안 함",
    "not a live radio": "실제 라디오 아님",
    "no backend selected": "백엔드 미선택",
    "none": "없음",
    # The control's own caption
    "Language": "언어",
}


class LanguageSwitch:
    """The corner control and the walker that applies the catalog.

    ``attach`` adds a small Language selector to the right edge of the header
    (the header grid gains one non-uniform column, nothing else moves).
    ``refresh`` is registered as a tick hook: free while English is selected,
    and while Korean is selected it re-translates whatever the repaint loop
    rewrote.  Reverting to English restores the recorded original of every
    widget this module ever touched.
    """

    def __init__(self, *, theme: str = tokens.DEFAULT_THEME) -> None:
        self.language = ENGLISH
        self.control = None
        self._root = None
        self._var = None
        self._palette = tokens.theme(theme)
        #: (widget path, slot, detail) -> the English text that was replaced.
        self._originals: Dict[Tuple[str, str, str], str] = {}

    # -- assembly ----------------------------------------------------------- #

    def attach(self, window) -> Optional[object]:
        """Add the selector to ``window`` (a ConsoleWindow).  Idempotent-safe
        for tests that never build a header: without one, the switch still
        works programmatically, there is simply no widget."""
        import tkinter as tk
        from tkinter import ttk

        self._root = window.root
        header = getattr(window, "header", None)
        host = getattr(header, "frame", None)
        if host is None:
            return None
        cell = tk.Frame(host, bg=self._palette["panel_bg"])
        column = host.grid_size()[0]
        cell.grid(row=0, column=column, sticky="ne",
                  padx=tokens.SPACING["tight"], pady=tokens.SPACING["hair"])
        tk.Label(cell, text="Language", anchor="e",
                 bg=self._palette["panel_bg"], fg=self._palette["fg_muted"],
                 font=tokens.font("micro")).pack(fill="x")
        self._var = tk.StringVar(value="English")
        box = ttk.Combobox(cell, state="readonly", width=7,
                           textvariable=self._var,
                           values=("English", "한국어"))
        box.pack(anchor="e")
        box.bind("<<ComboboxSelected>>", self._on_selected)
        self.control = box
        return box

    def _on_selected(self, _event=None) -> None:
        choice = self._var.get() if self._var is not None else "English"
        self.set_language(KOREAN if choice == "한국어" else ENGLISH)

    # -- switching ---------------------------------------------------------- #

    def set_language(self, language: str) -> None:
        if language not in (ENGLISH, KOREAN):
            raise ValueError(f"unknown language: {language!r}")
        self.language = language
        if self._var is not None:
            self._var.set("한국어" if language == KOREAN else "English")
        if self._root is None:
            return
        if language == KOREAN:
            self._walk(self._root)
        else:
            self._restore()

    def refresh(self) -> None:
        """Tick hook.  No-op in English; in Korean, re-translate anything the
        repaint loop rewrote and any widget created since the last tick."""
        if self.language == KOREAN and self._root is not None:
            self._walk(self._root)

    # -- the walker --------------------------------------------------------- #

    _TEXT_CLASSES = frozenset({
        "Label", "Button", "Checkbutton", "Radiobutton", "Menubutton",
        "Labelframe", "Message", "TLabel", "TButton", "TCheckbutton",
        "TRadiobutton", "TMenubutton", "TLabelframe",
    })

    def _walk(self, widget) -> None:
        try:
            cls = widget.winfo_class()
        except Exception:
            return
        if cls in self._TEXT_CLASSES:
            self._swap_text(widget)
        elif cls == "Treeview":
            self._swap_headings(widget)
        elif cls == "TNotebook":
            self._swap_tabs(widget)
        try:
            children = widget.winfo_children()
        except Exception:
            return
        for child in children:
            self._walk(child)

    def _swap_text(self, widget) -> None:
        try:
            current = str(widget.cget("text"))
        except Exception:
            return
        korean = CATALOG.get(current)
        if korean is None:
            return
        self._originals[(str(widget), "text", "")] = current
        try:
            widget.configure(text=korean)
        except Exception:
            pass

    def _swap_headings(self, tree) -> None:
        try:
            columns = ["#0"] + [str(c) for c in (tree.cget("columns") or ())]
        except Exception:
            return
        for column in columns:
            try:
                current = str(tree.heading(column, "text"))
            except Exception:
                continue
            korean = CATALOG.get(current)
            if korean is None:
                continue
            self._originals[(str(tree), "heading", column)] = current
            try:
                tree.heading(column, text=korean)
            except Exception:
                pass

    def _swap_tabs(self, notebook) -> None:
        try:
            tabs = notebook.tabs()
        except Exception:
            return
        for tab in tabs:
            try:
                current = str(notebook.tab(tab, "text"))
            except Exception:
                continue
            korean = CATALOG.get(current)
            if korean is None:
                continue
            self._originals[(str(notebook), "tab", str(tab))] = current
            try:
                notebook.tab(tab, text=korean)
            except Exception:
                pass

    # -- restoring ---------------------------------------------------------- #

    def _restore(self) -> None:
        if self._root is None:
            self._originals.clear()
            return
        for (path, slot, detail), original in self._originals.items():
            try:
                widget = self._root.nametowidget(path)
            except Exception:
                continue
            try:
                if slot == "text":
                    widget.configure(text=original)
                elif slot == "heading":
                    widget.heading(detail, text=original)
                elif slot == "tab":
                    widget.tab(detail, text=original)
            except Exception:
                continue
        self._originals.clear()


__all__ = ["CATALOG", "ENGLISH", "KOREAN", "LanguageSwitch"]
