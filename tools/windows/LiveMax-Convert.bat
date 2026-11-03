<# :
@echo off
setlocal
title LiveMax Media Convert
set "LM_SELF=%~f0"
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "iex ([IO.File]::ReadAllText($env:LM_SELF, [Text.Encoding]::UTF8))"
echo.
pause
exit /b
#>
# ============================================================================
#  LiveMax 미디어 변환기 (PC 전용)
#
#  사용법: 영상·음악 파일(또는 폴더)을 이 파일 아이콘 위로 끌어다 놓는다.
#    - 영상 → 라이브 송출 규격 MP4 (H.264 High 1080p 30fps 5500k CBR, 키프레임 2초, B프레임 없음, 무음)
#    - 음악 → 라이브 음원 규격 M4A (AAC-LC 128k 44.1kHz 스테레오, -14 LUFS 2-pass 정규화)
#    - 결과는 원본과 같은 폴더 안의 "LiveMax_변환" 폴더에, 원본과 같은 파일명으로 생긴다.
#    - 파일이 많으면(100개 초과) 폴더째 끌어다 놓는다. (윈도우가 한 번에 넘길 수 있는 경로 길이 제한)
#
#  서버는 재인코딩하지 않는다(DESIGN 11장). 그래서 규격 맞추기는 전부 여기 PC 에서 한다.
#  이 파일 상단(<# ... #>)은 배치 실행부, 아래는 PowerShell 코드다. 한 파일로 동작한다.
# ============================================================================

# ---------------------------------------------------------------- 설정 (필요하면 여기만 고친다)
$VideoBitrate  = "5500k"     # 라이브별 비트레이트 실험(4500k 등) 때 바꾼다
$VideoPreset   = "medium"    # 느릴수록 화질↑: veryfast / faster / medium / slow
$SplitSeconds  = 0           # 0 = 통째로 변환(파일명 그대로). 60 = 1분 클립으로 잘라 이름_001.mp4 ...
$AudioBitrate  = "128k"
$TargetLufs    = -14
$TruePeak      = -1
$OutDirName    = "LiveMax_변환"
$WarnFileCount = 100         # 파일을 이보다 많이 끌어다 놓으면 폴더째 넣으라고 경고
$MaxCmdLine    = 7900        # 윈도우 명령줄 한계(8191자) 근처 — 넘으면 목록이 잘렸을 수 있어 중단

