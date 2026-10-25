param([string]$PackageDir)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$area = Join-Path $root '.tools/installer-smoke'
New-Item -ItemType Directory -Force -Path $area | Out-Null
$fixture = Join-Path $area ([guid]::NewGuid().ToString('N'))
$binary = Join-Path $fixture 'steam/bin/win64/vrpathreg.exe'
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $binary) | Out-Null
# Use Windows PowerShell's .NET Framework compiler to create a self-contained test fixture.
$compilerScript = Join-Path $fixture 'compile.ps1'
@'
param($Source, $Destination)
$ErrorActionPreference = 'Stop'
Add-Type -Path $Source -ReferencedAssemblies System.Web.Extensions -OutputAssembly $Destination -OutputType ConsoleApplication
'@ | Set-Content -LiteralPath $compilerScript -Encoding utf8
& "$env:SystemRoot/System32/WindowsPowerShell/v1.0/powershell.exe" -NoProfile -File $compilerScript (Join-Path $PSScriptRoot 'installer-registry-fixture.cs') $binary
if ($LASTEXITCODE -ne 0) { throw 'Could not compile registry test fixture.' }
$iscc = Join-Path $root '.tools/InnoSetup/ISCC.exe'
if (-not (Test-Path -LiteralPath $iscc)) { $iscc = (Get-Command ISCC.exe -ErrorAction Stop).Source }
$defines = @('/DSmokeTest')
if ($PackageDir) { $defines += "/DPackageDir=$((Resolve-Path -LiteralPath $PackageDir).Path)" }
& $iscc @defines "/O$area" (Join-Path $root 'installer/Beyond-SLAM.iss') *> (Join-Path $fixture 'compile.log')
if ($LASTEXITCODE -ne 0) { throw "Smoke-test installer compilation failed; see $fixture/compile.log" }
& (Join-Path $root '.venv/Scripts/python.exe') (Join-Path $PSScriptRoot 'smoke-installer.py') (Join-Path $area 'Beyond-SLAM-Setup.exe') $fixture
if ($LASTEXITCODE -ne 0) { throw "Installer smoke test failed; see $fixture" }
Write-Host "Installer test evidence: $fixture/result.json"
