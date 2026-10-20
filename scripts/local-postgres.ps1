param(
    [ValidateSet('start', 'stop', 'status')]
    [string]$Action = 'start',
    [int]$Port = 55432
)
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$hasher = [Security.Cryptography.SHA256]::Create()
try {
    $rootHash = [BitConverter]::ToString(
        $hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($projectRoot))
    ).Replace('-', '').Substring(0, 12).ToLowerInvariant()
} finally { $hasher.Dispose() }
# EDB Windows binaries cannot reliably start from non-ASCII paths. Keep the private runtime in TEMP.
$runtimeRoot = [IO.Path]::GetFullPath((Join-Path $env:TEMP "teaching-pg-$rootHash"))
$binaryRoot = Join-Path $runtimeRoot 'pgsql\bin'
$dataRoot = Join-Path $runtimeRoot 'data'
$pgctl = Join-Path $binaryRoot 'pg_ctl.exe'

if ($Action -eq 'stop') {
    if (Test-Path -LiteralPath $pgctl) { & $pgctl -D $dataRoot -m fast -w stop }
    exit $LASTEXITCODE
}
if ($Action -eq 'status') {
    if (Test-Path -LiteralPath $pgctl) { & $pgctl -D $dataRoot status }
    exit $LASTEXITCODE
}
if (-not (Test-Path -LiteralPath $pgctl)) {
    $zipPath = Join-Path $projectRoot '.local\postgresql-windows.zip'
    if (-not (Test-Path -LiteralPath $zipPath)) {
        throw 'Place the official EDB PostgreSQL 17 Windows binaries ZIP at .local\postgresql-windows.zip, or use Docker Compose.'
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        foreach ($entry in $archive.Entries) {
            if ($entry.FullName -notmatch '^pgsql/(bin|lib|share)/' -or $entry.FullName.EndsWith('/')) { continue }
            $targetPath = [IO.Path]::GetFullPath((Join-Path $runtimeRoot $entry.FullName))
            if (-not $targetPath.StartsWith($runtimeRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'Archive entry escapes runtime directory'
            }
            [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($targetPath)) | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $targetPath, $false)
        }
    } finally { $archive.Dispose() }
}
if (-not (Test-Path -LiteralPath (Join-Path $dataRoot 'PG_VERSION'))) {
    $passwordPath = Join-Path $runtimeRoot 'init-password.txt'
    [IO.File]::WriteAllText($passwordPath, 'local-development-only', [Text.UTF8Encoding]::new($false))
    & (Join-Path $binaryRoot 'initdb.exe') -D $dataRoot -U teaching --pwfile=$passwordPath --auth=scram-sha-256 --encoding=UTF8 --locale=C
    if ($LASTEXITCODE -ne 0) { throw 'initdb failed' }
    # This exact file is created above and stays inside the checked runtime root.
    Remove-Item -LiteralPath $passwordPath
}
& $pgctl -D $dataRoot status *> $null
if ($LASTEXITCODE -ne 0) {
    $logPath = Join-Path $runtimeRoot 'postgres.log'
    $arguments = @('-D', "`"$dataRoot`"", '-l', "`"$logPath`"", '-o', "`"-h 127.0.0.1 -p $Port`"", '-w', 'start')
    $process = Start-Process -FilePath $pgctl -ArgumentList $arguments -WindowStyle Hidden -PassThru
    # Wait for pg_ctl itself, not its long-lived PostgreSQL child process.
    if (-not $process.WaitForExit(60000)) { throw 'PostgreSQL startup timed out' }
    if ($process.ExitCode -ne 0) { throw "PostgreSQL failed to start; see $logPath" }
}
$previousPassword = $env:PGPASSWORD
try {
    $env:PGPASSWORD = 'local-development-only'
    foreach ($databaseName in @('teaching', 'teaching_test')) {
        $exists = & (Join-Path $binaryRoot 'psql.exe') -h 127.0.0.1 -p $Port -U teaching -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = '$databaseName'"
        if ($LASTEXITCODE -ne 0) { throw 'Could not connect to local PostgreSQL' }
        if ($exists -ne '1') {
            & (Join-Path $binaryRoot 'createdb.exe') -h 127.0.0.1 -p $Port -U teaching $databaseName
            if ($LASTEXITCODE -ne 0) { throw "Could not create $databaseName" }
        }
    }
} finally { $env:PGPASSWORD = $previousPassword }
Write-Output "Local PostgreSQL ready on 127.0.0.1:$Port; data: $dataRoot"
