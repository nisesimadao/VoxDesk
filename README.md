# カラオケスタジオ

マイクの音を好きなスピーカーへ流しながら、曲のオフボーカル音源を **動画つき** で再生します。
動画をそのまま映すので、歌詞つきのカラオケ動画ならそのまま歌えます。

外部の再生ソフトやプラグインは不要です。Python とそのライブラリだけで動きます。

## 入手する

[リリースページ](../../releases/latest) から、お使いの OS のものを落としてください。

| OS | ファイル | 手順 |
|---|---|---|
| Windows | `*-windows-x64-setup.exe` | 実行してインストール |
| macOS (Apple Silicon) | `*-macos-arm64.dmg` | 開いて Applications へドラッグ |
| macOS (Intel) | `*-macos-x86_64.dmg` | 同上 |
| Linux | `*-linux-x86_64.tar.gz` | 展開して `./KaraokeStudio` |

macOS は署名していないため、初回は右クリック →「開く」で許可してください。
Linux は `sudo apt install libportaudio2` が必要です。

## ソースから動かす

| OS | 起動方法 |
|---|---|
| Windows | `カラオケスタジオを起動.bat` をダブルクリック |
| macOS / Linux | `chmod +x start.sh && ./start.sh` |

初回のみ数分の準備（仮想環境の作成と依存のインストール）が走ります。

macOS では `brew install python-tk`、Linux では `sudo apt install python3-tk libportaudio2`
（Fedora なら `python3-tkinter portaudio`）が別途必要です。起動スクリプトが不足を検出して案内します。

1. **カラオケ** タブで曲名を入れて検索 → 「オフボーカル度」が高いものを選んで再生
2. **マイク** タブで、マイクと出力先を選んで「マイクを入れる」
3. 声が小さい / ノイズが多いときは、プリセットを選ぶかスライダーを動かす

うまく動かないときは **設定・診断** タブの「すべて調べる」。
使えるデバイス、音が来ていないデバイス、Windows 側でミュートされているデバイスが一覧で分かります。

## オフボーカル音源の入手先

アプリの「音源の入手先」ボタンからも見られます。

| 入手先 | 内容 |
|---|---|
| 公式のカラオケ配信元 | カラオケ歌っちゃ王 / JOYSOUND / DAM CHANNEL など。検索欄の「公式カラオケ配信元のみ」で絞れます（結果に ✓ が付きます） |
| Karaoke Version (karaoke-version.jp) | 1 曲ずつ購入して MP3 を保存できる正規サービス。パートごとのミュートも可能。落としたファイルは「ファイルを開く」で再生 |
| ピアプロ / BOOTH / ニコニ・コモンズ | ボカロ・同人系。作者本人が off vocal を配布していることが多い |
| 手持ちの音源 | 正規に持っている曲なら「ファイルを開く」でそのまま再生できます |

Apple Music や Spotify は音声が保護されているため取り込めません。

## ボーカルを消す（任意機能）

「ボーカルを消す」を押すと、AI（Demucs）が曲からボーカルだけを取り除き、そのまま再生します。
検索結果の曲にも、「ファイルを開く」で読み込んだ手持ちの曲にも使えます。
作ったものは保存され、同じ曲を次に選んだときは待ち時間なしで再生されます。

この機能は動作環境を満たす PC でだけ有効になります（満たさない場合はボタンが押せず、理由が横に出ます）。

| 環境 | 必要な水準 |
|---|---|
| NVIDIA GPU（Windows / Linux） | VRAM 4 GB 以上、メモリ 8 GB 以上。VRAM 8 GB 以上で高品質モデル |
| Apple Silicon（macOS） | メモリ 16 GB 以上。32 GB 以上で高品質モデル |
| それ以外 | 使えません（CPU だけで動かすと 1 曲に数十分かかるため、あえて無効にしています） |

導入（合計 3 GB ほどダウンロードします）:

```powershell
.\.venv\Scripts\python.exe -m pip install torch --index-url https://download.pytorch.org/whl/cu126
.\.venv\Scripts\python.exe -m pip install -r requirements-vocal.txt
```

実測（RTX 3090 / 高品質モデル）: 4 分の曲でおよそ 20〜30 秒。
作った音源は「設定・診断」タブでまとめて削除できます。

## マイクの選び方

| つなぎ方 | プリセット | 備考 |
|---|---|---|
| カラオケ用の有線マイクを PC のマイク端子へ直挿し | カラオケマイク（有線・直挿し） | 出力が 20〜30 dB 足りないので大きく増幅します。ノイズも増えるためノイズ除去とハム除去が既定で入ります |
| USB マイク / ヘッドセット | USBマイク・ヘッドセット | いちばん安定します |
| オーディオインターフェース経由 | オーディオインターフェース | 機器側でゲインを取るので、アプリ側は控えめ |

ダイナミックマイク（カラオケでよく使う有線マイク）を PC のマイク端子に直挿しすると、
どうしても音が小さくノイズが乗ります。マイク用の入力があるオーディオインターフェースを
挟むのがいちばん確実です。

