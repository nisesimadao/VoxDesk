# ソースから配布物を作る（Windows）。
#
#   git clone https://github.com/nisesimadao/VoxDesk.git
#   cd VoxDesk
#   .\build.ps1
#
# 出来上がるもの:
#   dist\VoxDesk\ （そのまま動く一式）
#   dist\installer\VoxDesk-<版>-windows-x64-setup.exe （Inno Setup があるとき）

param([string]$Version = "dev")

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

function Say($text) { Write-Host "`n== $text" -ForegroundColor Cyan }

Say "確認"
$python = $null
foreach ($candidate in @("py -3.12", "py -3", "python")) {
    $parts = $candidate.Split(" ")
    $exe = (Get-Command $parts[0] -ErrorAction SilentlyContinue)
    if ($exe) { $python = $candidate; break }
}
if (-not $python) { throw "Python が見つかりません。python.org から 3.10 以上を入れてください。" }
Write-Host "  $python -> $(Invoke-Expression "$python -V")"

Say "仮想環境を用意"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Invoke-Expression "$python -m venv .venv"
}
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip -q
& ".\.venv\Scripts\python.exe" -m pip install -q -r requirements.txt
& ".\.venv\Scripts\python.exe" -m pip install -q "pyinstaller==6.11.1"

Say "アイコンを作る"
try { & ".\.venv\Scripts\python.exe" packaging\make_icon.py } catch { Write-Host "  生成に失敗（既定のもので続行）" }

Say "実行ファイルを作る"
& ".\.venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean packaging\VoxDesk.spec
if (-not (Test-Path "dist\VoxDesk\VoxDesk.exe")) { throw "ビルドに失敗しました。" }

Say "インストーラを作る"
$iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if (Test-Path $iscc) {
    & $iscc "/DAppVersion=$Version" "packaging\installer.iss"
    Get-ChildItem "dist\installer" | Format-Table Name, @{n='MB';e={[math]::Round($_.Length/1MB,1)}} -AutoSize
} else {
    Write-Host "  Inno Setup が無いので飛ばします（インストーラが要るなら jrsoftware.org から入れてください）"
    Write-Host "  dist\VoxDesk\VoxDesk.exe はそのまま動きます"
}

Say "完成"
Write-Host "そのまま動かすなら: .\VoxDesk を起動.bat"
