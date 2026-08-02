#!/usr/bin/env bash
# ソースから配布物を作る（macOS / Linux）。
#
#   git clone https://github.com/nisesimadao/VoxDesk.git
#   cd VoxDesk
#   chmod +x build.sh && ./build.sh
#
# 出来上がるもの:
#   macOS  dist/VoxDesk.app と dist/installer/VoxDesk-<版>-macos-<種別>.dmg
#   Linux  dist/VoxDesk/ と dist/installer/VoxDesk-<版>-linux-<種別>.tar.gz
#
# Intel Mac 向けはリリースに置いていないので、これで作ってください。
set -euo pipefail
cd "$(dirname "$0")"

VERSION="${1:-dev}"
PYTHON="${PYTHON:-python3}"
OS="$(uname -s)"
ARCH="$(uname -m)"

say() { printf "\n\033[1m== %s\033[0m\n" "$1"; }
die() { printf "\033[31m%s\033[0m\n" "$1" >&2; exit 1; }

say "確認"
command -v "$PYTHON" >/dev/null 2>&1 || die "python3 が見つかりません。Python 3.10 以上を入れてください。"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "Python 3.10 以上が必要です（今: $("$PYTHON" -V)）"
echo "  $("$PYTHON" -V) / $OS $ARCH"

if ! "$PYTHON" -c "import tkinter" >/dev/null 2>&1; then
    echo "tkinter が入っていません。次で入れてください:" >&2
    case "$OS" in
        Darwin) echo "  brew install python-tk" >&2 ;;
        Linux)  echo "  sudo apt install python3-tk     (Debian/Ubuntu)" >&2
                echo "  sudo dnf install python3-tkinter (Fedora)" >&2 ;;
    esac
    exit 1
fi

if [ "$OS" = "Linux" ] && ! ldconfig -p 2>/dev/null | grep -q libportaudio; then
    echo "警告: PortAudio が見つかりません。実行時に音が出ません。" >&2
    echo "  sudo apt install libportaudio2   (Debian/Ubuntu)" >&2
fi

say "仮想環境を用意"
[ -x ".venv/bin/python" ] || "$PYTHON" -m venv .venv
.venv/bin/python -m pip install --upgrade pip -q
.venv/bin/python -m pip install -q -r requirements.txt
.venv/bin/python -m pip install -q "pyinstaller==6.11.1"

say "アイコンを作る"
.venv/bin/python packaging/make_icon.py || echo "  アイコンの生成に失敗（既定のもので続行）"

say "実行ファイルを作る"
.venv/bin/python -m PyInstaller --noconfirm --clean packaging/VoxDesk.spec

mkdir -p dist/installer
case "$OS" in
    Darwin)
        say "dmg を作る"
        rm -rf dmg && mkdir -p dmg
        cp -R "dist/VoxDesk.app" dmg/
        ln -s /Applications dmg/Applications
        case "$ARCH" in
            arm64) LABEL="macos-arm64" ;;
            *)     LABEL="macos-x86_64" ;;
        esac
        OUT="dist/installer/VoxDesk-${VERSION}-${LABEL}.dmg"
        rm -f "$OUT"
        hdiutil create -volname "VoxDesk" -srcfolder dmg -ov -format UDZO "$OUT" >/dev/null
        rm -rf dmg
        ;;
    Linux)
        say "tar.gz を作る"
        cp README.md dist/VoxDesk/ 2>/dev/null || true
        chmod +x dist/VoxDesk/VoxDesk
        OUT="dist/installer/VoxDesk-${VERSION}-linux-${ARCH}.tar.gz"
        tar czf "$OUT" -C dist VoxDesk
        ;;
    *)
        die "この OS には対応していません: $OS"
        ;;
esac

say "完成"
ls -lh "$OUT"
echo
echo "そのまま動かすなら: ./start.sh"
