# Kaing Gauge Helper

## 프로젝트 개요

메이플스토리 보스 **카링**의 G/D/H 게이지 숫자를 OCR로 읽고, 현재 페이즈 기준으로 안전한 실(레이저) 색을 추천하는 데스크톱 유틸리티입니다. 사용자가 직접 페이즈와 ROI를 지정하므로 자동 판별은 하지 않습니다.

## 무엇을 위한 도구인가?

- 보스전 중 게이지 수치를 빠르게 확인하기 어려운 상황을 보조합니다.
- 실 색을 선택해야 하는 순간에 **실수 확률을 줄이고 안정적으로 판단**할 수 있게 돕습니다.

## 주요 기능

- **페이즈 수동 선택**: 1-혼돈 / 1-도올 / 1-궁기 / 2 / 3 중 하나 선택
- **ROI 드래그 지정**: 게이지 전체 및 숫자 영역(G→D→H)을 마우스로 지정
- **게이지 OCR**: mss + OpenCV 전처리 + Tesseract OCR
- **실 추천 로직**: 규칙 기반 시뮬레이션 + depth=2 lookahead
- **오버레이 표시**: 항상 위/반투명/드래그 가능
- **다크/라이트 테마**: System/Dark/Light 토글

## 실행 환경

- **OS**: Windows 10/11, macOS
- **Python**: 3.9 이상 권장
- **필수 라이브러리**:
  - customtkinter
  - mss
  - opencv-python
  - pillow
  - pytesseract
  - numpy

## 설치 방법

1) **Python 설치**
   - https://www.python.org 에서 3.9+ 버전 설치

2) **필수 패키지 설치**

```bash
pip install customtkinter mss opencv-python pillow pytesseract numpy
```

3) **Tesseract 설치 및 경로 설정**
   - Windows용 Tesseract 설치 후 경로 확인
   - 기본 경로 예시: `C:\Program Files\Tesseract-OCR\tesseract.exe`
   - `core.py` 상단의 `TESSERACT_CMD` 값을 자신의 설치 경로로 수정

## 실행 방법

```bash
python main.py
```

### 최초 실행 시 설정 순서

1. **게이지 ROI 지정(전체)** 클릭 → 전체 게이지 영역 드래그
2. **숫자 ROI 지정(G→D→H)** 클릭 → 게이지 내부 숫자 영역을 순서대로 선택
3. **Start** 클릭

## UI 페이지 안내 및 기능 연결

| 페이지 | UI 요소 | 연결된 기존 기능 |
| --- | --- | --- |
| Dashboard | Start/Stop | `App.start()` / `App.stop()` (기존 루프 유지) |
| Dashboard | 페이즈 리셋/완료 | `reset_phase_progress()` / `complete_current_phase1()` |
| Dashboard | OCR 표시/추천 실 | `GaugeService.process_frame()` + `choose_best_laser()` |
| ROI / Capture | 게이지 ROI 지정 | `set_gauge_roi()` → `RoiSelector` |
| ROI / Capture | 숫자 ROI 지정 | `set_digit_rois()` → `RoiSelector` |
| ROI / Capture | ROI 저장/불러오기 | `save_roi_data()` / `load_roi_data()` |
| Debug | OCR 이미지/텍스트 | `GaugeService._read_gauge(debug=True)` 결과 표시 |
| Strategy | 시뮬레이션 | `choose_best_laser()` 호출 |

## 코드 구조

- `core.py`: 캡처/OCR/전략/ROI 데이터 처리 (기능 로직)
- `ui_app.py`: CustomTkinter UI 및 화면 구성 (뷰)
- `main.py`: 실행 진입점

## macOS 참고 사항

- macOS에서는 기본적으로 시스템 폰트가 자동 적용됩니다.
- 고해상도 디스플레이에서 텍스트가 작으면 CustomTkinter의 스케일링을 사용할 수 있습니다:

```python
import customtkinter as ctk
ctk.set_widget_scaling(1.0)
```

## PyInstaller 패키징

### Windows

```bash
pyinstaller --noconsole --onefile --name kaling-helper main.py
```

### macOS

```bash
pyinstaller --noconsole --onefile --name kaling-helper main.py
```

## 주의사항

- **해상도/게임 UI 스케일 변경 시 ROI를 반드시 다시 지정**해야 합니다.
- OCR 인식이 불안정한 경우:
  - 숫자 ROI 영역을 더 정확히 지정
  - 게임 내 밝기/대비 조정
  - 전투 이펙트가 적은 순간에 ROI 재지정
