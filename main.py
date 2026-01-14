import json
import os
import tkinter as tk
from tkinter import ttk
from collections import deque
from dataclasses import dataclass
from tkinter import messagebox
from typing import Deque, Dict, Literal, Optional, Tuple

import cv2
import mss
import numpy as np
import pytesseract
from PIL import Image, ImageTk

Color = Literal["RED", "GREEN", "YELLOW"]

TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
MONITOR_INDEX = 1
OCR_HISTORY = 5
DEBUG_OCR = False  # [PATCH2]

pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


@dataclass(frozen=True)
class Gauge:
    G: int
    D: int
    H: int


def is_fixed(v: int) -> bool:
    # 값이 0 또는 1000인지 확인
    return v == 0 or v == 1000


def is_stable(gauge: Gauge, tol: int = 20) -> bool:
    # 안정 상태 여부 확인 (500 ± tol)
    lower = 500 - tol
    upper = 500 + tol
    return (
        lower <= gauge.G <= upper
        and lower <= gauge.D <= upper
        and lower <= gauge.H <= upper
    )


def clamp(v: int) -> int:
    # 값을 0~1000 범위로 제한
    return max(0, min(1000, v))


def transfer(src: int, dst: int, x: int) -> Tuple[int, int]:
    # 게이지 이동 규칙 적용
    if is_fixed(src) or is_fixed(dst):
        return src, dst
    delta = min(x, src, 1000 - dst)
    return clamp(src - delta), clamp(dst + delta)


def apply_laser(s: Gauge, color: Color, x: int) -> Gauge:
    # 실 색상에 따라 게이지 이동 적용
    G, D, H = s.G, s.D, s.H
    if color == "RED":
        H, G = transfer(H, G, x)
    elif color == "GREEN":
        G, D = transfer(G, D, x)
    else:
        D, H = transfer(D, H, x)
    return Gauge(G, D, H)


def margin(v: int) -> int:
    # 경계까지의 최소 거리 계산
    return min(v, 1000 - v)


def score(s: Gauge):
    # 추천 평가 점수 계산
    min_margin = min(margin(s.G), margin(s.D), margin(s.H))
    boundary = int(is_fixed(s.G)) + int(is_fixed(s.D)) + int(is_fixed(s.H))
    dist = (s.G - 500) ** 2 + (s.D - 500) ** 2 + (s.H - 500) ** 2
    return (min_margin, -boundary, -dist)


def score_to_target(s: Gauge, target: Gauge):
    # 목표 게이지에 가까울수록 높은 점수
    min_margin = min(margin(s.G), margin(s.D), margin(s.H))
    boundary = int(is_fixed(s.G)) + int(is_fixed(s.D)) + int(is_fixed(s.H))
    dist = (s.G - target.G) ** 2 + (s.D - target.D) ** 2 + (s.H - target.H) ** 2
    return (min_margin, -boundary, -dist)


def choose_best_laser(
    state: Gauge,
    x: int,
    depth: int = 2,
    allowed_colors: Optional[Tuple[Color, ...]] = None,
    target: Optional[Gauge] = None,
) -> Color:
    # lookahead 기반 최적 실 색상 선택
    colors = allowed_colors or ("RED", "GREEN", "YELLOW")
    best: Color = colors[0]
    best_key = None
    score_func = score if target is None else lambda s: score_to_target(s, target)
    for c1 in colors:
        s1 = apply_laser(state, c1, x)
        if depth > 1:
            key = max(score_func(apply_laser(s1, c2, x)) for c2 in colors)
        else:
            key = score_func(s1)
        if best_key is None or key > best_key:
            best_key = key
            best = c1
    return best


