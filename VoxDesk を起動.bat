@echo off
rem ダブルクリックで起動する。初回は必要なものを自動で入れる。
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo 初回セットアップを行います。数分かかります...
    py -3.12 -m venv .venv 2>nul || python -m venv .venv
    ".venv\Scripts\python.exe" -m pip install --upgrade pip -q
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo セットアップに失敗しました。
        pause
        exit /b 1
    )
)

start "" ".venv\Scripts\pythonw.exe" app.py
