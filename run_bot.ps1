# Rula_Chan — 会話とリンク検査だけのBotを起動します。
#
# 音楽機能はこのビルドに入っていません(music.py がありません)。音楽が必要なら
# 隣の Rula_Music を使ってください。
#
# API サーバーとは別プロセスです。設定は1つ上の .env を共有します。
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot
$root = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $root ".env"

if (-not (Test-Path $envFile)) {
    Write-Host "$envFile がありません。.env.example をコピーしてください。" -ForegroundColor Yellow
    exit 1
}

# Either source counts. discord_bot.py reads the environment first and only
# falls back to .env, so demanding the file here would refuse to start over a
# token that is already set as a Windows environment variable.
$tokenSet = [bool][Environment]::GetEnvironmentVariable("DISCORD_TOKEN") -or
            (Select-String -Path $envFile -Pattern "^DISCORD_TOKEN=.+" -Quiet)
if (-not $tokenSet) {
    Write-Host "DISCORD_TOKEN が未設定です(環境変数にも $envFile にもありません)。" -ForegroundColor Yellow
    exit 1
}

# A warning rather than a refusal, matching the bot itself: without a key it
# starts with the conversation disabled and says so, and link checking still
# works. Stopping here would be stricter than the thing being started.
$keySet = [bool][Environment]::GetEnvironmentVariable("RULA_API_KEY") -or
          (Select-String -Path $envFile -Pattern "^RULA_API_KEY=.+" -Quiet)
if (-not $keySet) {
    Write-Host "RULA_API_KEY が未設定です。会話機能は無効で起動します。" -ForegroundColor Yellow
    Write-Host "リンク検査は設定なしで動きます。" -ForegroundColor Gray
}

# The API's port lives in the same .env; read it rather than assuming 8000, or a
# moved API looks like a stopped one.
$portMatch = Select-String -Path $envFile -Pattern "^RULA_API_PORT=(\d+)" | Select-Object -First 1
$port = if ($env:RULA_API_PORT) { $env:RULA_API_PORT }
        elseif ($portMatch) { $portMatch.Matches[0].Groups[1].Value }
        else { "8000" }

# Only checked when the API is meant to be local. With RULA_API_BASE pointing at
# a tunnel there is nothing on 127.0.0.1 to find, and failing on that would stop
# the bot over the absence of something it was never going to use.
if (-not $env:RULA_API_BASE -and
    -not (Select-String -Path $envFile -Pattern "^RULA_API_BASE=.+" -Quiet)) {
    try {
        $health = Invoke-RestMethod "http://127.0.0.1:$port/health" -TimeoutSec 5
        Write-Host "API: $($health.weights) ($($health.backend))" -ForegroundColor Gray
    } catch {
        Write-Host "APIサーバーに接続できません (127.0.0.1:$port)。" -ForegroundColor Yellow
        Write-Host "会話機能は使えませんが、リンク検査は動きます。" -ForegroundColor Gray
    }
}

if ($env:VIRUSTOTAL_API_KEY -or
    (Select-String -Path $envFile -Pattern "^VIRUSTOTAL_API_KEY=.+" -Quiet)) {
    Write-Host "リンク検査: VirusTotal 併用" -ForegroundColor Gray
} else {
    Write-Host "リンク検査: オフライン判定のみ (VIRUSTOTAL_API_KEY 未設定)" -ForegroundColor Gray
}

Write-Host "Discord に接続します (Ctrl+C で停止)" -ForegroundColor Cyan
python main.py