## 音が出ない・入らないとき

1. **設定・診断 → すべて調べる** を実行する
2. `△ 開けるが音が来ない` と出たら、アプリではなく機器側の問題です
   - 機器の入力切替（LINE / MIC）と録音レベルのつまみ
   - マイク本体のスイッチ、ケーブルの挿し込み
   - USB は別のポートに挿し直す（ハブ経由なら直挿し）
3. `Windows でミュートされています` と出たらそこを解除する
4. Host API を `Windows WASAPI` から `MME` に変えると動く機器もあります

## 遅延を減らすには

「設定・診断」タブのバッファを小さくします（既定 25 ms）。
音が途切れるようなら少しずつ戻してください。ノイズ除去を切ると 10 ms ほど短くなります。

## コマンドラインから使う

```powershell
.\.venv\Scripts\python.exe cli.py --list                        # デバイス一覧
.\.venv\Scripts\python.exe cli.py --check                       # 全部診断
.\.venv\Scripts\python.exe cli.py --mic Logicool --out JBL      # 流す
.\.venv\Scripts\python.exe cli.py --mic SE-U33GX --out JBL --preset karaoke --gain 30
```

## VST3

手持ちの VST3 をマイクの後段に挿せます（設定・診断タブ）。
`C:\Program Files\Common Files\VST3` などを自動で探します。
プラグインによっては認証が必要で、未認証だとノイズが混ざることがあります。

## 仕組み

| ファイル | 役割 |
|---|---|
| `app.py` | 画面 |
| `router.py` | マイク → 出力の経路。入力と出力を別ストリームで開き、レートが違えば変換し、クロック差はバッファ量を見て吸収する |
| `mic_chain.py` | 音作り。スペクトル減算のノイズ除去（numpy）＋ pedalboard のゲート/コンプ/エコー/リミッタ/VST3 |
| `player.py` | 動画再生。PyAV でデコードし、音声クロックに映像を合わせる。音は選んだデバイスへ出す |
| `music_search.py` | yt-dlp で検索し、タイトルからオフボーカルらしさを採点する |
| `devices.py` | デバイスの一覧と診断。実際に開いてデータが来るかまで確認する |
| `platform_support.py` | OS ごとの違い（設定の保存先、Host API の既定、プラグイン探索先、フォントとテーマ）を集約 |
| `comutil.py` | WASAPI はスレッドごとに COM 初期化が必要。忘れると原因の分かりにくいエラーになる。他 OS では何もしない |

## 対応 OS

Windows / macOS / Linux で動く作りにしています。OS ごとの違いは次のとおりです。

| | Windows | macOS | Linux |
|---|---|---|---|
| Host API の既定 | WASAPI | Core Audio | ALSA |
| 設定の保存先 | `%APPDATA%` | `~/Library/Application Support` | `~/.config` |
| 作成した音源 | `%LOCALAPPDATA%` | `~/Library/Caches` | `~/.cache` |
| プラグイン | VST3 | VST3 + Audio Unit | VST3 |
| OS 側のミュート検出 | 対応 | 非対応（診断の他項目は動作） | 非対応（同左） |
| ボーカル除去 | CUDA | Apple Silicon (MPS) | CUDA |

開発は Windows 11 + RTX 3090 で行い、macOS と Linux では OS 分岐のロジックのみ検証しています
（実機での動作確認は未実施）。

## 配布物の作り方

`v1.0.0` のようなタグを push すると、GitHub Actions が Windows / macOS (Intel・Apple Silicon) /
Linux 向けをまとめて作り、リリースに並べます。手元で作るなら次のとおりです。

```
pip install pyinstaller
pyinstaller --noconfirm --clean packaging/KaraokeStudio.spec
```

`dist/KaraokeStudio/` に一式ができます。Windows のインストーラは Inno Setup で
`packaging/installer.iss` をコンパイルすると作れます。

配布物には torch / demucs を含めていません（3 GB を超え、GitHub のリリースは 1 ファイル 2 GB まで）。
ボーカル除去を使う場合はソースから導入してください。

## プラグイン画面の仕組み

pedalboard の `show_editor()` はメインスレッドからしか呼べず、閉じるまで戻ってきません
（[spotify/pedalboard#386](https://github.com/spotify/pedalboard/issues/386)）。
そのまま呼ぶとアプリ全体が停止するため、`vst_editor_host.py` を別プロセスとして起動し、
標準入出力の JSON でつまみの値をやりとりしています。同じ理由で報告されている
「ウィンドウを動かせない」「複数開くと左上で重なる」も、プロセス分離と
ウィンドウスタイルの付与で解消しています。

音を処理しているのは本体側のインスタンスなので、エディタ内のメーター表示は動きません。
これを直すにはプラグインごとに音声処理を別プロセスへ移す（共有メモリでやりとりする）
必要があり、遅延と複雑さが増すため採用していません。

再生する動画の扱いは、各サービスの利用規約と著作権法に従ってください。
