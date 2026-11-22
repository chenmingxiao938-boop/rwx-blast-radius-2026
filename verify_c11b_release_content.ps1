param(
    [Parameter(Mandatory)] [string] $Repo,
    [Parameter(Mandatory)] [string] $Archive,
    [Parameter(Mandatory)] [string] $Export,
    [switch] $RequireRawIndexArchive
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Read-CommandBytes([string] $Program, [string[]] $CommandArgs) {
    $info = [Diagnostics.ProcessStartInfo]::new($Program)
    $info.UseShellExecute = $false
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    foreach ($arg in $CommandArgs) { $info.ArgumentList.Add($arg) }
    $process = [Diagnostics.Process]::Start($info)
    $stream = [IO.MemoryStream]::new()
    try {
        $stderr = $process.StandardError.ReadToEndAsync()
        $process.StandardOutput.BaseStream.CopyTo($stream)
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) { throw $stderr.GetAwaiter().GetResult() }
        return ,$stream.ToArray()
    } finally {
        $stream.Dispose()
        $process.Dispose()
    }
}

function Test-ExactBytes([byte[]] $Left, [byte[]] $Right) {
    return [Convert]::ToBase64String($Left) -ceq [Convert]::ToBase64String($Right)
}

function ConvertTo-CanonicalLf([byte[]] $Bytes) {
    # Latin-1 maps every byte to exactly one code point and back. This keeps
    # BOMs, non-UTF-8 bytes, and bare CR bytes unchanged.
    $octets = [Text.Encoding]::Latin1.GetString($Bytes)
    return ,([Text.Encoding]::Latin1.GetBytes($octets.Replace("`r`n", "`n")))
}

function Get-ByteHash([byte[]] $Bytes) {
    return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($Bytes)).ToLowerInvariant()
}

function Test-CanonicalTextPath([string] $Path) {
    return $Path -cmatch '\.(py|ps1|md|json|ya?ml|txt|toml|lock)$' -or
           $Path -cin @('.dockerignore', '.gitignore', 'Dockerfile', 'Dockerfile.coverage', 'Makefile')
}

# Check the three negative cases and a bare CR before reading release files.
$plain = [byte[]]@(0xC3, 0xA9, 0x0A)
$crlf = [byte[]]@(0xC3, 0xA9, 0x0D, 0x0A)
if (-not (Test-ExactBytes $plain (ConvertTo-CanonicalLf $crlf))) { throw 'CRLF contract failed' }
foreach ($rejected in @(
    ,([byte[]]@(0xEF, 0xBB, 0xBF, 0xC3, 0xA9, 0x0A))
    ,([byte[]]@(0xC3, 0xA9))
    ,([byte[]]@(0xE9, 0x0A))
    ,([byte[]]@(0xC3, 0xA9, 0x0D))
)) {
    if (Test-ExactBytes $plain (ConvertTo-CanonicalLf $rejected)) {
        throw 'canonical comparison accepted a non-CRLF change'
    }
}

$repoRoot = (Resolve-Path -LiteralPath $Repo).ProviderPath
$exportRoot = (Resolve-Path -LiteralPath $Export).ProviderPath
$archivePath = (Resolve-Path -LiteralPath $Archive).ProviderPath
$git = (Get-Command git.exe).Source
$gitBytes = Read-CommandBytes $git @('-C', $repoRoot, 'ls-files', '--stage', '-z')
$utf8 = [Text.UTF8Encoding]::new($false, $true)
$index = [Collections.Generic.Dictionary[string,string]]::new([StringComparer]::Ordinal)
foreach ($row in $utf8.GetString($gitBytes).Split([char]0, [StringSplitOptions]::RemoveEmptyEntries)) {
    if ($row -cnotmatch '^100(?:644|755) ([0-9a-f]{40,64}) 0\t(.+)$') { throw "invalid index entry: $row" }
    $oid = $Matches[1]
    $path = $Matches[2]
    if ($path -match '(^/|[\\:]|(^|/)\.{1,2}(/|$))') { throw "unsafe release path: $path" }
    $index.Add($path, $oid)
}

$archived = [Collections.Generic.Dictionary[string,byte[]]]::new([StringComparer]::Ordinal)
$file = [IO.File]::OpenRead($archivePath)
$tar = [System.Formats.Tar.TarReader]::new($file)
try {
    while ($null -ne ($member = $tar.GetNextEntry())) {
        if ($member.EntryType -in @('Directory', 'GlobalExtendedAttributes')) { continue }
        if ($member.EntryType -notin @('RegularFile', 'V7RegularFile') -or -not $index.ContainsKey($member.Name)) {
            throw "untracked or non-regular archive member: $($member.Name)"
        }
        $stream = [IO.MemoryStream]::new()
        try {
            if ($null -ne $member.DataStream) { $member.DataStream.CopyTo($stream) }
            $archived.Add($member.Name, $stream.ToArray())
        } finally {
            $stream.Dispose()
        }
    }
} finally {
    $tar.Dispose()
    $file.Dispose()
}
if ($archived.Count -ne $index.Count) { throw 'archive file count differs from Git index' }

$exported = @(Get-ChildItem -LiteralPath $exportRoot -File -Recurse -Force)
if ($exported.Count -ne $index.Count) { throw 'extracted build context file count differs from Git index' }
foreach ($entry in $exported) {
    $path = [IO.Path]::GetRelativePath($exportRoot, $entry.FullName).Replace('\', '/')
    if (-not $index.ContainsKey($path)) { throw "untracked file in build context: $path" }
}

$report = foreach ($path in ($index.Keys | Sort-Object)) {
    $work = [IO.File]::ReadAllBytes((Join-Path $repoRoot $path))
    $blob = Read-CommandBytes $git @('-C', $repoRoot, 'cat-file', 'blob', $index[$path])
    $memberBytes = $archived[$path]
    $extracted = [IO.File]::ReadAllBytes((Join-Path $exportRoot $path))
    if (-not (Test-ExactBytes $memberBytes $extracted)) { throw "extraction changed bytes: $path" }
    $workEqual = Test-ExactBytes $blob $work
    $archiveEqual = Test-ExactBytes $blob $memberBytes
    if (-not ($workEqual -and $archiveEqual)) {
        if (-not (Test-CanonicalTextPath $path)) { throw "non-text release file changed: $path" }
        $canonical = ConvertTo-CanonicalLf $blob
        if (-not (Test-ExactBytes $canonical (ConvertTo-CanonicalLf $work)) -or
            -not (Test-ExactBytes $canonical (ConvertTo-CanonicalLf $memberBytes))) {
            throw "release content differs beyond CRLF/LF: $path"
        }
    }
    if ($RequireRawIndexArchive -and -not $archiveEqual) { throw "release archive is not byte-exact to index: $path" }
    [pscustomobject]@{
        path = $path
        index_blob = $index[$path]
        worktree_sha256 = Get-ByteHash $work
        index_sha256 = Get-ByteHash $blob
        archive_sha256 = Get-ByteHash $memberBytes
        worktree_equals_index = $workEqual
        archive_equals_index = $archiveEqual
    }
}
ConvertTo-Json -InputObject @($report) -Depth 3
