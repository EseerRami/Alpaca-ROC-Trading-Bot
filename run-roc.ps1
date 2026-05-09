# =====================================================================
# ROC-Bot launcher (PowerShell)
#
# Usage:
#   .\run-roc.ps1
#
# This bot ranks stocks in AUTH/Tickers.txt by 1-min ROC, buys the top
# one if ASK > LTP using 100% of available capital, sells at 2% gain.
# Then repeats. (HFT-style — runs continuously during market hours.)
#
# WARNING: 100% capital allocation per trade, no stop-loss. Paper only.
#
# Assumes you've already filled in AUTH/auth.txt with paper keys.
# =====================================================================

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Test-Path "AUTH\auth.txt")) {
    Write-Error "AUTH\auth.txt not found. Copy AUTH\auth.txt.template -> AUTH\auth.txt and fill in your paper keys."
    exit 1
}

# First-run venv setup
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Output "[setup] Creating virtualenv..."
    python -m venv .venv
    .\.venv\Scripts\python.exe -m pip install --upgrade pip
    # Original requirements.txt pins old versions. Use newer compatible ones.
    .\.venv\Scripts\python.exe -m pip install "alpaca-trade-api>=3.0" "pandas>=2.0" "numpy>=1.24" "pytz>=2023.3"
}

# Make sure tick_data/ and trade_log dirs exist
New-Item -ItemType Directory -Force -Path "tick_data" | Out-Null

Write-Output "[ROC] Starting ROC trading loop. Ctrl+C to stop."
Write-Output "[ROC] Strategy: rank by 1-min ROC, buy top with ASK>LTP at 100% capital, sell at 2% gain."
Write-Output "[ROC] Tickers:"
Get-Content "AUTH\Tickers.txt"
Write-Output "---"

.\.venv\Scripts\python.exe main.py
