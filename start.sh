#!/usr/bin/env bash
# macOS / Linux 用の起動スクリプト。初回は必要なものを自動で入れる。
#   chmod +x start.sh && ./start.sh
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "python3 が見つかりません。Python 3.10 以上を入れてください。" >&2
    exit 1
fi

# tkinter は OS のパッケージとして別に入れる必要があることが多い
if ! "$PYTHON" -c "import tkinter" >/dev/null 2>&1; then
    echo "tkinter が入っていません。次のように入れてください:" >&2
    case "$(uname -s)" in
        Darwin) echo "  brew install python-tk" >&2 ;;
        Linux)  echo "  sudo apt install python3-tk     (Debian/Ubuntu)" >&2
                echo "  sudo dnf install python3-tkinter (Fedora)" >&2 ;;
    esac
    exit 1
fi

# Linux では PortAudio 本体も必要
if [ "$(uname -s)" = "Linux" ] && ! ldconfig -p 2>/dev/null | grep -q libportaudio; then
    echo "PortAudio が見つかりません。次のように入れてください:" >&2
    echo "  sudo apt install libportaudio2   (Debian/Ubuntu)" >&2
    echo "  sudo dnf install portaudio       (Fedora)" >&2
fi

if [ ! -x ".venv/bin/python" ]; then
    echo "初回セットアップを行います。数分かかります..."
    "$PYTHON" -m venv .venv
    .venv/bin/python -m pip install --upgrade pip -q
    .venv/bin/python -m pip install -r requirements.txt
fi

exec .venv/bin/python app.py
