# Data Preprocessor

종합소득세 신고서와 선택 입력 재무제표를 로컬에서 분석해 엑셀 한 파일로 저장합니다.

## Windows

64비트 Python 3.12(Tcl/Tk 포함)를 설치한 다음 PowerShell에서 실행합니다.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe install_models.py
.\.venv\Scripts\python.exe desktop.py
```

## macOS

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python install_models.py
.venv/bin/python desktop.py
```

첫 설치에서 공개 모델 가중치를 내려받습니다. 이후 신고서, 재무상태표, 손익계산서 PDF를 선택한 뒤 **추출 시작 → 엑셀 저장**을 누릅니다. 재무제표는 선택 사항입니다. 신고서와 추출 결과는 로컬에서 처리됩니다.
