param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    Write-Host '첫 실행: 프로젝트 전용 Python 환경을 준비합니다.'
    & python -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 이상을 설치한 뒤 다시 실행하세요.' }
}
& $taskPython -c "import importlib.util, sys; sys.exit(0 if all(importlib.util.find_spec(name) for name in ('PIL','numpy','onnxruntime','pypdfium2')) else 1)"
if ($LASTEXITCODE -ne 0) {
    & $taskPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw '이미지 처리 라이브러리 설치에 실패했습니다.' }
}
if ($NoBrowser) {
    & $taskPython -u archive.py
    if ($LASTEXITCODE -ne 0) { throw '프로그램 실행에 실패했습니다.' }
    exit 0
}
$taskUrl = 'http://127.0.0.1:18765'
try {
    $taskHealth = Invoke-RestMethod -Uri "$taskUrl/api/health" -TimeoutSec 1
    if ($taskHealth.app -eq 'RefArchive') {
        if ($taskHealth.version -ne 2) { throw 'OLD_SERVER' }
        if ($taskHealth.storage -ne (Join-Path $PSScriptRoot 'data\library')) { throw 'OTHER_LIBRARY' }
        Start-Process $taskUrl
        exit 0
    }
} catch {
    if ($_.Exception.Message -eq 'OLD_SERVER') { throw '이전 서버가 실행 중입니다. 기존 실행 창에서 Ctrl+C로 종료한 뒤 다시 실행하세요.' }
    if ($_.Exception.Message -eq 'OTHER_LIBRARY') { throw '다른 자료함이 실행 중입니다. 해당 자료함을 종료한 뒤 다시 실행하세요.' }
}
$taskRuntime = Join-Path $PSScriptRoot 'data'
New-Item -ItemType Directory -Path $taskRuntime -Force | Out-Null
$taskProcess = Start-Process -FilePath $taskPython -ArgumentList @('-u', 'archive.py') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskRuntime 'runtime.log') -RedirectStandardError (Join-Path $taskRuntime 'runtime-error.log') -PassThru
for ($taskAttempt = 0; $taskAttempt -lt 30; $taskAttempt++) {
    if ($taskProcess.HasExited) { throw '서버가 실행되지 않았습니다. data\runtime-error.log와 실행 포트를 확인하세요.' }
    try {
        $taskHealth = Invoke-RestMethod -Uri "$taskUrl/api/health" -TimeoutSec 1
        if ($taskHealth.app -eq 'RefArchive' -and $taskHealth.version -eq 2 -and $taskHealth.storage -eq (Join-Path $PSScriptRoot 'data\library')) {
            Start-Process $taskUrl
            Write-Host 'RefArchive 실행 중. 화면의 보관 · 백업에서 프로그램을 종료할 수 있습니다.'
            exit 0
        }
    } catch { }
    Start-Sleep -Milliseconds 300
}
throw '서버 준비가 지연됩니다. data\runtime-error.log를 확인하고 잠시 후 자료함 주소를 여세요.'