$VideoExt = @(".mp4", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".m4v", ".mpg", ".mpeg", ".ts", ".mts", ".m2ts", ".flv")
$AudioExt = @(".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".aif", ".aiff", ".alac")

$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

function Say($m)  { Write-Host $m }
function Ok($m)   { Write-Host $m -ForegroundColor Green }
function Warn($m) { Write-Host $m -ForegroundColor Yellow }
function Bad($m)  { Write-Host $m -ForegroundColor Red }

# ---------------------------------------------------------------- ffmpeg 찾기
function Find-Tool($name) {
    $c = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return $c.Path }
    $cands = @(
        "C:\ffmpeg\bin\$name.exe",
        (Join-Path $env:ProgramFiles "ffmpeg\bin\$name.exe"),
        (Join-Path $env:USERPROFILE "ffmpeg\bin\$name.exe"),
        (Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Links\$name.exe")
    )
    foreach ($p in $cands) { if ($p -and (Test-Path -LiteralPath $p)) { return $p } }
    $pk = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages"
    if ($env:LOCALAPPDATA -and (Test-Path -LiteralPath $pk)) {
        $f = Get-ChildItem -LiteralPath $pk -Recurse -Filter "$name.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($f) { return $f.FullName }
    }
    return $null
}

# ---------------------------------------------------------------- 끌어다 놓은 경로 읽기
# 경로를 배치(cmd)의 %* 로 넘기면 & % 같은 문자가 든 파일명이 깨진다.
# 그래서 cmd 를 거치지 않고, 이 PowerShell 을 띄운 cmd 프로세스의 원래 명령줄을 직접 읽는다.
$script:CmdLineLength = 0
function Get-DroppedPaths {
    if ($env:LM_TEST_ARGS) { return @($env:LM_TEST_ARGS -split "\|" | Where-Object { $_ }) }
    try {
        $me = Get-CimInstance Win32_Process -Filter "ProcessId=$PID" -ErrorAction Stop
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($me.ParentProcessId)" -ErrorAction Stop
        $cl = [string]$parent.CommandLine
    } catch { return @() }
    $script:CmdLineLength = $cl.Length
    $self = $env:LM_SELF
    $i = $cl.IndexOf($self, [System.StringComparison]::OrdinalIgnoreCase)
    if ($i -lt 0) { return @() }
    $rest = $cl.Substring($i + $self.Length)
    if ($rest.StartsWith('"')) { $rest = $rest.Substring(1) }
    $out = @()
    foreach ($m in [regex]::Matches($rest, '"[^"]*"|\S+')) {
        $t = $m.Value.Trim('"')
        if ($t -and (Test-Path -LiteralPath $t)) { $out += (Get-Item -LiteralPath $t).FullName }
    }
    return $out
}

# ---------------------------------------------------------------- 종류 판별 (실제 스트림 기준)
function Get-Kind($path) {
    $raw = & $script:FFPROBE -v error -show_entries "stream=codec_type:stream_disposition=attached_pic" -of json $path 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    try { $o = ($raw | Out-String) | ConvertFrom-Json } catch { return $null }
    $v = @($o.streams | Where-Object { $_.codec_type -eq "video" -and -not ($_.disposition -and $_.disposition.attached_pic -eq 1) })
    $a = @($o.streams | Where-Object { $_.codec_type -eq "audio" })
    if ($v.Count -gt 0) { return "video" }
    if ($a.Count -gt 0) { return "audio" }
    return $null
}

function Test-UpToDate($src, $dst) {
    if (-not (Test-Path -LiteralPath $dst)) { return $false }
    return ((Get-Item -LiteralPath $dst).LastWriteTime -ge (Get-Item -LiteralPath $src).LastWriteTime)
}

# ---------------------------------------------------------------- 영상 변환
function Convert-Video($src, $outDir) {
    $base = [IO.Path]::GetFileNameWithoutExtension($src)
    # 16:9 가 아니면 비율을 유지한 채 검은 여백을 붙인다. 프레임은 30fps 고정, 8bit 4:2:0.
    $vf = "scale=1920:1080:force_original_aspect_ratio=decrease:flags=lanczos,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30,format=yuv420p"
    $enc = @(
        "-map", "0:v:0", "-an", "-sn", "-dn", "-map_metadata", "-1",
        "-vf", $vf,
        "-c:v", "libx264", "-profile:v", "high", "-preset", $VideoPreset,
        "-b:v", $VideoBitrate, "-minrate", $VideoBitrate, "-maxrate", $VideoBitrate, "-bufsize", $VideoBitrate,
        "-x264-params", "nal-hrd=cbr:force-cfr=1",
        "-g", "60", "-keyint_min", "60", "-sc_threshold", "0", "-bf", "0"
    )
    if ($SplitSeconds -gt 0) {
        $first = Join-Path $outDir ("{0}_001.mp4" -f $base)
        if (Test-UpToDate $src $first) { return "skip" }
        $pattern = Join-Path $outDir ($base.Replace("%", "%%") + "_%03d.mp4")
        & $script:FFMPEG -hide_banner -nostdin -y -loglevel error -stats -i $src @enc `
            -f segment -segment_time $SplitSeconds -reset_timestamps 1 -segment_start_number 1 `
            -segment_format mp4 -segment_format_options "movflags=+faststart" $pattern
        if ($LASTEXITCODE -ne 0) { return "fail" }
        return "ok"
    }
    $dst = Join-Path $outDir "$base.mp4"
    if (Test-UpToDate $src $dst) { return "skip" }
    $tmp = Join-Path $outDir "$base.part.mp4"
    & $script:FFMPEG -hide_banner -nostdin -y -loglevel error -stats -i $src @enc -movflags "+faststart" $tmp
    if ($LASTEXITCODE -ne 0) { Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue; return "fail" }
    Move-Item -LiteralPath $tmp -Destination $dst -Force
    return "ok"
}

# ---------------------------------------------------------------- 음원 변환 (-14 LUFS 2-pass)
function Convert-Audio($src, $outDir) {
    $base = [IO.Path]::GetFileNameWithoutExtension($src)
    $dst = Join-Path $outDir "$base.m4a"
    if (Test-UpToDate $src $dst) { return "skip" }
    $tmp = Join-Path $outDir "$base.part.m4a"

    # 1차: 라우드니스 측정
    $ln = "loudnorm=I=${TargetLufs}:TP=${TruePeak}:LRA=20"
    $log = (& $script:FFMPEG -hide_banner -nostdin -i $src -map 0:a:0 -af "${ln}:print_format=json" -f null - 2>&1 |
            ForEach-Object { "$_" }) -join "`n"
    $af = $null
    $json = [regex]::Match($log, '\{[^{}]*"input_i"[^{}]*\}').Value
    if ($json) {
        try {
            $m = $json | ConvertFrom-Json
            if ("$($m.input_i)" -notmatch "inf") {
                $af = "${ln}:measured_I=$($m.input_i):measured_TP=$($m.input_tp):measured_LRA=$($m.input_lra):measured_thresh=$($m.input_thresh):offset=$($m.target_offset):linear=true"
            }
        } catch { }
    }
    if (-not $af) { Warn "    (라우드니스 측정 불가 — 완전 무음이거나 너무 짧음. 정규화 없이 변환. 완전 무음 파일은 비트레이트가 너무 낮아 서버 검증에서 거부될 수 있음)" }

    # 2차: 정규화 + AAC-LC 128k 44.1kHz 스테레오. 곡명·아티스트 태그는 유지 (대시보드 '지금 곡' 표시용)
    $a = @("-hide_banner", "-nostdin", "-y", "-loglevel", "error", "-i", $src, "-map", "0:a:0", "-vn", "-sn", "-map_metadata", "0")
    if ($af) { $a += @("-af", $af) }
    $a += @("-ar", "44100", "-ac", "2", "-c:a", "aac", "-profile:a", "aac_low", "-b:a", $AudioBitrate, "-movflags", "+faststart", $tmp)
    & $script:FFMPEG @a
    if ($LASTEXITCODE -ne 0) { Remove-Item -LiteralPath $tmp -ErrorAction SilentlyContinue; return "fail" }
    Move-Item -LiteralPath $tmp -Destination $dst -Force
    return "ok"
}

# ================================================================ 실행
Write-Host ""
Write-Host "=============== LiveMax 미디어 변환 ===============" -ForegroundColor Cyan

$script:FFMPEG = Find-Tool "ffmpeg"
$script:FFPROBE = Find-Tool "ffprobe"
if (-not $script:FFMPEG -or -not $script:FFPROBE) {
    Bad "ffmpeg / ffprobe 를 찾을 수 없습니다."
    Say "  설치: PowerShell 에서  winget install Gyan.FFmpeg  실행 후, 창을 모두 닫고 다시 시도하세요."
    return
}

$items = @(Get-DroppedPaths)
if ($items.Count -eq 0) {
    Say "변환할 영상·음악 파일(또는 폴더)을 이 파일 아이콘 위로 끌어다 놓으세요."
    Say "  결과: 원본 옆 '$OutDirName' 폴더에 같은 파일명으로 생깁니다."
    return
}
if ($script:CmdLineLength -ge $MaxCmdLine) {
    Bad "끌어다 놓은 파일이 너무 많아 윈도우가 목록을 잘랐을 수 있습니다. 변환을 시작하지 않습니다."
    Warn "→ 파일들을 폴더 하나에 모아서, 그 '폴더'를 끌어다 놓으세요."
    return
}
$droppedFiles = @($items | Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }).Count
if ($droppedFiles -gt $WarnFileCount) {
    Warn "파일을 $droppedFiles 개나 한꺼번에 끌어다 놓았습니다."
    Warn "윈도우는 한 번에 넘길 수 있는 경로 길이에 한계가 있어 일부가 빠질 수 있습니다."
    Warn "→ 다음부터는 파일들을 폴더에 모아 '폴더째' 끌어다 놓으세요. (이번 목록은 그대로 진행합니다)"
    Write-Host ""
}

