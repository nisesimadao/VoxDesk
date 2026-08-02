# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller の設定。

配布物に含めないもの:
  torch / demucs  … 3 GB を超えるうえ、GitHub のリリースは 1 ファイル 2 GB まで。
                    ボーカル除去を使う人だけ、あとから入れてもらう。
"""

import os
import sys

from PyInstaller.utils.hooks import collect_all

ROOT = os.path.dirname(os.path.abspath(SPECPATH))  # SPECPATH は packaging フォルダ
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

datas, binaries, hiddenimports = [], [], []
# ネイティブのライブラリを抱えているものは、まとめて取り込まないと実行時に落ちる。
# sounddevice 本体は単一モジュールで、PortAudio の DLL は別パッケージ
# _sounddevice_data に入っている。これを忘れると音が一切出ない。
# numpy / scipy は既定のフックだけでは内部モジュールを取りこぼすことがある
#（numpy 2.5 で "No module named 'numpy._core._exceptions'" になった）
for package in ("_sounddevice_data", "av", "pedalboard", "yt_dlp", "numpy", "scipy"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

# 子プロセス（プラグイン画面）は app.py --vst-editor から呼ばれる
hiddenimports += ["vst_editor_host", "sounddevice"]

analysis = Analysis(
    [os.path.join(ROOT, "app.py")],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    excludes=["torch", "demucs", "matplotlib", "pytest", "IPython"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="KaraokeStudio",
    debug=False,
    strip=False,
    upx=False,
    # 普段はコンソール窓を出さない。KS_CONSOLE=1 で作ると
    # 起動時の例外がその場に表示され、原因を追える
    console=bool(os.environ.get("KS_CONSOLE")),
    disable_windowed_traceback=False,
)

collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="KaraokeStudio",
)

if sys.platform == "darwin":
    app = BUNDLE(
        collection,
        name="KaraokeStudio.app",
        bundle_identifier="net.raiid.karaokestudio",
        info_plist={
            "CFBundleName": "KaraokeStudio",
            "CFBundleDisplayName": "カラオケスタジオ",
            "NSHighResolutionCapable": True,
            # macOS ではマイク利用の理由を書かないと録音が拒否される
            "NSMicrophoneUsageDescription":
                "マイクの音をスピーカーへ流し、カラオケとして使うために利用します。",
        },
    )
