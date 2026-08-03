r"""VoxDesk — マイクを流しながら、オフボーカル音源を動画付きで再生する。

起動:
    Windows       VoxDesk を起動.bat（または .venv\Scripts\pythonw.exe app.py）
    macOS / Linux ./start.sh（または .venv/bin/python app.py）

構成:
    app.py          入口（ここ）
    wxui.py         画面（wxPython。OS 本物の部品を使う）
    tkui.py         以前の画面（Tk）。--tk で使える。当面の逃げ道として残してある
    uicommon.py     画面まわりで共通の定数と小物
    router.py       マイク → スピーカーの経路（レート変換とドリフト補正つき）
    mic_chain.py    マイクの音作り（ノイズ除去・ゲート・コンプ・エコー・VST3）
    juce_thread.py  VST3（JUCE）専用のスレッド
    player.py       動画再生（PyAV でデコードし、音は選んだデバイスへ）
    music_search.py YouTube から曲を探す
    lyrics.py       時刻つき歌詞（LRCLIB）
    ranking.py      カラオケの人気ランキング
    separator.py    AI でボーカルを消す（Demucs）
    webserver.py    スマホをリモコンにする小さなサーバ
    devices.py      デバイスの一覧と診断
    platform_support.py  OS ごとの違い（保存先・Host API・フォント・プラグイン探索先）
"""

from __future__ import annotations

import sys

# プラグイン画面用の子プロセスは、この下の重い読み込み（numpy / 音声 / 動画）を
# 必要としない。インストーラ版では自分自身を起動するため、ここで先に分岐して
# 起動を数秒短くする。
if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--vst-editor":
    import vst_editor_host

    sys.argv = [sys.argv[0], *sys.argv[2:]]
    sys.exit(vst_editor_host.main())

# ボーカル除去が使えるかの判定も、torch を画面のプロセスに持ち込まないよう
# 別プロセスで行う（DLL の読み込みが画面を固まらせるため）。
if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--capability":
    import separator

    print(separator.capability_json())
    sys.exit(0)


def main() -> None:
    if "--tk" in sys.argv:  # 以前の画面。何かあったときの逃げ道
        import tkui

        tkui.main()
        return
    import wxui

    wxui.main()


if __name__ == "__main__":
    main()