X_TABLE: Dict[str, int] = {
    "1-HONDON": 50,
    "1-DOOL": 50,
    "1-GUNGGI": 50,
    "2": 60,
    "3": 70,
}
PHASE_ALLOWED_COLORS: Dict[str, Tuple[Color, ...]] = {
    # 페이즈별 등장 가능한 실 색 목록
    "1-HONDON": ("RED", "GREEN"),
    "1-DOOL": ("RED", "YELLOW"),
    "1-GUNGGI": ("GREEN", "YELLOW"),
    "2": ("RED", "GREEN", "YELLOW"),
    "3": ("RED", "GREEN", "YELLOW"),
}
PHASE1_LIST = ("1-HONDON", "1-DOOL", "1-GUNGGI")
PHASE1_LABELS = {
    "1-HONDON": "혼돈",
    "1-DOOL": "도올",
    "1-GUNGGI": "궁기",
}
PREP_TARGETS2: Dict[Tuple[str, str], Gauge] = {
    ("1-HONDON", "1-DOOL"): Gauge(G=900, D=100, H=500),
    ("1-DOOL", "1-GUNGGI"): Gauge(G=100, D=500, H=900),
}

ROI_PATH = "roi_config.json"


def default_roi_data() -> Dict[str, object]:
    # 기본 ROI 데이터 구성
    return {
        "gauge_roi": None,
        "digit_rois": {"G": None, "D": None, "H": None},
        "overlay_pos": {"x": 200, "y": 200},
    }


def load_roi_data() -> Dict[str, object]:
    # ROI 설정 파일 로드
    if not os.path.exists(ROI_PATH):
        return default_roi_data()
    with open(ROI_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def save_roi_data(data: Dict[str, object]) -> None:
    # ROI 설정 파일 저장
    with open(ROI_PATH, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


class RoiSelector(tk.Toplevel):
    def __init__(
        self,
        master: tk.Tk,
        image: Image.Image,
        on_complete,
        title: str,
    ):
        # ROI 선택용 스크린샷 창 구성
        super().__init__(master)
        self.title(title)
        self.attributes("-topmost", True)
        self.configure(bg="black")
        self.on_complete = on_complete
        self._start = None
        self._rect = None

        self.bind("<Escape>", self._cancel)

        self.canvas = tk.Canvas(self, cursor="cross", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self._photo = ImageTk.PhotoImage(image)
        self.canvas.create_image(0, 0, image=self._photo, anchor=tk.NW)
        self.canvas.config(width=image.width, height=image.height)
        self.geometry(f"{image.width}x{image.height}+0+0")

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)

    def _on_press(self, event):
        # 드래그 시작 좌표 기록
        self._start = (event.x, event.y)
        if self._rect:
            self.canvas.delete(self._rect)
        self._rect = self.canvas.create_rectangle(
            event.x,
            event.y,
            event.x,
            event.y,
            outline="red",
            width=2,
        )

    def _on_drag(self, event):
        # 드래그 중 사각형 갱신
        if not self._start or not self._rect:
            return
        self.canvas.coords(self._rect, self._start[0], self._start[1], event.x, event.y)

    def _on_release(self, event):
        # 드래그 종료 시 ROI 확정
        if not self._start:
            return
        x1, y1 = self._start
        x2, y2 = event.x, event.y
        left, right = sorted([x1, x2])
        top, bottom = sorted([y1, y2])
        self.on_complete((left, top, right, bottom))
        self.destroy()

    def _cancel(self, _event):
        # ESC 취소 처리
        self.on_complete(None)
        self.destroy()


class Overlay(tk.Toplevel):
    def __init__(self, master: tk.Tk, pos: Dict[str, int]):
        # 오버레이 UI 구성
        super().__init__(master)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.7)
        self.overrideredirect(True)
        self.configure(bg="black")
        self.geometry(f"+{pos['x']}+{pos['y']}")

        self.label = tk.Label(
            self,
            text="Phase: -\nG: -  D: -  H: -\nLaser: -",
            fg="white",
            bg="black",
            font=("Arial", 12, "bold"),
            justify=tk.LEFT,
        )
        self.label.pack(padx=10, pady=6)

        self._drag_start = None
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_drag)

    def _on_press(self, event):
        # 오버레이 드래그 시작
        self._drag_start = (event.x, event.y)

    def _on_drag(self, event):
        # 오버레이 위치 이동
        if not self._drag_start:
            return
        x = self.winfo_x() + (event.x - self._drag_start[0])
        y = self.winfo_y() + (event.y - self._drag_start[1])
        self.geometry(f"+{x}+{y}")

    def update_text(self, phase: str, gauge: Optional[Gauge], laser: str) -> None:
        # 오버레이 표시 텍스트 갱신
        if gauge:
            text = f"Phase: {phase}\nG: {gauge.G}  D: {gauge.D}  H: {gauge.H}\nLaser: {laser}"
        else:
            text = f"Phase: {phase}\nG: -  D: -  H: -\nLaser: {laser}"
        self.label.config(text=text)

    def get_position(self) -> Dict[str, int]:
        # 현재 오버레이 위치 반환
        return {"x": self.winfo_x(), "y": self.winfo_y()}


