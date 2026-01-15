import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Deque, Dict, Literal, Optional, Tuple

import cv2
import mss
import numpy as np
import pytesseract
from PIL import Image

Color = Literal["RED", "GREEN", "YELLOW"]

TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
MONITOR_INDEX = 1
OCR_HISTORY = 5
DEBUG_OCR = False

pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


@dataclass(frozen=True)
class Gauge:
    G: int
    D: int
    H: int


@dataclass
class OcrDebug:
    processed_images: Dict[str, np.ndarray]
    raw_text: Dict[str, str]


@dataclass
class GaugeResult:
    gauge: Optional[Gauge]
    laser: str
    ignored_spike: bool
    debug: Optional[OcrDebug] = None


def is_fixed(v: int) -> bool:
    return v == 0 or v == 1000


def is_stable(gauge: Gauge, tol: int = 20) -> bool:
    lower = 500 - tol
    upper = 500 + tol
    return (
        lower <= gauge.G <= upper
        and lower <= gauge.D <= upper
        and lower <= gauge.H <= upper
    )


def clamp(v: int) -> int:
    return max(0, min(1000, v))


def transfer(src: int, dst: int, x: int) -> Tuple[int, int]:
    if is_fixed(src) or is_fixed(dst):
        return src, dst
    delta = min(x, src, 1000 - dst)
    return clamp(src - delta), clamp(dst + delta)


def apply_laser(s: Gauge, color: Color, x: int) -> Gauge:
    G, D, H = s.G, s.D, s.H
    if color == "RED":
        H, G = transfer(H, G, x)
    elif color == "GREEN":
        G, D = transfer(G, D, x)
    else:
        D, H = transfer(D, H, x)
    return Gauge(G, D, H)


def margin(v: int) -> int:
    return min(v, 1000 - v)


def score(s: Gauge):
    min_margin = min(margin(s.G), margin(s.D), margin(s.H))
    boundary = int(is_fixed(s.G)) + int(is_fixed(s.D)) + int(is_fixed(s.H))
    dist = (s.G - 500) ** 2 + (s.D - 500) ** 2 + (s.H - 500) ** 2
    return (min_margin, -boundary, -dist)


def score_to_target(s: Gauge, target: Gauge):
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

ROI_PATH = Path("roi_config.json")


def default_roi_data() -> Dict[str, object]:
    return {
        "gauge_roi": None,
        "digit_rois": {"G": None, "D": None, "H": None},
        "overlay_pos": {"x": 200, "y": 200},
        "monitor_offset": {"x": 0, "y": 0},
        "monitor_index": MONITOR_INDEX,
    }


def load_roi_data() -> Dict[str, object]:
    if not ROI_PATH.exists():
        return default_roi_data()
    with ROI_PATH.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    if "monitor_offset" not in data:
        data["monitor_offset"] = {"x": 0, "y": 0}
    if "monitor_index" not in data:
        data["monitor_index"] = MONITOR_INDEX
    if "digit_rois" not in data:
        data["digit_rois"] = {"G": None, "D": None, "H": None}
    if "overlay_pos" not in data:
        data["overlay_pos"] = {"x": 200, "y": 200}

    return data


def save_roi_data(data: Dict[str, object]) -> None:
    with ROI_PATH.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def list_monitors() -> Tuple[int, Dict[int, str]]:
    with mss.mss() as sct:
        total = len(sct.monitors)
        options = {}
        for idx in range(total):
            if idx == 0:
                label = "All Monitors"
            else:
                monitor = sct.monitors[idx]
                label = (
                    f"Monitor {idx} ({monitor['width']}x{monitor['height']})"
                )
            options[idx] = label
    return total, options


