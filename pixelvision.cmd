@echo off
setlocal
set "ROOT=%~dp0"
set "PY=%ROOT%.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo pixelvision: no virtualenv at "%ROOT%.venv".
  echo Run:  powershell -ExecutionPolicy Bypass -File "%ROOT%scripts\bootstrap.ps1"
  exit /b 2
)
"%PY%" "%ROOT%scripts\capture.py" %*
