# Native Windows entry point. Pass all arguments directly to the controller.
$ErrorActionPreference = 'Stop'
function Find-OwnerPython {
    if ($env:RSI_PYTHON) {
        if ($env:RSI_PYTHON -notmatch '^(?:[A-Za-z]:[\\/]|\\\\)') {
            throw 'RSI_PYTHON must be the full path to python.exe.'
        }
        $candidates = @($env:RSI_PYTHON)
    } else {
        # Official CPython installations register their executable with PEP 514.
        $candidates = @(
            foreach ($hive in 'HKCU:', 'HKLM:') {
                $key = "$hive\Software\Python\PythonCore"
                foreach ($version in (Get-ChildItem -LiteralPath $key -ErrorAction SilentlyContinue | Sort-Object PSChildName -Descending)) {
                    $install = Get-Item -LiteralPath ($version.PSPath + '\InstallPath') -ErrorAction SilentlyContinue
                    if ($install) {
                        $exe = $install.GetValue('ExecutablePath')
                        if ($exe) { $exe } elseif ($install.GetValue('')) { Join-Path $install.GetValue('') 'python.exe' }
                    }
                }
            }
        )
    }
    foreach ($candidate in $candidates) {
        if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        $file = Get-Item -LiteralPath $candidate
        $origins = @($PSScriptRoot)
        if ($env:RSI_ROOT) { $origins += $env:RSI_ROOT }
        foreach ($origin in $origins) {
            $boundary = [IO.Path]::GetFullPath($origin).TrimEnd('\') + '\'
            if ($file.FullName.StartsWith($boundary, [StringComparison]::OrdinalIgnoreCase)) {
                throw "The controller Python must be outside $origin."
            }
        }
        # Check the executable and its ancestors before starting anything from that path.
        for ($part = $file; $part; $part = $(if ($part -is [IO.FileInfo]) { $part.Directory } else { $part.Parent })) {
            if ($part.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Use the physical path to Python. This path contains a reparse point: $($part.FullName)"
            }
        }
        & $file.FullName -I -B -c 'import sys; sys.exit(sys.version_info < (3, 11))'
        if ($LASTEXITCODE -eq 0) { return $file.FullName }
    }
    throw 'Install Python 3.11 or newer, or set RSI_PYTHON to its full executable path.'
}

try {
    $interpreter = Find-OwnerPython
    & $interpreter -I -B (Join-Path $PSScriptRoot 'control\rsi\__main__.py') @args
    exit $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine("rsi: " + $_.Exception.Message)
    exit 2
}
