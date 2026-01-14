import json
import os
import tkinter as tk
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

pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


@dataclass(frozen=True)
class Gauge:
    G: int
    D: int
    H: int


def is_fixed(v: int) -> bool:
    return v == 0 or v == 1000


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


def choose_best_laser(state: Gauge, x: int, depth: int = 2) -> Color:
    COLORS: Tuple[Color, ...] = ("RED", "GREEN", "YELLOW")
    best: Color = "RED"
    best_key = None
    for c1 in COLORS:
        s1 = apply_laser(state, c1, x)
        if depth > 1:
            key = max(score(apply_laser(s1, c2, x)) for c2 in COLORS)
        else:
            key = score(s1)
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

ROI_PATH = "roi_config.json"


def default_roi_data() -> Dict[str, object]:
    return {
        "gauge_roi": None,
        "digit_rois": {"G": None, "D": None, "H": None},
        "overlay_pos": {"x": 200, "y": 200},
    }


def load_roi_data() -> Dict[str, object]:
    if not os.path.exists(ROI_PATH):
        return default_roi_data()
    with open(ROI_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def save_roi_data(data: Dict[str, object]) -> None:
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
        if not self._start or not self._rect:
            return
        self.canvas.coords(self._rect, self._start[0], self._start[1], event.x, event.y)

    def _on_release(self, event):
        if not self._start:
            return
        x1, y1 = self._start
        x2, y2 = event.x, event.y
        left, right = sorted([x1, x2])
        top, bottom = sorted([y1, y2])
        self.on_complete((left, top, right, bottom))
        self.destroy()

    def _cancel(self, _event):
        self.on_complete(None)
        self.destroy()


class Overlay(tk.Toplevel):
    def __init__(self, master: tk.Tk, pos: Dict[str, int]):
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
        self._drag_start = (event.x, event.y)

    def _on_drag(self, event):
        if not self._drag_start:
            return
        x = self.winfo_x() + (event.x - self._drag_start[0])
        y = self.winfo_y() + (event.y - self._drag_start[1])
        self.geometry(f"+{x}+{y}")

    def update_text(self, phase: str, gauge: Optional[Gauge], laser: str) -> None:
        if gauge:
            text = f"Phase: {phase}\nG: {gauge.G}  D: {gauge.D}  H: {gauge.H}\nLaser: {laser}"
        else:
            text = f"Phase: {phase}\nG: -  D: -  H: -\nLaser: {laser}"
        self.label.config(text=text)

    def get_position(self) -> Dict[str, int]:
        return {"x": self.winfo_x(), "y": self.winfo_y()}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Karing Gauge Helper")
        self.geometry("460x360")
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

        self._build_ui()
        self.overlay = Overlay(self, self.roi_data.get("overlay_pos", {"x": 200, "y": 200}))
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        phase_frame = tk.LabelFrame(self, text="페이즈/방")
        phase_frame.pack(fill=tk.X, padx=12, pady=10)

        phases = [
            ("1-혼돈", "1-HONDON"),
            ("1-도올", "1-DOOL"),
            ("1-궁기", "1-GUNGGI"),
            ("2", "2"),
            ("3", "3"),
        ]
        for text, value in phases:
            rb = tk.Radiobutton(
                phase_frame,
                text=text,
                value=value,
                variable=self.phase_var,
                indicatoron=0,
                width=8,
                padx=8,
                pady=4,
            )
            rb.pack(side=tk.LEFT, padx=4, pady=6)

        button_frame = tk.Frame(self)
        button_frame.pack(fill=tk.X, padx=12, pady=6)

        tk.Button(button_frame, text="게이지 ROI 지정(전체)", command=self.set_gauge_roi).pack(
            fill=tk.X, pady=3
        )
        tk.Button(
            button_frame, text="숫자 ROI 지정(G→D→H)", command=self.set_digit_rois
        ).pack(fill=tk.X, pady=3)
        tk.Button(button_frame, text="Start", command=self.start).pack(fill=tk.X, pady=3)
        tk.Button(button_frame, text="Stop", command=self.stop).pack(fill=tk.X, pady=3)

        status_frame = tk.LabelFrame(self, text="상태")
        status_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=10)

        tk.Label(status_frame, textvariable=self.gauge_label, font=("Arial", 12)).pack(
            anchor=tk.W, padx=10, pady=8
        )
        tk.Label(status_frame, textvariable=self.laser_label, font=("Arial", 12, "bold")).pack(
            anchor=tk.W, padx=10, pady=6
        )

    def set_gauge_roi(self):
        screenshot = self._capture_screen()
        RoiSelector(self, screenshot, self._on_gauge_selected, "게이지 ROI 선택")

    def set_digit_rois(self):
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
        if rect:
            self.roi_data["gauge_roi"] = rect
            save_roi_data(self.roi_data)

    def _on_digit_selected(self, rect, label):
        if rect:
            self.roi_data["digit_rois"][label] = rect
            save_roi_data(self.roi_data)
        self._digit_index += 1
        self._prompt_digit_roi()

    def start(self):
        if not self.roi_data.get("gauge_roi"):
            messagebox.showwarning("경고", "게이지 ROI를 먼저 지정하세요.")
            return
        if not all(self.roi_data.get("digit_rois", {}).get(k) for k in ("G", "D", "H")):
            messagebox.showwarning("경고", "숫자 ROI를 먼저 지정하세요.")
            return
        if self.running:
            return
        self.running = True
        self._schedule_loop()

    def stop(self):
        self.running = False
        if self._loop_after:
            self.after_cancel(self._loop_after)
            self._loop_after = None

    def _schedule_loop(self):
        if not self.running:
            return
        self._loop_after = self.after(400, self._process_frame)

    def _process_frame(self):
        if not self.running:
            return
        gauge = self._read_gauge()
        phase = self.phase_var.get()
        laser = "-"
        if gauge:
            laser = choose_best_laser(gauge, X_TABLE[phase])
            self.gauge_label.set(f"G: {gauge.G}  D: {gauge.D}  H: {gauge.H}")
            self.laser_label.set(f"추천 실: {laser}")
        self.overlay.update_text(phase, gauge, laser)
        self._schedule_loop()

    def _read_gauge(self) -> Optional[Gauge]:
        gx1, gy1, gx2, gy2 = self.roi_data["gauge_roi"]
        width = gx2 - gx1
        height = gy2 - gy1
        with mss.mss() as sct:
            monitor = {"left": gx1, "top": gy1, "width": width, "height": height}
            frame = np.array(sct.grab(monitor))
        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)
        values = {}
        for key, rect in self.roi_data["digit_rois"].items():
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
        for key, value in values.items():
            self._history[key].append(value)
        if not all(self._history[key] for key in ("G", "D", "H")):
            return None
        median_values = {
            key: int(np.median(list(self._history[key]))) for key in ("G", "D", "H")
        }
        return Gauge(median_values["G"], median_values["D"], median_values["H"])

    @staticmethod
    def _preprocess_for_ocr(img: np.ndarray) -> np.ndarray:
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (3, 3), 0)
        _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        thresh = cv2.dilate(thresh, np.ones((2, 2), np.uint8), iterations=1)
        return thresh

    @staticmethod
    def _capture_screen() -> Image.Image:
        with mss.mss() as sct:
            monitor_index = MONITOR_INDEX
            if monitor_index >= len(sct.monitors):
                monitor_index = 1 if len(sct.monitors) > 1 else 0
            monitor = sct.monitors[monitor_index]
            frame = np.array(sct.grab(monitor))
        frame = cv2.cvtColor(frame, cv2.COLOR_BGRA2RGB)
        return Image.fromarray(frame)

    def _on_close(self):
        self.stop()
        self.roi_data["overlay_pos"] = self.overlay.get_position()
        save_roi_data(self.roi_data)
        self.overlay.destroy()
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.mainloop()
