from __future__ import annotations

import tkinter as tk
from tkinter import messagebox
from collections import deque
from typing import Dict, Optional, Tuple

import customtkinter as ctk
from PIL import Image, ImageTk

from core import (
    DEBUG_OCR,
    MONITOR_INDEX,
    OCR_HISTORY,
    PHASE1_LABELS,
    PHASE1_LIST,
    PREP_TARGETS2,
    PHASE_ALLOWED_COLORS,
    X_TABLE,
    Gauge,
    GaugeService,
    GaugeResult,
    choose_best_laser,
    default_roi_data,
    is_stable,
    list_monitors,
    load_roi_data,
    save_roi_data,
)


class RoiSelector(ctk.CTkToplevel):
    def __init__(self, master: ctk.CTk, image: Image.Image, on_complete, title: str):
        super().__init__(master)
        self.title(title)
        self.attributes("-topmost", True)
        self.configure(bg="black")
        self.on_complete = on_complete
        self._start: Optional[Tuple[int, int]] = None
        self._rect = None

        self.bind("<Escape>", self._cancel)

        self.canvas = tk.Canvas(self, cursor="cross", highlightthickness=0, bg="black")
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
            event.x, event.y, event.x, event.y, outline="red", width=2
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


class Overlay(ctk.CTkToplevel):
    def __init__(self, master: ctk.CTk, pos: Dict[str, int]):
        super().__init__(master)
        self.attributes("-topmost", True)
        self.attributes("-alpha", 0.7)
        self.overrideredirect(True)
        self.configure(fg_color="black")
        self.geometry(f"+{pos['x']}+{pos['y']}")

        self.label = ctk.CTkLabel(
            self,
            text="Phase: -\nG: -  D: -  H: -\nLaser: -",
            text_color="white",
            font=ctk.CTkFont(size=12, weight="bold"),
            justify="left",
        )
        self.label.pack(padx=10, pady=6)

        self._drag_start: Optional[Tuple[int, int]] = None
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
        self.label.configure(text=text)

    def get_position(self) -> Dict[str, int]:
        return {"x": self.winfo_x(), "y": self.winfo_y()}


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Kaling Gauge Helper")
        self.geometry("1200x720")
        self.minsize(1100, 640)

        ctk.set_appearance_mode("System")
        ctk.set_default_color_theme("dark-blue")

        self.roi_data = load_roi_data()
        self.service = GaugeService(self.roi_data, OCR_HISTORY)

        self.running = False
        self._loop_after = None
        self._history_log: deque[str] = deque(maxlen=120)

        self.phase_var = ctk.StringVar(value="1-HONDON")
        self.gauge_label = ctk.StringVar(value="G: -  D: -  H: -")
        self.laser_label = ctk.StringVar(value="추천 실: -")
        self.mode_label = ctk.StringVar(value="모드: STABLE  |  현재: 1-HONDON")
        self.phase1_status_var = ctk.StringVar(
            value="완료: - / 남은: 혼돈, 도올, 궁기 / 다음: -"
        )
        self.stable_var = ctk.BooleanVar(value=True)
        self.debug_var = ctk.BooleanVar(value=DEBUG_OCR)
        self.update_interval_var = ctk.IntVar(value=300)
        self.ocr_history_var = ctk.IntVar(value=OCR_HISTORY)
        self.monitor_index_var = ctk.IntVar(value=self.roi_data.get("monitor_index", MONITOR_INDEX))
        self.preview_scale_var = ctk.DoubleVar(value=1.0)
        self.theme_var = ctk.StringVar(value="System")

        self._phase1_done: set[str] = set()
        self._phase1_current: Optional[str] = self.phase_var.get()
        self._phase1_next_choice: Optional[str] = None
        self._phase_guard = False

        self.phase_var.trace_add("write", self._on_phase_change)

        self._build_ui()
        self._update_phase1_status()

        self.overlay = Overlay(self, self.roi_data.get("overlay_pos", {"x": 200, "y": 200}))
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # =========================
    # UI
    # =========================
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.sidebar = ctk.CTkFrame(self, width=200, corner_radius=0)
        self.sidebar.grid(row=0, column=0, sticky="ns")
        self.sidebar.grid_rowconfigure(6, weight=1)

        ctk.CTkLabel(
            self.sidebar,
            text="Kaling Helper",
            font=ctk.CTkFont(size=18, weight="bold"),
        ).grid(row=0, column=0, padx=20, pady=(24, 18), sticky="w")

        self.nav_buttons = {}
        nav_items = [
            ("Dashboard", "dashboard"),
            ("ROI / Capture", "roi"),
            ("Strategy / Simulator", "strategy"),
            ("Debug", "debug"),
        ]
        for idx, (label, key) in enumerate(nav_items, start=1):
            button = ctk.CTkButton(
                self.sidebar,
                text=label,
                anchor="w",
                corner_radius=12,
                command=lambda k=key: self._show_page(k),
            )
            button.grid(row=idx, column=0, padx=18, pady=6, sticky="ew")
            self.nav_buttons[key] = button

        ctk.CTkLabel(self.sidebar, text="테마", font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=7, column=0, padx=18, pady=(8, 4), sticky="w"
        )
        theme_toggle = ctk.CTkSegmentedButton(
            self.sidebar,
            values=["System", "Dark", "Light"],
            variable=self.theme_var,
            command=self._apply_theme,
        )
        theme_toggle.grid(row=8, column=0, padx=18, pady=(0, 18), sticky="ew")

        self.page_container = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.page_container.grid(row=0, column=1, sticky="nsew")
        self.page_container.grid_rowconfigure(0, weight=1)
        self.page_container.grid_columnconfigure(0, weight=1)

        self.pages = {
            "dashboard": self._build_dashboard_page(),
            "roi": self._build_roi_page(),
            "strategy": self._build_strategy_page(),
            "debug": self._build_debug_page(),
        }
        self._show_page("dashboard")

    def _create_page_frame(self) -> Tuple[ctk.CTkFrame, ctk.CTkFrame, ctk.CTkFrame]:
        page = ctk.CTkFrame(self.page_container, corner_radius=0, fg_color="transparent")
        page.grid_rowconfigure(0, weight=1)
        page.grid_columnconfigure(0, weight=3)
        page.grid_columnconfigure(1, weight=1)

        main_panel = ctk.CTkFrame(page, corner_radius=18)
        options_panel = ctk.CTkFrame(page, corner_radius=18)
        main_panel.grid(row=0, column=0, sticky="nsew", padx=(20, 10), pady=20)
        options_panel.grid(row=0, column=1, sticky="nsew", padx=(10, 20), pady=20)
        return page, main_panel, options_panel

    def _build_dashboard_page(self) -> ctk.CTkFrame:
        page, main_panel, options_panel = self._create_page_frame()

        header = ctk.CTkFrame(main_panel, corner_radius=16)
        header.pack(fill="x", padx=16, pady=16)
        ctk.CTkLabel(
            header,
            text="Dashboard",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(side="left", padx=16, pady=12)

        phase_frame = ctk.CTkFrame(header, corner_radius=12)
        phase_frame.pack(side="right", padx=16, pady=10)
        ctk.CTkLabel(phase_frame, text="페이즈 선택").pack(side="left", padx=10)
        self.phase_selector = ctk.CTkOptionMenu(
            phase_frame,
            values=["1-혼돈", "1-도올", "1-궁기", "2", "3"],
            command=self._on_phase_label_change,
        )
        self.phase_selector.set("1-혼돈")
        self.phase_selector.pack(side="left", padx=10, pady=6)

        cards = ctk.CTkFrame(main_panel, corner_radius=0, fg_color="transparent")
        cards.pack(fill="x", padx=16, pady=(0, 12))
        cards.grid_columnconfigure((0, 1, 2), weight=1, uniform="card")

        self.ocr_card = ctk.CTkFrame(cards, corner_radius=16)
        self.ocr_card.grid(row=0, column=0, padx=6, pady=6, sticky="nsew")
        ctk.CTkLabel(self.ocr_card, text="OCR 값", font=ctk.CTkFont(size=12, weight="bold")).pack(
            anchor="w", padx=14, pady=(12, 4)
        )
        self.ocr_value_label = ctk.CTkLabel(
            self.ocr_card,
            textvariable=self.gauge_label,
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        self.ocr_value_label.pack(anchor="w", padx=14, pady=(0, 14))

        self.status_card = ctk.CTkFrame(cards, corner_radius=16)
        self.status_card.grid(row=0, column=1, padx=6, pady=6, sticky="nsew")
        ctk.CTkLabel(self.status_card, text="상태", font=ctk.CTkFont(size=12, weight="bold")).pack(
            anchor="w", padx=14, pady=(12, 4)
        )
        self.stable_status_label = ctk.CTkLabel(
            self.status_card,
            text="안정",
            font=ctk.CTkFont(size=18, weight="bold"),
        )
        self.stable_status_label.pack(anchor="w", padx=14, pady=(0, 4))
        ctk.CTkLabel(
            self.status_card,
            textvariable=self.mode_label,
            font=ctk.CTkFont(size=12),
        ).pack(anchor="w", padx=14, pady=(0, 14))

        self.laser_card = ctk.CTkFrame(cards, corner_radius=16)
        self.laser_card.grid(row=0, column=2, padx=6, pady=6, sticky="nsew")
        ctk.CTkLabel(
            self.laser_card, text="추천 실", font=ctk.CTkFont(size=12, weight="bold")
        ).pack(anchor="w", padx=14, pady=(12, 4))
        self.laser_value_label = ctk.CTkLabel(
            self.laser_card,
            textvariable=self.laser_label,
            font=ctk.CTkFont(size=20, weight="bold"),
        )
        self.laser_value_label.pack(anchor="w", padx=14, pady=(0, 14))

        phase_status = ctk.CTkFrame(main_panel, corner_radius=16)
        phase_status.pack(fill="x", padx=16, pady=(4, 12))
        ctk.CTkLabel(
            phase_status,
            text="1페 진행 상태",
            font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(anchor="w", padx=14, pady=(10, 4))
        self.phase1_status_label = ctk.CTkLabel(
            phase_status, textvariable=self.phase1_status_var, font=ctk.CTkFont(size=12)
        )
        self.phase1_status_label.pack(anchor="w", padx=14, pady=(0, 12))

        log_frame = ctk.CTkFrame(main_panel, corner_radius=16)
        log_frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        ctk.CTkLabel(log_frame, text="상태 로그", font=ctk.CTkFont(size=12, weight="bold")).pack(
            anchor="w", padx=14, pady=(12, 4)
        )
        self.log_box = ctk.CTkTextbox(log_frame, height=160)
        self.log_box.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.log_box.configure(state="disabled")

        ctk.CTkLabel(
            options_panel,
            text="옵션",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(16, 8))

        self._build_dashboard_options(options_panel)
        return page

    def _build_dashboard_options(self, options_panel: ctk.CTkFrame) -> None:
        phase_actions = ctk.CTkFrame(options_panel, corner_radius=16)
        phase_actions.pack(fill="x", padx=16, pady=8)
        ctk.CTkLabel(phase_actions, text="페이즈 제어", font=ctk.CTkFont(size=12, weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 4)
        )
        self.phase1_next_var = ctk.StringVar(value="-")
        self.phase1_next_combo = ctk.CTkOptionMenu(
            phase_actions,
            values=["-"],
            variable=self.phase1_next_var,
            command=self._on_next_selected,
        )
        self.phase1_next_combo.pack(fill="x", padx=12, pady=4)
        self.phase1_move_button = ctk.CTkButton(
            phase_actions,
            text="선택한 페이즈로 이동",
            command=self.move_to_next_phase1,
        )
        self.phase1_move_button.pack(fill="x", padx=12, pady=(4, 10))

        control_card = ctk.CTkFrame(options_panel, corner_radius=16)
        control_card.pack(fill="x", padx=16, pady=8)
        ctk.CTkLabel(control_card, text="실행", font=ctk.CTkFont(size=12, weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 4)
        )
        ctk.CTkButton(control_card, text="Start", command=self.start).pack(
            fill="x", padx=12, pady=4
        )
        ctk.CTkButton(control_card, text="Stop", command=self.stop).pack(
            fill="x", padx=12, pady=4
        )
        ctk.CTkButton(
            control_card, text="페이즈 리셋(1페 초기화)", command=self.reset_phase_progress
        ).pack(fill="x", padx=12, pady=4)
        ctk.CTkButton(
            control_card, text="현재 페이즈 완료(1페 확정)", command=self.complete_current_phase1
        ).pack(fill="x", padx=12, pady=(4, 10))

        settings_card = ctk.CTkFrame(options_panel, corner_radius=16)
        settings_card.pack(fill="x", padx=16, pady=8)
        ctk.CTkLabel(settings_card, text="OCR 설정", font=ctk.CTkFont(size=12, weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 4)
        )

        ctk.CTkLabel(settings_card, text="업데이트 주기 (ms)").pack(
            anchor="w", padx=12, pady=(4, 0)
        )
        interval_slider = ctk.CTkSlider(
            settings_card,
            from_=100,
            to=1000,
            number_of_steps=18,
            variable=self.update_interval_var,
            command=lambda _value: self._sync_interval_entry(),
        )
        interval_slider.pack(fill="x", padx=12, pady=6)
        self.interval_entry = ctk.CTkEntry(settings_card, textvariable=self.update_interval_var)
        self.interval_entry.pack(fill="x", padx=12, pady=(0, 8))

        self.debug_switch = ctk.CTkSwitch(
            settings_card, text="DEBUG_OCR", variable=self.debug_var
        )
        self.debug_switch.pack(anchor="w", padx=12, pady=6)

        ctk.CTkLabel(settings_card, text="OCR 히스토리 길이").pack(anchor="w", padx=12)
        self.history_entry = ctk.CTkEntry(settings_card, textvariable=self.ocr_history_var)
        self.history_entry.pack(fill="x", padx=12, pady=4)
        ctk.CTkButton(
            settings_card, text="히스토리 적용", command=self._apply_history_length
        ).pack(fill="x", padx=12, pady=(0, 10))

        monitor_card = ctk.CTkFrame(options_panel, corner_radius=16)
        monitor_card.pack(fill="x", padx=16, pady=8)
        ctk.CTkLabel(monitor_card, text="모니터 선택", font=ctk.CTkFont(size=12, weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 4)
        )
        _, monitor_options = list_monitors()
        self.monitor_option_menu = ctk.CTkOptionMenu(
            monitor_card,
            values=[monitor_options[idx] for idx in sorted(monitor_options)],
            command=self._on_monitor_selected,
        )
        selected_label = monitor_options.get(self.monitor_index_var.get(), "Monitor 1")
        self.monitor_option_menu.set(selected_label)
        self.monitor_option_menu.pack(fill="x", padx=12, pady=(0, 10))

        stable_card = ctk.CTkFrame(options_panel, corner_radius=16)
        stable_card.pack(fill="x", padx=16, pady=8)
        ctk.CTkLabel(stable_card, text="모드", font=ctk.CTkFont(size=12, weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 4)
        )
        ctk.CTkSwitch(
            stable_card,
            text="Stable 모드 (500±tol이면 HOLD)",
            variable=self.stable_var,
        ).pack(anchor="w", padx=12, pady=(0, 10))

    def _build_roi_page(self) -> ctk.CTkFrame:
        page, main_panel, options_panel = self._create_page_frame()

        header = ctk.CTkFrame(main_panel, corner_radius=16)
        header.pack(fill="x", padx=16, pady=16)
        ctk.CTkLabel(
            header,
            text="ROI / Capture",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(side="left", padx=16, pady=12)

        preview_frame = ctk.CTkFrame(main_panel, corner_radius=16)
        preview_frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        ctk.CTkLabel(
            preview_frame, text="캡처 프리뷰", font=ctk.CTkFont(size=12, weight="bold")
        ).pack(anchor="w", padx=14, pady=(12, 4))
        self.roi_preview_label = ctk.CTkLabel(preview_frame, text="프리뷰 없음")
        self.roi_preview_label.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        action_frame = ctk.CTkFrame(main_panel, corner_radius=16)
        action_frame.pack(fill="x", padx=16, pady=(0, 16))
        ctk.CTkLabel(action_frame, text="ROI 작업", font=ctk.CTkFont(size=12, weight="bold")).pack(
            anchor="w", padx=14, pady=(10, 4)
        )
        ctk.CTkButton(action_frame, text="게이지 ROI 지정(전체)", command=self.set_gauge_roi).pack(
            fill="x", padx=14, pady=4
        )
        ctk.CTkButton(
            action_frame, text="숫자 ROI 지정(G→D→H)", command=self.set_digit_rois
        ).pack(fill="x", padx=14, pady=4)
        ctk.CTkButton(action_frame, text="현재 ROI 저장", command=self._save_roi).pack(
            fill="x", padx=14, pady=4
        )
        ctk.CTkButton(action_frame, text="ROI 불러오기", command=self._load_roi).pack(
            fill="x", padx=14, pady=4
        )
        ctk.CTkButton(action_frame, text="기본값 복원", command=self._reset_roi).pack(
            fill="x", padx=14, pady=(4, 10)
        )

        ctk.CTkLabel(
            options_panel,
            text="ROI 설정",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(16, 8))

        monitor_card = ctk.CTkFrame(options_panel, corner_radius=16)
        monitor_card.pack(fill="x", padx=16, pady=8)
        ctk.CTkLabel(monitor_card, text="모니터 선택", font=ctk.CTkFont(size=12, weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 4)
        )
        _, monitor_options = list_monitors()
        self.roi_monitor_menu = ctk.CTkOptionMenu(
            monitor_card,
            values=[monitor_options[idx] for idx in sorted(monitor_options)],
            command=self._on_monitor_selected,
        )
        selected_label = monitor_options.get(self.monitor_index_var.get(), "Monitor 1")
        self.roi_monitor_menu.set(selected_label)
        self.roi_monitor_menu.pack(fill="x", padx=12, pady=(0, 10))

        coord_card = ctk.CTkFrame(options_panel, corner_radius=16)
        coord_card.pack(fill="x", padx=16, pady=8)
        ctk.CTkLabel(coord_card, text="Gauge ROI 좌표", font=ctk.CTkFont(size=12, weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 4)
        )
        coord_grid = ctk.CTkFrame(coord_card, fg_color="transparent")
        coord_grid.pack(fill="x", padx=12, pady=6)
        coord_grid.grid_columnconfigure((0, 1), weight=1)

        self.roi_x_var = ctk.IntVar(value=0)
        self.roi_y_var = ctk.IntVar(value=0)
        self.roi_w_var = ctk.IntVar(value=0)
        self.roi_h_var = ctk.IntVar(value=0)
        self._sync_roi_entry_from_data()

        ctk.CTkLabel(coord_grid, text="X").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(coord_grid, text="Y").grid(row=0, column=1, sticky="w")
        ctk.CTkEntry(coord_grid, textvariable=self.roi_x_var).grid(
            row=1, column=0, sticky="ew", padx=(0, 6), pady=(0, 6)
        )
        ctk.CTkEntry(coord_grid, textvariable=self.roi_y_var).grid(
            row=1, column=1, sticky="ew", padx=(6, 0), pady=(0, 6)
        )
        ctk.CTkLabel(coord_grid, text="W").grid(row=2, column=0, sticky="w")
        ctk.CTkLabel(coord_grid, text="H").grid(row=2, column=1, sticky="w")
        ctk.CTkEntry(coord_grid, textvariable=self.roi_w_var).grid(
            row=3, column=0, sticky="ew", padx=(0, 6)
        )
        ctk.CTkEntry(coord_grid, textvariable=self.roi_h_var).grid(
            row=3, column=1, sticky="ew", padx=(6, 0)
        )
        ctk.CTkButton(coord_card, text="좌표 적용", command=self._apply_roi_coordinates).pack(
            fill="x", padx=12, pady=(6, 10)
        )

        scale_card = ctk.CTkFrame(options_panel, corner_radius=16)
        scale_card.pack(fill="x", padx=16, pady=8)
        ctk.CTkLabel(scale_card, text="프리뷰 스케일", font=ctk.CTkFont(size=12, weight="bold")).pack(
            anchor="w", padx=12, pady=(10, 4)
        )
        ctk.CTkSlider(
            scale_card,
            from_=0.5,
            to=2.0,
            number_of_steps=15,
            variable=self.preview_scale_var,
            command=lambda _v: self._refresh_preview(),
        ).pack(fill="x", padx=12, pady=(0, 10))

        return page

    def _build_strategy_page(self) -> ctk.CTkFrame:
        page, main_panel, options_panel = self._create_page_frame()

        header = ctk.CTkFrame(main_panel, corner_radius=16)
        header.pack(fill="x", padx=16, pady=16)
        ctk.CTkLabel(
            header,
            text="Strategy / Simulator",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(side="left", padx=16, pady=12)

        sim_card = ctk.CTkFrame(main_panel, corner_radius=16)
        sim_card.pack(fill="x", padx=16, pady=(0, 16))
        ctk.CTkLabel(sim_card, text="시뮬레이션", font=ctk.CTkFont(size=12, weight="bold")).pack(
            anchor="w", padx=14, pady=(10, 4)
        )
        sim_grid = ctk.CTkFrame(sim_card, fg_color="transparent")
        sim_grid.pack(fill="x", padx=14, pady=6)
        sim_grid.grid_columnconfigure((0, 1, 2), weight=1)

        self.sim_g_var = ctk.IntVar(value=500)
        self.sim_d_var = ctk.IntVar(value=500)
        self.sim_h_var = ctk.IntVar(value=500)

        ctk.CTkEntry(sim_grid, textvariable=self.sim_g_var).grid(
            row=1, column=0, sticky="ew", padx=4
        )
        ctk.CTkEntry(sim_grid, textvariable=self.sim_d_var).grid(
            row=1, column=1, sticky="ew", padx=4
        )
        ctk.CTkEntry(sim_grid, textvariable=self.sim_h_var).grid(
            row=1, column=2, sticky="ew", padx=4
        )
        ctk.CTkLabel(sim_grid, text="G").grid(row=0, column=0)
        ctk.CTkLabel(sim_grid, text="D").grid(row=0, column=1)
        ctk.CTkLabel(sim_grid, text="H").grid(row=0, column=2)

        self.sim_result_label = ctk.CTkLabel(sim_card, text="추천 실: -", font=ctk.CTkFont(size=16, weight="bold"))
        self.sim_result_label.pack(anchor="w", padx=14, pady=(6, 12))
        ctk.CTkButton(sim_card, text="시뮬레이션 실행", command=self._run_simulation).pack(
            fill="x", padx=14, pady=(0, 12)
        )

        ctk.CTkLabel(
            options_panel,
            text="전략 파라미터",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(16, 8))
        ctk.CTkLabel(
            options_panel,
            text="tol/가중치 옵션은 TODO",
            text_color="gray",
        ).pack(anchor="w", padx=16, pady=(0, 8))

        return page

    def _build_debug_page(self) -> ctk.CTkFrame:
        page, main_panel, options_panel = self._create_page_frame()

        header = ctk.CTkFrame(main_panel, corner_radius=16)
        header.pack(fill="x", padx=16, pady=16)
        ctk.CTkLabel(
            header,
            text="Debug",
            font=ctk.CTkFont(size=20, weight="bold"),
        ).pack(side="left", padx=16, pady=12)

        images_frame = ctk.CTkFrame(main_panel, corner_radius=16)
        images_frame.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        images_frame.grid_columnconfigure((0, 1, 2), weight=1)

        self.debug_image_labels = {}
        for idx, key in enumerate(("G", "D", "H")):
            card = ctk.CTkFrame(images_frame, corner_radius=14)
            card.grid(row=0, column=idx, padx=6, pady=6, sticky="nsew")
            ctk.CTkLabel(card, text=f"{key} OCR", font=ctk.CTkFont(size=12, weight="bold")).pack(
                anchor="w", padx=12, pady=(10, 4)
            )
            label = ctk.CTkLabel(card, text="이미지 없음")
            label.pack(fill="both", expand=True, padx=12, pady=(0, 12))
            self.debug_image_labels[key] = label

        text_card = ctk.CTkFrame(main_panel, corner_radius=16)
        text_card.pack(fill="x", padx=16, pady=(0, 16))
        ctk.CTkLabel(
            text_card, text="OCR Raw Text", font=ctk.CTkFont(size=12, weight="bold")
        ).pack(anchor="w", padx=14, pady=(10, 4))
        self.debug_textbox = ctk.CTkTextbox(text_card, height=140)
        self.debug_textbox.pack(fill="x", padx=14, pady=(0, 12))
        self.debug_textbox.configure(state="disabled")

        ctk.CTkLabel(
            options_panel,
            text="전처리 옵션",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(anchor="w", padx=16, pady=(16, 8))
        ctk.CTkLabel(
            options_panel,
            text="blur/threshold 옵션은 TODO",
            text_color="gray",
        ).pack(anchor="w", padx=16, pady=(0, 8))

        return page

    def _show_page(self, key: str) -> None:
        for page in self.pages.values():
            page.grid_forget()
        page = self.pages[key]
        page.grid(row=0, column=0, sticky="nsew")
        for btn_key, btn in self.nav_buttons.items():
            btn.configure(fg_color=("#1f6aa5" if btn_key == key else "transparent"))

    # =========================
    # ROI Selection
    # =========================
    def set_gauge_roi(self):
        screenshot, offset, used_index = self.service.capture_screen_with_offset(
            self.monitor_index_var.get()
        )
        self.roi_data["monitor_offset"] = offset
        self.roi_data["monitor_index"] = used_index
        save_roi_data(self.roi_data)
        self._refresh_preview(image=screenshot)
        RoiSelector(self, screenshot, self._on_gauge_selected, "게이지 ROI 선택")

    def set_digit_rois(self):
        if not self.roi_data.get("gauge_roi"):
            messagebox.showwarning("경고", "먼저 게이지 ROI를 지정하세요.")
            return

        screenshot, offset, used_index = self.service.capture_screen_with_offset(
            self.monitor_index_var.get()
        )
        self.roi_data["monitor_offset"] = offset
        self.roi_data["monitor_index"] = used_index
        save_roi_data(self.roi_data)

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
            left, top, right, bottom = rect
            if (right - left) < 20 or (bottom - top) < 20:
                messagebox.showwarning("경고", "ROI가 너무 작습니다. 다시 지정해주세요.")
                return
            self.roi_data["gauge_roi"] = rect
            save_roi_data(self.roi_data)
            self._sync_roi_entry_from_data()

    def _on_digit_selected(self, rect, label):
        if rect:
            left, top, right, bottom = rect
            if (right - left) < 10 or (bottom - top) < 10:
                messagebox.showwarning("경고", f"{label} ROI가 너무 작습니다. 다시 지정해주세요.")
                return
            self.roi_data["digit_rois"][label] = rect
            save_roi_data(self.roi_data)
        self._digit_index += 1
        self._prompt_digit_roi()

    def _save_roi(self):
        save_roi_data(self.roi_data)
        self._log("ROI 저장 완료")

    def _load_roi(self):
        self.roi_data = load_roi_data()
        self.service.update_roi_data(self.roi_data)
        self.monitor_index_var.set(self.roi_data.get("monitor_index", MONITOR_INDEX))
        _, monitor_options = list_monitors()
        label = monitor_options.get(self.monitor_index_var.get(), "Monitor 1")
        if hasattr(self, "monitor_option_menu"):
            self.monitor_option_menu.set(label)
        if hasattr(self, "roi_monitor_menu"):
            self.roi_monitor_menu.set(label)
        self._sync_roi_entry_from_data()
        self._log("ROI 불러오기 완료")

    def _reset_roi(self):
        self.roi_data = default_roi_data()
        save_roi_data(self.roi_data)
        self.service.update_roi_data(self.roi_data)
        self.monitor_index_var.set(self.roi_data.get("monitor_index", MONITOR_INDEX))
        _, monitor_options = list_monitors()
        label = monitor_options.get(self.monitor_index_var.get(), "Monitor 1")
        if hasattr(self, "monitor_option_menu"):
            self.monitor_option_menu.set(label)
        if hasattr(self, "roi_monitor_menu"):
            self.roi_monitor_menu.set(label)
        self._sync_roi_entry_from_data()
        self._log("ROI 기본값 복원")

    def _apply_roi_coordinates(self):
        x = self.roi_x_var.get()
        y = self.roi_y_var.get()
        w = self.roi_w_var.get()
        h = self.roi_h_var.get()
        if w <= 0 or h <= 0:
            messagebox.showwarning("경고", "폭/높이는 0보다 커야 합니다.")
            return
        self.roi_data["gauge_roi"] = (x, y, x + w, y + h)
        save_roi_data(self.roi_data)
        self._log("Gauge ROI 좌표 적용")

    # =========================
    # Start/Stop/Loop
    # =========================
    def start(self):
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
        self._log("루프 시작")
        self._schedule_loop()

    def stop(self):
        self.running = False
        if self._loop_after:
            self.after_cancel(self._loop_after)
            self._loop_after = None
        self._log("루프 정지")

    def _schedule_loop(self):
        if not self.running:
            return
        interval = max(100, int(self.update_interval_var.get()))
        self._loop_after = self.after(interval, self._process_frame)

    def _process_frame(self):
        if not self.running:
            return

        try:
            result = self.service.process_frame(
                self.phase_var.get(),
                self.stable_var.get(),
                self._phase1_next_choice,
                debug_ocr=self.debug_var.get(),
            )
            self._update_ui_from_result(result)
        except Exception as exc:
            self.running = False
            messagebox.showerror("루프 에러", str(exc))
            return

        self._schedule_loop()

    def _update_ui_from_result(self, result: GaugeResult) -> None:
        if result.gauge:
            self.gauge_label.set(
                f"G: {result.gauge.G}  D: {result.gauge.D}  H: {result.gauge.H}"
            )
            stable = is_stable(result.gauge)
        else:
            self.gauge_label.set("G: -  D: -  H: -")
            stable = False

        self.laser_label.set(f"추천 실: {result.laser}")

        color_map = {"RED": "#e53935", "GREEN": "#43a047", "YELLOW": "#fbc02d", "HOLD": "#90caf9"}
        self.laser_value_label.configure(text_color=color_map.get(result.laser, "white"))

        mode_tag = "STABLE" if self.stable_var.get() else "PREP"
        self.mode_label.set(f"모드: {mode_tag}  |  현재: {self.phase_var.get()}")
        self.stable_status_label.configure(text="안정" if stable else "불안정")

        overlay_phase = f"{self.phase_var.get()} [{mode_tag}]"
        if self._phase1_next_choice:
            next_label = PHASE1_LABELS.get(self._phase1_next_choice, self._phase1_next_choice)
            overlay_phase = f"{overlay_phase} → NEXT: {next_label}"
        self.overlay.update_text(overlay_phase, result.gauge, result.laser)

        if result.ignored_spike:
            self._log("OCR 스파이크 감지: 1회 무시")

        if result.debug:
            self._update_debug_view(result)

    # =========================
    # Debug UI
    # =========================
    def _update_debug_view(self, result: GaugeResult) -> None:
        debug = result.debug
        if not debug:
            return
        for key, image in debug.processed_images.items():
            pil_img = Image.fromarray(image)
            pil_img = pil_img.resize((220, 140))
            photo = ImageTk.PhotoImage(pil_img)
            label = self.debug_image_labels.get(key)
            if label:
                label.configure(image=photo, text="")
                label.image = photo

        text_lines = [f"{key}: {debug.raw_text.get(key, '').strip()}" for key in ("G", "D", "H")]
        self.debug_textbox.configure(state="normal")
        self.debug_textbox.delete("1.0", "end")
        self.debug_textbox.insert("end", "\n".join(text_lines))
        self.debug_textbox.configure(state="disabled")

    # =========================
    # Helpers
    # =========================
    def _on_phase_label_change(self, label: str) -> None:
        map_label = {
            "1-혼돈": "1-HONDON",
            "1-도올": "1-DOOL",
            "1-궁기": "1-GUNGGI",
            "2": "2",
            "3": "3",
        }
        self.phase_var.set(map_label.get(label, label))

    def _sync_interval_entry(self) -> None:
        self.interval_entry.delete(0, "end")
        self.interval_entry.insert(0, str(self.update_interval_var.get()))

    def _apply_history_length(self) -> None:
        new_len = max(1, int(self.ocr_history_var.get()))
        self.service.reset_history(new_len)
        self._log(f"OCR 히스토리 길이 적용: {new_len}")

    def _on_monitor_selected(self, label: str) -> None:
        _, monitor_options = list_monitors()
        for idx, name in monitor_options.items():
            if name == label:
                self.monitor_index_var.set(idx)
                self.roi_data["monitor_index"] = idx
                save_roi_data(self.roi_data)
                if hasattr(self, "monitor_option_menu"):
                    self.monitor_option_menu.set(label)
                if hasattr(self, "roi_monitor_menu"):
                    self.roi_monitor_menu.set(label)
                break

    def _sync_roi_entry_from_data(self) -> None:
        roi = self.roi_data.get("gauge_roi")
        if not roi:
            self.roi_x_var.set(0)
            self.roi_y_var.set(0)
            self.roi_w_var.set(0)
            self.roi_h_var.set(0)
            return
        x1, y1, x2, y2 = roi
        self.roi_x_var.set(x1)
        self.roi_y_var.set(y1)
        self.roi_w_var.set(max(0, x2 - x1))
        self.roi_h_var.set(max(0, y2 - y1))

    def _refresh_preview(self, image: Optional[Image.Image] = None) -> None:
        if image is None:
            screenshot, _, _ = self.service.capture_screen_with_offset(
                self.monitor_index_var.get()
            )
            image = screenshot

        scale = float(self.preview_scale_var.get())
        new_size = (int(image.width * scale), int(image.height * scale))
        preview_image = image.resize(new_size)
        photo = ImageTk.PhotoImage(preview_image)
        self.roi_preview_label.configure(image=photo, text="")
        self.roi_preview_label.image = photo

    def _apply_theme(self, mode: str) -> None:
        ctk.set_appearance_mode(mode)

    def _log(self, message: str) -> None:
        self._history_log.appendleft(message)
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.insert("end", "\n".join(self._history_log))
        self.log_box.configure(state="disabled")

    # =========================
    # Phase1 UI logic
    # =========================
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

        self.service.reset_history(self.ocr_history_var.get())
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
            self.phase1_next_combo.configure(values=["-"])
            self.phase1_next_var.set("-")
            self._phase1_next_choice = None
            self.phase1_move_button.configure(state="disabled")
            messagebox.showinfo("알림", "1페 흉수 3개 완료. 2페로 진행하세요.")
            return

        self.phase1_next_combo.configure(values=remaining)

        if len(remaining) == 1:
            self.phase1_next_var.set(remaining[0])
            self._phase1_next_choice = remaining[0]
        else:
            if self.phase1_next_var.get() not in remaining:
                self.phase1_next_var.set(remaining[0])
            self._phase1_next_choice = self.phase1_next_var.get()

        self.phase1_move_button.configure(state="normal")

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

    def _run_simulation(self):
        try:
            gauge = Gauge(
                int(self.sim_g_var.get()), int(self.sim_d_var.get()), int(self.sim_h_var.get())
            )
        except ValueError:
            messagebox.showwarning("경고", "G/D/H 값을 숫자로 입력하세요.")
            return
        phase = self.phase_var.get()
        target = None
        if phase in PHASE1_LIST and self._phase1_next_choice:
            target = PREP_TARGETS2.get((phase, self._phase1_next_choice))
        laser = choose_best_laser(
            gauge,
            X_TABLE[phase],
            allowed_colors=PHASE_ALLOWED_COLORS[phase],
            target=target,
        )
        self.sim_result_label.configure(text=f"추천 실: {laser}")

    # =========================
    # Close
    # =========================
    def _on_close(self):
        self.stop()
        self.roi_data["overlay_pos"] = self.overlay.get_position()
        save_roi_data(self.roi_data)
        self.overlay.destroy()
        self.destroy()