class GaugeService:
    def __init__(self, roi_data: Dict[str, object], ocr_history: int = OCR_HISTORY):
        self.roi_data = roi_data
        self.history: Dict[str, Deque[int]] = {
            "G": deque(maxlen=ocr_history),
            "D": deque(maxlen=ocr_history),
            "H": deque(maxlen=ocr_history),
        }
        self.last_good_gauge: Optional[Gauge] = None
        self.last_good_laser: Optional[str] = None
        self.pending_gauge: Optional[Gauge] = None

    def update_roi_data(self, roi_data: Dict[str, object]) -> None:
        self.roi_data = roi_data

    def reset_history(self, ocr_history: int) -> None:
        self.history = {
            "G": deque(maxlen=ocr_history),
            "D": deque(maxlen=ocr_history),
            "H": deque(maxlen=ocr_history),
        }
        self.pending_gauge = None
        self.last_good_gauge = None
        self.last_good_laser = None

    def process_frame(
        self,
        phase: str,
        stable_mode: bool,
        phase1_next_choice: Optional[str],
        debug_ocr: bool = False,
    ) -> GaugeResult:
        raw_gauge, debug_payload = self._read_gauge(debug_ocr)
        display_gauge = self.last_good_gauge
        ignored_spike = False

        if raw_gauge:
            accepted_gauge = None
            if self.last_good_gauge is None:
                accepted_gauge = raw_gauge
                self.pending_gauge = None
            else:
                deltas = (
                    abs(raw_gauge.G - self.last_good_gauge.G),
                    abs(raw_gauge.D - self.last_good_gauge.D),
                    abs(raw_gauge.H - self.last_good_gauge.H),
                )
                if any(delta > 250 for delta in deltas):
                    if self.pending_gauge == raw_gauge:
                        accepted_gauge = raw_gauge
                        self.pending_gauge = None
                    else:
                        self.pending_gauge = raw_gauge
                        ignored_spike = True
                else:
                    accepted_gauge = raw_gauge
                    self.pending_gauge = None

            if accepted_gauge:
                for key in ("G", "D", "H"):
                    self.history[key].append(getattr(accepted_gauge, key))

                median_values = {
                    key: int(np.median(list(self.history[key])))
                    for key in ("G", "D", "H")
                }
                gauge = Gauge(median_values["G"], median_values["D"], median_values["H"])
                self.last_good_gauge = gauge
                display_gauge = gauge

                prep_target = None
                if not stable_mode and phase in PHASE1_LIST and phase1_next_choice:
                    prep_target = PREP_TARGETS2.get((phase, phase1_next_choice))

                if stable_mode and is_stable(gauge):
                    self.last_good_laser = "HOLD"
                else:
                    self.last_good_laser = choose_best_laser(
                        gauge,
                        X_TABLE[phase],
                        allowed_colors=PHASE_ALLOWED_COLORS[phase],
                        target=prep_target,
                    )

        laser = self.last_good_laser or "-"
        return GaugeResult(
            gauge=display_gauge,
            laser=laser,
            ignored_spike=ignored_spike,
            debug=debug_payload,
        )

    def _read_gauge(self, debug_ocr: bool) -> Tuple[Optional[Gauge], Optional[OcrDebug]]:
        gauge_roi = self.roi_data.get("gauge_roi")
        digit_rois = self.roi_data.get("digit_rois", {})
        if not gauge_roi:
            return None, None

        gx1, gy1, gx2, gy2 = gauge_roi
        width = gx2 - gx1
        height = gy2 - gy1

        off = self.roi_data.get("monitor_offset", {"x": 0, "y": 0})
        abs_left = gx1 + int(off.get("x", 0))
        abs_top = gy1 + int(off.get("y", 0))

        with mss.mss() as sct:
            monitor = {"left": abs_left, "top": abs_top, "width": width, "height": height}
            frame = np.array(sct.grab(monitor))

        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

        values: Dict[str, int] = {}
        debug_images: Dict[str, np.ndarray] = {}
        debug_text: Dict[str, str] = {}

        for key in ("G", "D", "H"):
            rect = digit_rois.get(key)
            if not rect:
                return None, None
            x1, y1, x2, y2 = rect
            roi = frame[y1:y2, x1:x2]
            if roi.size == 0:
                return None, None

            processed = self._preprocess_for_ocr(roi)
            text = pytesseract.image_to_string(
                processed, config="--psm 7 -c tessedit_char_whitelist=0123456789"
            )
            digits = "".join(ch for ch in text if ch.isdigit())
            if not digits:
                if debug_ocr:
                    debug_images[key] = processed
                    debug_text[key] = text
                return None, None

            value = int(digits)
            if value < 0 or value > 1000:
                return None, None
            values[key] = value

            if debug_ocr:
                debug_images[key] = processed
                debug_text[key] = text

        debug_payload = None
        if debug_ocr:
            debug_payload = OcrDebug(processed_images=debug_images, raw_text=debug_text)

        return Gauge(values["G"], values["D"], values["H"]), debug_payload

    @staticmethod
    def _preprocess_for_ocr(img: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        scale = 2.5
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        blur = cv2.GaussianBlur(gray, (3, 3), 0)
        _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        white_ratio = np.mean(thresh == 255)
        if white_ratio > 0.7:
            thresh = 255 - thresh
        kernel = np.ones((2, 2), np.uint8)
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=1)
        thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel, iterations=1)
        return thresh

    @staticmethod
    def capture_screen_with_offset(monitor_index: int) -> Tuple[Image.Image, Dict[str, int], int]:
        with mss.mss() as sct:
            if monitor_index >= len(sct.monitors):
                monitor_index = 1 if len(sct.monitors) > 1 else 0
            monitor = sct.monitors[monitor_index]
            frame = np.array(sct.grab(monitor))

        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB)
        img = Image.fromarray(frame)
        offset = {"x": int(monitor["left"]), "y": int(monitor["top"])}
        return img, offset, monitor_index