class App(tk.Tk):
    def __init__(self):
        # 메인 애플리케이션 초기화
        # [PATCH] OCR/레이저 안정화를 위한 마지막 정상값 저장
        super().__init__()
        self.title("Kaling Gauge Helper")  # [PATCH2]
        self.geometry("520x430")
        self.resizable(False, False)

        self.roi_data = load_roi_data()
        self.running = False
        self._loop_after = None
        self._history: Dict[str, Deque[int]] = {
            "G": deque(maxlen=OCR_HISTORY),
            "D": deque(maxlen=OCR_HISTORY),
            "H": deque(maxlen=OCR_HISTORY),
        }

        self.phase_var = tk.StringVar(value="1-HONDON")
        self.gauge_label = tk.StringVar(value="G: -  D: -  H: -")
        self.laser_label = tk.StringVar(value="추천 실: -")
        self.mode_label = tk.StringVar(value="모드: STABLE  |  현재: 1-HONDON")
        self.phase1_status_var = tk.StringVar(
            value="완료: - / 남은: 혼돈, 도올, 궁기 / 다음: -"
        )
        self.stable_var = tk.BooleanVar(value=True)
        self._phase1_done: set[str] = set()
        self._phase1_current: Optional[str] = self.phase_var.get()
        self._phase1_next_choice: Optional[str] = None
        self._phase_guard = False
        self._last_good_gauge: Optional[Gauge] = None
        self._last_good_laser: Optional[str] = None
        self._pending_gauge: Optional[Gauge] = None  # [PATCH2]

        self.phase_var.trace_add("write", self._on_phase_change)
        self._build_ui()
        self._update_phase1_status()
        self.overlay = Overlay(self, self.roi_data.get("overlay_pos", {"x": 200, "y": 200}))
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        # 메인 UI 구성
        phase_frame = tk.LabelFrame(self, text="페이즈/방")
        phase_frame.pack(fill=tk.X, padx=12, pady=(10, 6))

        phases = [
            ("1-혼돈", "1-HONDON"),
            ("1-도올", "1-DOOL"),
            ("1-궁기", "1-GUNGGI"),
            ("2", "2"),
            ("3", "3"),
        ]
        for idx, (text, value) in enumerate(phases):
            rb = tk.Radiobutton(
                phase_frame,
                text=text,
                value=value,
                variable=self.phase_var,
                indicatoron=0,
                width=10,
                padx=6,
                pady=4,
            )
            row = 0 if idx < 3 else 1
            col = idx if idx < 3 else idx - 3
            rb.grid(row=row, column=col, padx=6, pady=4, sticky="ew")
        for col in range(3):
            phase_frame.grid_columnconfigure(col, weight=1)

        stable_frame = tk.Frame(self)
        stable_frame.pack(fill=tk.X, padx=12, pady=(0, 8))

        tk.Checkbutton(
            stable_frame,
            text="Stable 모드 (500±tol이면 HOLD)",
            variable=self.stable_var,
        ).pack(side=tk.LEFT, padx=4)

        setting_frame = tk.LabelFrame(self, text="설정")
        setting_frame.pack(fill=tk.X, padx=12, pady=6)
        action_frame = tk.LabelFrame(self, text="실행")
        action_frame.pack(fill=tk.X, padx=12, pady=6)

        tk.Button(setting_frame, text="게이지 ROI 지정(전체)", command=self.set_gauge_roi).pack(
            fill=tk.X, padx=8, pady=4
        )
        tk.Button(
            setting_frame, text="숫자 ROI 지정(G→D→H)", command=self.set_digit_rois
        ).pack(fill=tk.X, padx=8, pady=4)
        tk.Button(action_frame, text="Start", command=self.start).pack(
            fill=tk.X, padx=8, pady=4
        )
        tk.Button(action_frame, text="Stop", command=self.stop).pack(
            fill=tk.X, padx=8, pady=4
        )
        tk.Button(
            action_frame,
            text="페이즈 리셋(1페 초기화)",
            command=self.reset_phase_progress,
        ).pack(fill=tk.X, padx=8, pady=4)
        tk.Button(
            action_frame,
            text="현재 페이즈 완료(1페 확정)",
            command=self.complete_current_phase1,
        ).pack(fill=tk.X, padx=8, pady=4)

        next_frame = tk.Frame(action_frame)
        next_frame.pack(fill=tk.X, padx=8, pady=(2, 6))
        tk.Label(next_frame, text="다음 페이즈:").pack(side=tk.LEFT)
        self.phase1_next_var = tk.StringVar(value="-")
        self.phase1_next_combo = ttk.Combobox(
            next_frame,
            textvariable=self.phase1_next_var,
            values=["-"],
            state="readonly",
            width=12,
        )
        self.phase1_next_combo.pack(side=tk.LEFT, padx=6)
        self.phase1_next_combo.bind("<<ComboboxSelected>>", self._on_next_selected)
        self.phase1_move_button = tk.Button(
            next_frame, text="선택한 페이즈로 이동", command=self.move_to_next_phase1
        )
        self.phase1_move_button.pack(side=tk.LEFT, padx=6)
        self.phase1_move_button.config(state=tk.DISABLED)

        status_frame = tk.LabelFrame(self, text="상태")
        status_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        tk.Label(status_frame, textvariable=self.gauge_label, font=("Arial", 12)).pack(
            anchor=tk.W, padx=10, pady=8
        )
        self.laser_value_label = tk.Label(
            status_frame, textvariable=self.laser_label, font=("Arial", 13, "bold")
        )
        self.laser_value_label.pack(anchor=tk.W, padx=10, pady=6)
        tk.Label(status_frame, textvariable=self.mode_label, font=("Arial", 10)).pack(
            anchor=tk.W, padx=10, pady=(2, 8)
        )
        tk.Label(status_frame, textvariable=self.phase1_status_var, font=("Arial", 10)).pack(
            anchor=tk.W, padx=10, pady=(0, 8)
        )

    def set_gauge_roi(self):
        # 게이지 전체 ROI 지정
        screenshot = self._capture_screen()
        RoiSelector(self, screenshot, self._on_gauge_selected, "게이지 ROI 선택")

    def set_digit_rois(self):
        # 숫자 ROI 지정 (게이지 영역 기준)
        if not self.roi_data.get("gauge_roi"):
            messagebox.showwarning("경고", "먼저 게이지 ROI를 지정하세요.")
            return
        screenshot = self._capture_screen()
        gx1, gy1, gx2, gy2 = self.roi_data["gauge_roi"]
        screenshot = screenshot.crop((gx1, gy1, gx2, gy2))
        self._digit_order = ["G", "D", "H"]
        self._digit_index = 0
        self._digit_screenshot = screenshot
        self._prompt_digit_roi()

    def _prompt_digit_roi(self):
        # 숫자 ROI 순차 지정 진행
        if self._digit_index >= len(self._digit_order):
            save_roi_data(self.roi_data)
            return
        label = self._digit_order[self._digit_index]
        RoiSelector(
            self,
            self._digit_screenshot,
            lambda rect: self._on_digit_selected(rect, label),
            f"숫자 ROI 선택 ({label})",
        )

    def _on_gauge_selected(self, rect):
        # 게이지 ROI 저장
        if rect:
            self.roi_data["gauge_roi"] = rect
            save_roi_data(self.roi_data)

    def _on_digit_selected(self, rect, label):
        # 숫자 ROI 저장
        if rect:
            self.roi_data["digit_rois"][label] = rect
            save_roi_data(self.roi_data)
        self._digit_index += 1
        self._prompt_digit_roi()

    def start(self):
        # OCR 및 추천 루프 시작
        if not self.roi_data.get("gauge_roi"):
            messagebox.showwarning("경고", "게이지 ROI를 먼저 지정하세요.")
            return
        if not all(self.roi_data.get("digit_rois", {}).get(k) for k in ("G", "D", "H")):
            messagebox.showwarning("경고", "숫자 ROI를 먼저 지정하세요.")
            return
        if self.running:
            return
        self._phase1_current = self.phase_var.get()
        self._refresh_phase1_next_options()
        self.running = True
        self._schedule_loop()

    def stop(self):
        # OCR 및 추천 루프 중지
        self.running = False
        if self._loop_after:
            self.after_cancel(self._loop_after)
            self._loop_after = None

    def _schedule_loop(self):
        # 다음 프레임 처리 예약
        # [PATCH] 업데이트 주기를 300ms로 단축
        if not self.running:
            return
        self._loop_after = self.after(300, self._process_frame)

    def _process_frame(self):
        # OCR 수행 및 결과 반영
        # [PATCH] OCR/레이저 안정화 및 HOLD 처리
        # [PATCH2] 급변 필터/연속 확인 및 history 누적 정책 적용
        if not self.running:
            return
        raw_gauge = self._read_gauge()  # [PATCH2]
        phase = self.phase_var.get()  # [PATCH2]
        display_gauge = self._last_good_gauge  # [PATCH2]
        ignored_spike = False  # [PATCH2]
        if raw_gauge:  # [PATCH2]
            if DEBUG_OCR:  # [PATCH2]
                print(  # [PATCH2]
                    f"[OCR] raw: G={raw_gauge.G} D={raw_gauge.D} H={raw_gauge.H}"  # [PATCH2]
                )  # [PATCH2]
            accepted_gauge = None  # [PATCH2]
            if self._last_good_gauge is None:  # [PATCH2]
                accepted_gauge = raw_gauge  # [PATCH2]
                self._pending_gauge = None  # [PATCH2]
            else:  # [PATCH2]
                deltas = (  # [PATCH2]
                    abs(raw_gauge.G - self._last_good_gauge.G),  # [PATCH2]
                    abs(raw_gauge.D - self._last_good_gauge.D),  # [PATCH2]
                    abs(raw_gauge.H - self._last_good_gauge.H),  # [PATCH2]
                )  # [PATCH2]
                if any(delta > 250 for delta in deltas):  # [PATCH2]
                    if self._pending_gauge == raw_gauge:  # [PATCH2]
                        accepted_gauge = raw_gauge  # [PATCH2]
                        self._pending_gauge = None  # [PATCH2]
                    else:  # [PATCH2]
                        self._pending_gauge = raw_gauge  # [PATCH2]
                        ignored_spike = True  # [PATCH2]
                else:  # [PATCH2]
                    accepted_gauge = raw_gauge  # [PATCH2]
                    self._pending_gauge = None  # [PATCH2]
            if accepted_gauge:  # [PATCH2]
                for key in ("G", "D", "H"):  # [PATCH2]
                    self._history[key].append(getattr(accepted_gauge, key))  # [PATCH2]
                median_values = {  # [PATCH2]
                    key: int(np.median(list(self._history[key])))  # [PATCH2]
                    for key in ("G", "D", "H")  # [PATCH2]
                }  # [PATCH2]
                gauge = Gauge(  # [PATCH2]
                    median_values["G"], median_values["D"], median_values["H"]  # [PATCH2]
                )  # [PATCH2]
                self._last_good_gauge = gauge  # [PATCH2]
                display_gauge = gauge  # [PATCH2]
                try:  # [PATCH2]
                    stable_mode = self.stable_var.get()  # [PATCH2]
                    prep_target = None
                    if (
                        not stable_mode
                        and phase in PHASE1_LIST
                        and self._phase1_next_choice
                    ):
                        prep_target = PREP_TARGETS2.get(
                            (phase, self._phase1_next_choice)
                        )
                    if stable_mode and is_stable(gauge):  # [PATCH2]
                        # [PATCH] 안정 상태에서는 추천 계산을 건너뛰고 HOLD 표시
                        self._last_good_laser = "HOLD"  # [PATCH2]
                    else:  # [PATCH2]
                        self._last_good_laser = choose_best_laser(  # [PATCH2]
                            gauge,  # [PATCH2]
                            X_TABLE[phase],  # [PATCH2]
                            allowed_colors=PHASE_ALLOWED_COLORS[phase],  # [PATCH2]
                            target=prep_target,  # [PATCH2]
                        )  # [PATCH2]
                except Exception:  # [PATCH2]
                    # [PATCH] 계산 실패 시 마지막 정상 레이저 유지
                    pass  # [PATCH2]
            if DEBUG_OCR:  # [PATCH2]
                print(  # [PATCH2]
                    f"[OCR] accepted: {display_gauge} ignored_spike={ignored_spike}"  # [PATCH2]
                )  # [PATCH2]
        laser = self._last_good_laser or "-"  # [PATCH2]
        if display_gauge:  # [PATCH2]
            self.gauge_label.set(  # [PATCH2]
                f"G: {display_gauge.G}  D: {display_gauge.D}  H: {display_gauge.H}"  # [PATCH2]
            )  # [PATCH2]
        else:  # [PATCH2]
            self.gauge_label.set("G: -  D: -  H: -")  # [PATCH2]
        laser_text = f"추천 실: {laser}"
        self.laser_label.set(laser_text)  # [PATCH2]
        color_map = {"RED": "#e53935", "GREEN": "#43a047", "YELLOW": "#fbc02d"}
        self.laser_value_label.config(fg=color_map.get(laser, "black"))
        mode_tag = "STABLE" if self.stable_var.get() else "PREP"
        self.mode_label.set(f"모드: {mode_tag}  |  현재: {phase}")
        overlay_phase = f"{phase} [{mode_tag}]"
        if self._phase1_next_choice:
            next_label = PHASE1_LABELS.get(self._phase1_next_choice, self._phase1_next_choice)
            overlay_phase = f"{overlay_phase} → NEXT: {next_label}"
        self.overlay.update_text(overlay_phase, display_gauge, laser)  # [PATCH2]
        self._schedule_loop()

    def _read_gauge(self) -> Optional[Gauge]:
        # OCR로 게이지 숫자 읽기
        # [PATCH2] OCR raw 읽기만 수행 (history는 채택된 gauge에서만 누적)
        gx1, gy1, gx2, gy2 = self.roi_data["gauge_roi"]
        width = gx2 - gx1
        height = gy2 - gy1
        with mss.mss() as sct:
            monitor = {"left": gx1, "top": gy1, "width": width, "height": height}
            frame = np.array(sct.grab(monitor))
        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
        values = {}
        for key in ("G", "D", "H"):  # [PATCH2]
            rect = self.roi_data["digit_rois"].get(key)  # [PATCH2]
            if not rect:
                return None
            x1, y1, x2, y2 = rect
            roi = frame[y1:y2, x1:x2]
            if roi.size == 0:
                return None
            processed = self._preprocess_for_ocr(roi)
            text = pytesseract.image_to_string(
                processed, config="--psm 7 -c tessedit_char_whitelist=0123456789"
            )
            digits = "".join(ch for ch in text if ch.isdigit())
            if not digits:
                return None
            value = int(digits)
            if value < 0 or value > 1000:
                return None
            values[key] = value
        return Gauge(values["G"], values["D"], values["H"])  # [PATCH2]

    @staticmethod
    def _preprocess_for_ocr(img: np.ndarray) -> np.ndarray:
        # OCR 전처리
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)  # [PATCH2]
        scale = 2.5  # [PATCH2]
        gray = cv2.resize(  # [PATCH2]
            gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC  # [PATCH2]
        )  # [PATCH2]
        blur = cv2.GaussianBlur(gray, (3, 3), 0)  # [PATCH2]
        _, thresh = cv2.threshold(  # [PATCH2]
            blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU  # [PATCH2]
        )  # [PATCH2]
        white_ratio = np.mean(thresh == 255)  # [PATCH2]
        if white_ratio > 0.7:  # [PATCH2]
            thresh = 255 - thresh  # [PATCH2]
        kernel = np.ones((2, 2), np.uint8)  # [PATCH2]
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=1)  # [PATCH2]
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel, iterations=1)  # [PATCH2]
        return thresh  # [PATCH2]

    @staticmethod
    def _capture_screen() -> Image.Image:
        # 화면 캡처
        with mss.mss() as sct:
            monitor_index = MONITOR_INDEX
            if monitor_index >= len(sct.monitors):
                monitor_index = 1 if len(sct.monitors) > 1 else 0
            monitor = sct.monitors[monitor_index]
            frame = np.array(sct.grab(monitor))
        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB)
        return Image.fromarray(frame)

    def _on_phase_change(self, *_args):
        if self._phase_guard:
            return
        phase = self.phase_var.get()
        if phase in self._phase1_done:
            messagebox.showwarning("경고", "이미 완료된 흉수는 다시 선택할 수 없습니다.")
            remaining = [p for p in PHASE1_LIST if p not in self._phase1_done]
            fallback = remaining[0] if remaining else self._phase1_current
            if fallback and fallback != phase:
                self._phase_guard = True
                self.phase_var.set(fallback)
                self._phase_guard = False
                phase = fallback
        self._phase1_current = phase
        self._update_phase1_status()

    def reset_phase_progress(self):
        self._phase1_done = set()
        self._phase1_next_choice = None
        self._phase1_current = self.phase_var.get()
        self._refresh_phase1_next_options()
        for key in ("G", "D", "H"):
            self._history[key].clear()
        self._pending_gauge = None
        self._update_phase1_status()
        messagebox.showinfo("알림", "1페 진행이 초기화되었습니다.")

    def complete_current_phase1(self):
        phase = self.phase_var.get()
        if phase not in PHASE1_LIST:
            messagebox.showwarning("경고", "1페 흉수에서만 사용할 수 있습니다.")
            return
        if phase in self._phase1_done:
            messagebox.showinfo("알림", "이미 완료 처리된 흉수입니다.")
            return
        self._phase1_done.add(phase)
        self._refresh_phase1_next_options()
        self._update_phase1_status()

    def move_to_next_phase1(self):
        chosen_phase = self.phase1_next_var.get()
        if chosen_phase in ("", "-"):
            return
        self.phase_var.set(chosen_phase)
        self._update_phase1_status()

    def _on_next_selected(self, _event=None):
        chosen_phase = self.phase1_next_var.get()
        if chosen_phase in ("", "-"):
            return
        self._phase1_next_choice = chosen_phase
        self._update_phase1_status()

    def _refresh_phase1_next_options(self):
        remaining = [p for p in PHASE1_LIST if p not in self._phase1_done]
        if not remaining:
            self.phase1_next_combo.config(values=["-"])
            self.phase1_next_var.set("-")
            self._phase1_next_choice = None
            self.phase1_move_button.config(state=tk.DISABLED)
            messagebox.showinfo("알림", "1페 흉수 3개 완료. 2페로 진행하세요.")
            return
        self.phase1_next_combo.config(values=remaining)
        if len(remaining) == 1:
            self.phase1_next_var.set(remaining[0])
            self._phase1_next_choice = remaining[0]
        else:
            if self.phase1_next_var.get() not in remaining:
                self.phase1_next_var.set(remaining[0])
            self._phase1_next_choice = self.phase1_next_var.get()
        self.phase1_move_button.config(state=tk.NORMAL)

    def _update_phase1_status(self):
        if self._phase1_next_choice in self._phase1_done:
            self._phase1_next_choice = None
        done_list = [PHASE1_LABELS[p] for p in PHASE1_LIST if p in self._phase1_done]
        remaining_list = [PHASE1_LABELS[p] for p in PHASE1_LIST if p not in self._phase1_done]
        done_text = ", ".join(done_list) if done_list else "-"
        remaining_text = ", ".join(remaining_list) if remaining_list else "-"
        next_text = PHASE1_LABELS.get(self._phase1_next_choice, "-")
        self.phase1_status_var.set(
            f"완료: {done_text} / 남은: {remaining_text} / 다음: {next_text}"
        )

    def _on_close(self):
        # 종료 시 상태 저장 및 정리
        self.stop()
        self.roi_data["overlay_pos"] = self.overlay.get_position()
        save_roi_data(self.roi_data)
        self.overlay.destroy()
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
