@echo off
rem Kaynak koddan çalıştırma (Windows): arayüzü tarayıcıda açar.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe py -3 -m venv .venv
.venv\Scripts\pip install -q --disable-pip-version-check -r requirements.txt
.venv\Scripts\python gui.py