# 대상 파일 모으기 (폴더는 하위 폴더까지, 결과 폴더는 제외)
$all = $VideoExt + $AudioExt
$files = New-Object System.Collections.Generic.List[string]
foreach ($it in $items) {
    if (Test-Path -LiteralPath $it -PathType Container) {
        Get-ChildItem -LiteralPath $it -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { ($all -contains $_.Extension.ToLower()) -and (($_.DirectoryName -split "[\\/]") -notcontains $OutDirName) -and ($_.Name -notmatch "\.part\.") } |
            Sort-Object FullName | ForEach-Object { $files.Add($_.FullName) }
    } elseif ($all -contains ([IO.Path]::GetExtension($it).ToLower())) {
        $files.Add($it)
    } else {
        Warn "건너뜀 (영상·음악 확장자가 아님): $([IO.Path]::GetFileName($it))"
    }
}
if ($files.Count -eq 0) { Warn "변환할 영상·음악 파일이 없습니다."; return }

Say "영상: H.264 High 1920x1080 30fps $VideoBitrate CBR, 키프레임 2초, B프레임 없음, 무음$(if ($SplitSeconds -gt 0) { ", ${SplitSeconds}초 클립 분할" })"
Say "음원: AAC-LC $AudioBitrate 44.1kHz 스테레오, $TargetLufs LUFS"
Say "대상: $($files.Count)개 파일"
Write-Host ""

