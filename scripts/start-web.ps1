$ErrorActionPreference = "Stop"

# 从脚本位置计算项目根目录，因此 PowerShell 当前目录不影响 web 路径。
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
  throw "node 18+ is required"
}
if (Test-Path (Join-Path $Root ".venv\Scripts\python.exe")) {
  $env:SKILLSBENCH_PYTHON = Join-Path $Root ".venv\Scripts\python.exe"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
  $env:SKILLSBENCH_PYTHON = (Get-Command py).Source
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
  $env:SKILLSBENCH_PYTHON = (Get-Command python).Source
} else {
  throw "Python 3.12+ is required for trajectory analysis and repair"
}

node runner-app/server.mjs