$n = 0; $ok = 0; $skip = 0; $failed = @(); $outDirs = @{}
$t0 = Get-Date
foreach ($f in $files) {
    $n++
    $name = [IO.Path]::GetFileName($f)
    $kind = Get-Kind $f
    if (-not $kind) { Bad ("[{0}/{1}] {2} — 읽을 수 없는 파일" -f $n, $files.Count, $name); $failed += $f; continue }
    $outDir = Join-Path ([IO.Path]::GetDirectoryName($f)) $OutDirName
    if (-not (Test-Path -LiteralPath $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }
    $outDirs[$outDir] = $true
    $label = "음원"
    if ($kind -eq "video") { $label = "영상" }
    Say ("[{0}/{1}] {2}  {3}" -f $n, $files.Count, $label, $name)
    $s = Get-Date
    if ($kind -eq "video") { $r = Convert-Video $f $outDir } else { $r = Convert-Audio $f $outDir }
    $sec = [int]((Get-Date) - $s).TotalSeconds
    switch ($r) {
        "ok"   { $ok++; Ok "    완료 ($sec 초)" }
        "skip" { $skip++; Say "    이미 변환됨 — 건너뜀" }
        default { $failed += $f; Bad "    실패 (위 ffmpeg 메시지 참고)" }
    }
}

$total = [int]((Get-Date) - $t0).TotalMinutes
Write-Host ""
Write-Host "=================== 결과 ===================" -ForegroundColor Cyan
Ok  "완료 $ok 개 / 건너뜀 $skip 개 / 실패 $($failed.Count) 개  (총 $total 분)"
if ($failed.Count) {
    Bad "실패한 파일:"
    $failed | ForEach-Object { Bad "  $_" }
}
Say "결과 폴더:"
$outDirs.Keys | Sort-Object | ForEach-Object { Say "  $_" }
Say ""
Say "다음: 결과 폴더의 파일을 서버의 /media/_incoming/video/<폴더>/ 또는 /media/_incoming/audio/<폴더>/ 로 올리세요."
