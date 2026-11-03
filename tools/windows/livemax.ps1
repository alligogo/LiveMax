# LiveMax 서버 관리 도구 (Windows PowerShell 5.1 이상)
# 사용법: 같은 폴더의 LiveMax.bat 를 더블클릭 → 메뉴에서 번호 선택
#
# PC 에 저장되는 것 (ZIP 을 다시 받아도 유지됨):
#   %USERPROFILE%\.ssh\id_ed25519        이 PC 의 SSH 키 (서버 접속용, 절대 공유 금지)
#   %USERPROFILE%\.ssh\livemax_deploy    공용 GitHub deploy key (읽기 전용, 모든 서버가 같이 씀)
#   %USERPROFILE%\livemax\servers.txt    서버 목록 (이름 IP)
#
# 주의 (PowerShell 5.1): 원격 명령 문자열 안에 큰따옴표를 쓰지 않는다. 5.1 은 외부 프로그램에
# 넘기는 인자 속 큰따옴표를 망가뜨린다. 원격 명령에는 작은따옴표만 쓴다.

param(
    [Parameter(Position = 0)][string]$Command = "menu",
    [string]$Name = "",
    [string]$Ip = ""
)

$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$RepoUrl     = "git@github.com:alligogo/LiveMax.git"
$DeployKeysPage = "https://github.com/alligogo/LiveMax/settings/keys"
$RepoRoot    = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Bootstrap   = Join-Path $RepoRoot "bootstrap.sh"
$SshDir      = Join-Path $env:USERPROFILE ".ssh"
$MyKey       = Join-Path $SshDir "id_ed25519"
$DeployKey   = Join-Path $SshDir "livemax_deploy"
$DataDir     = Join-Path $env:USERPROFILE "livemax"
$ServersFile = Join-Path $DataDir "servers.txt"
$DashRemotePort = 8080

# ---------------------------------------------------------------- 출력 도우미
function Say($msg)  { Write-Host $msg }
function Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "  [주의] $msg" -ForegroundColor Yellow }
function Fail($msg) { Write-Host "  [실패] $msg" -ForegroundColor Red }
function Pause-Enter($msg = "계속하려면 Enter") { [void](Read-Host $msg) }

# ---------------------------------------------------------------- 사전 확인
function Test-Tools {
    if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
        Fail "ssh 프로그램이 없습니다."
        Say  "  설정 → 시스템 → 선택적 기능 → 기능 추가 → 'OpenSSH 클라이언트' 설치 후 다시 실행하세요."
        return $false
    }
    return $true
}

function Test-MyKey {
    if (-not (Test-Path "$MyKey.pub")) {
        Fail "이 PC 의 SSH 키가 없습니다. 메뉴 1 (PC 준비)을 먼저 하세요."
        return $false
    }
    return $true
}

function Test-DeployKey {
    if (-not (Test-Path $DeployKey)) {
        Fail "공용 GitHub deploy key 가 없습니다. 메뉴 1 (PC 준비)을 먼저 하세요."
        return $false
    }
    return $true
}

# ---------------------------------------------------------------- 서버 목록
function Get-Servers {
    $list = @()
    if (Test-Path $ServersFile) {
        foreach ($line in Get-Content $ServersFile) {
            $t = $line.Trim()
            if ($t -eq "" -or $t.StartsWith("#")) { continue }
            $p = $t -split "\s+"
            if ($p.Count -ge 2) { $list += [pscustomobject]@{ Name = $p[0]; Ip = $p[1] } }
        }
    }
    return $list
}

function Add-Server($n, $addr) {
    if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Path $DataDir | Out-Null }
    $exists = Get-Servers | Where-Object { $_.Name -eq $n -or $_.Ip -eq $addr }
    if ($exists) { return }
    Add-Content -Path $ServersFile -Value "$n $addr" -Encoding UTF8
    Ok "서버 목록에 추가: $n ($addr)"
}

function Show-Servers {
    $s = @(Get-Servers)
    if ($s.Count -eq 0) { Say "  (등록된 서버 없음)"; return }
    for ($i = 0; $i -lt $s.Count; $i++) {
        Say ("  {0}. {1,-10} {2}" -f ($i + 1), $s[$i].Name, $s[$i].Ip)
    }
}

function Select-Server([switch]$AllowAll) {
    $s = @(Get-Servers)
    if ($s.Count -eq 0) { Fail "등록된 서버가 없습니다 (메뉴 2 로 설치하거나 8 로 추가)."; return $null }
    if ($Name) {
        $m = $s | Where-Object { $_.Name -eq $Name }
        if ($m) { return ,@($m) }
    }
    Show-Servers
    $hint = "번호"
    if ($AllowAll) { $hint = "번호 (전체는 a)" }
    $ans = Read-Host "  서버를 고르세요 $hint"
    if ($AllowAll -and $ans -eq "a") { return ,$s }
    $idx = 0
    if ([int]::TryParse($ans, [ref]$idx) -and $idx -ge 1 -and $idx -le $s.Count) { return ,@($s[$idx - 1]) }
    Fail "잘못된 선택"
    return $null
}

# ---------------------------------------------------------------- SSH 도우미
function Get-SshOpts([switch]$Batch) {
    $o = @("-i", $MyKey, "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=30")
    if ($Batch) { $o += @("-o", "BatchMode=yes", "-o", "ConnectTimeout=10") }
    return $o
}

function Invoke-Remote($addr, $cmd) {
    # 원격 출력은 화면에만 보여 주고(색 코드 제거), 함수는 종료 코드만 돌려준다
    $o = Get-SshOpts -Batch
    & ssh @o "root@$addr" $cmd 2>&1 | ForEach-Object { ("$_" -replace "\x1b\[[0-9;]*[A-Za-z]", "") } | Out-Host
    return $LASTEXITCODE
}

function Test-KeyLogin($addr) {
    $o = Get-SshOpts -Batch
    & ssh @o "root@$addr" "true" 2>$null
    return ($LASTEXITCODE -eq 0)
}

# ================================================================ 1. PC 준비 (최초 1회)
function Invoke-Setup {
    Step "1/3 이 PC 의 SSH 키"
    if (-not (Test-Path $SshDir)) { New-Item -ItemType Directory -Path $SshDir | Out-Null }
    if (Test-Path "$MyKey.pub") {
        Ok "이미 있습니다: $MyKey.pub"
    } else {
        Say "  키를 만듭니다. 질문이 나오면 3번 모두 그냥 Enter 를 누르세요."
        & ssh-keygen -t ed25519 -f $MyKey -C "$env:USERNAME-pc"
        if (-not (Test-Path "$MyKey.pub")) { Fail "키 생성 실패"; return }
        Ok "생성: $MyKey.pub"
    }
    Say "  (개인키 $MyKey 는 서버 열쇠입니다. USB 등에 .ssh 폴더째 백업해 두세요)"

    Step "2/3 공용 GitHub deploy key"
    if (Test-Path $DeployKey) {
        Ok "이미 있습니다: $DeployKey"
    } else {
        Say "  이미 GitHub 에 deploy key 를 등록한 서버가 있으면 그 서버의 키를 가져와 모든 서버에 같이 씁니다."
        Say "  (그러면 GitHub 등록·폰 인증을 다시 할 필요가 없습니다)"
        $src = Read-Host "  등록된 서버의 IP (예: live1 의 IP, 없으면 그냥 Enter)"
        if ($src) {
            $o = Get-SshOpts
            & scp @o "root@${src}:/root/.ssh/livemax_deploy" "root@${src}:/root/.ssh/livemax_deploy.pub" $SshDir
            if (-not (Test-Path $DeployKey)) { Fail "키를 가져오지 못했습니다 (IP·접속 확인)"; return }
            Ok "가져옴: $DeployKey"
            $nm = Read-Host "  그 서버 이름을 목록에 추가할까요? 이름 입력 (예: live1, 건너뛰려면 Enter)"
            if ($nm) { Add-Server $nm $src }
        } else {
            Say "  새 deploy key 를 만듭니다. 질문이 나오면 3번 모두 그냥 Enter."
            & ssh-keygen -t ed25519 -f $DeployKey -C "livemax-fleet"
            if (-not (Test-Path "$DeployKey.pub")) { Fail "키 생성 실패"; return }
            Get-Content "$DeployKey.pub" | Set-Clipboard
            Say "  공개키를 클립보드에 복사했습니다. 브라우저에서 GitHub 페이지를 엽니다."
            Say "  Add deploy key → Title: livemax-fleet → Key: Ctrl+V → Allow write access 체크하지 않음 → Add key"
            Start-Process $DeployKeysPage
            Pause-Enter "  등록(폰/메일 인증 포함)을 마쳤으면 Enter"
        }
    }

    Step "3/3 GitHub 연결 확인"
    $out = & ssh -i $DeployKey -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new -o BatchMode=yes -T git@github.com 2>&1
    if ("$out" -match "successfully authenticated") { Ok "GitHub 연결 OK — 새 서버 설치 준비 완료" }
    else { Fail "GitHub 이 이 키를 모릅니다: $out"; Say "  $DeployKeysPage 에 등록됐는지 확인하세요." }
}

# ================================================================ 2. 새 서버 설치
function Invoke-NewServer {
    if (-not (Test-Tools) -or -not (Test-MyKey) -or -not (Test-DeployKey)) { return }
    if (-not (Test-Path $Bootstrap)) { Fail "bootstrap.sh 를 찾을 수 없습니다: $Bootstrap (ZIP 을 통째로 풀었는지 확인)"; return }

    $n = $Name
    if (-not $n) { $n = Read-Host "  서버 이름 (예: live2)" }
    if ($n -notmatch "^[a-z0-9][a-z0-9-]{0,30}$") { Fail "이름은 영문 소문자·숫자·- 만 (예: live2)"; return }
    $addr = $Ip
    if (-not $addr) { $addr = Read-Host "  서버 IP (Contabo 메일/패널의 IP Address)" }
    $tmp = $null
    if (-not [System.Net.IPAddress]::TryParse($addr, [ref]$tmp)) { Fail "IP 형식 오류: $addr"; return }
    $dup = Get-Servers | Where-Object { $_.Name -eq $n -and $_.Ip -ne $addr }
    if ($dup) { Fail "이름 $n 은 이미 다른 IP 로 등록돼 있습니다 ($($dup.Ip))"; return }

    Step "1/7 서버 접속 준비 (root 비밀번호를 한 번만 입력합니다)"
    & ssh-keygen -R $addr 2>$null | Out-Null   # 같은 IP 로 재설치한 경우 예전 지문 제거
    if (Test-KeyLogin $addr) {
        Ok "이미 키로 접속됩니다"
    } else {
        $pub = (Get-Content "$MyKey.pub" -Raw).Trim()
        $body = ($pub -split "\s+")[1]
        Say "  password: 가 나오면 Contabo 에서 정한 root 비밀번호를 입력하세요 (화면에 안 보이는 게 정상)"
        & ssh -o StrictHostKeyChecking=accept-new -o PubkeyAuthentication=no "root@$addr" "umask 077; mkdir -p ~/.ssh; grep -qF '$body' ~/.ssh/authorized_keys 2>/dev/null || echo '$pub' >> ~/.ssh/authorized_keys"
        if ($LASTEXITCODE -ne 0) { Fail "접속 실패 — IP·비밀번호를 확인하세요"; return }
        if (-not (Test-KeyLogin $addr)) { Fail "키 등록 후에도 키 접속이 안 됩니다"; return }
        Ok "키 접속 확인 — 이제 비밀번호는 필요 없습니다"
    }

    Step "2/7 OS 확인"
    $os = & ssh @(Get-SshOpts -Batch) "root@$addr" "grep PRETTY_NAME /etc/os-release"
    Say "  $os"
    if ("$os" -notmatch "Ubuntu 2[4-9]") { Fail "Ubuntu 24.04 가 아닙니다. Contabo 패널에서 Ubuntu 24.04 로 재설치하세요."; return }

    Step "3/7 파일 전송 (bootstrap.sh, 공용 deploy key)"
    & scp @(Get-SshOpts -Batch) $Bootstrap "root@${addr}:/root/bootstrap.sh"
    if ($LASTEXITCODE -ne 0) { Fail "bootstrap.sh 전송 실패"; return }
    & scp @(Get-SshOpts -Batch) $DeployKey "$DeployKey.pub" "root@${addr}:/root/.ssh/"
    if ($LASTEXITCODE -ne 0) { Fail "deploy key 전송 실패"; return }
    [void](Invoke-Remote $addr "chmod 600 /root/.ssh/livemax_deploy; sed -i 's/\r$//' /root/bootstrap.sh")
    Ok "전송 완료"

    Step "4/7 서버 최초 설정 (업데이트·방화벽·보안·자동 업데이트 정책) — 수 분 걸립니다"
    # 1단계에서 키 접속을 직접 확인했으므로 --yes 로 비밀번호 로그인을 끈다
    $rc = Invoke-Remote $addr "bash /root/bootstrap.sh --hostname $n --yes"
    if ($rc -ne 0) { Fail "bootstrap 실패 (위 메시지 확인)"; return }
    if (-not (Test-KeyLogin $addr)) { Fail "bootstrap 후 접속 확인 실패 — Contabo VNC 콘솔로 확인 필요"; return }

    Step "5/7 GitHub 연결 확인 + 코드 받기"
    $rc = Invoke-Remote $addr "ssh -o BatchMode=yes -T git@github.com 2>&1 | grep -q 'successfully authenticated'"
    if ($rc -ne 0) { Fail "서버에서 GitHub 접속 실패 — 메뉴 1 (PC 준비)의 GitHub 연결 확인을 먼저 통과해야 합니다"; return }
    $rc = Invoke-Remote $addr "if [ -d /opt/livemax/.git ]; then cd /opt/livemax && git pull --ff-only; else git clone $RepoUrl /opt/livemax; fi"
    if ($rc -ne 0) { Fail "코드 받기 실패"; return }
    Ok "코드 준비 완료 (/opt/livemax)"

    Step "6/7 LiveMax 설치 — 5~10분 걸립니다"
    $rc = Invoke-Remote $addr "cd /opt/livemax && ./install.sh"
    if ($rc -ne 0) { Fail "설치 중 오류 (위 메시지를 복사해서 질문하세요)"; return }
    Add-Server $n $addr

    Step "7/7 마무리"
    $pw = & ssh @(Get-SshOpts -Batch) "root@$addr" "cat /opt/livemax/secrets/admin.initial_password 2>/dev/null"
    $needReboot = & ssh @(Get-SshOpts -Batch) "root@$addr" "test -f /var/run/reboot-required && echo yes"
    if ("$needReboot" -eq "yes") {
        # 새 서버는 아직 송출 중인 라이브가 없으므로 지금 재부팅해도 안전하다
        Say "  커널 업데이트 적용을 위해 재부팅합니다 (새 서버라 송출 영향 없음)..."
        [void](Invoke-Remote $addr "systemctl reboot")
        Start-Sleep -Seconds 20
        $back = $false
        for ($i = 0; $i -lt 30; $i++) {
            if (Test-KeyLogin $addr) { $back = $true; break }
            Start-Sleep -Seconds 10
        }
        if ($back) { Ok "재부팅 완료" } else { Warn "재부팅 후 아직 응답이 없습니다. 몇 분 뒤 메뉴 5 (상태 점검)로 확인하세요." }
    }

    Write-Host ""
    Write-Host "==================================================================" -ForegroundColor Green
    Write-Host "  설치 완료: $n ($addr)" -ForegroundColor Green
    if ($pw) {
        Write-Host "  대시보드 아이디:     admin" -ForegroundColor Green
        Write-Host "  대시보드 초기 비밀번호: $pw" -ForegroundColor Green
        Write-Host "  → 지금 적어 두세요. 첫 로그인 때 새 비밀번호로 바꾸게 됩니다." -ForegroundColor Green
    }
    Write-Host "==================================================================" -ForegroundColor Green
    Say "다음: 메뉴 3 (대시보드 열기) → 비밀번호 변경 → 폴더·미디어 → 라이브 추가 → 메뉴 4 (라이브 시작)"
}

# ================================================================ 3. 대시보드 열기
function Invoke-Dashboard {
    if (-not (Test-Tools) -or -not (Test-MyKey)) { return }
    $sel = Select-Server
    if (-not $sel) { return }
    $srv = $sel[0]
    $all = @(Get-Servers)
    $idx = 0
    for ($i = 0; $i -lt $all.Count; $i++) { if ($all[$i].Name -eq $srv.Name) { $idx = $i } }
    $local = 8081 + $idx   # 서버마다 다른 PC 포트 → 여러 대를 동시에 열 수 있음
    Step "$($srv.Name) 대시보드 연결 (SSH 터널 localhost:$local)"
    $tunArgs = "-N -i `"$MyKey`" -o IdentitiesOnly=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L ${local}:127.0.0.1:$DashRemotePort root@$($srv.Ip)"
    $p = Start-Process ssh -ArgumentList $tunArgs -PassThru -WindowStyle Hidden
    Start-Sleep -Seconds 3
    if ($p.HasExited) { Fail "터널 연결 실패 (이미 열려 있거나 서버 응답 없음)"; return }
    Start-Process "http://localhost:$local"
    Ok "브라우저에서 http://localhost:$local 을 열었습니다"
    Say "  (localhost 쿠키는 포트끼리 공유돼서, 다른 서버 대시보드를 열면 다시 로그인해야 할 수 있습니다)"
    Pause-Enter "  대시보드를 다 썼으면 Enter (연결 종료)"
    if (-not $p.HasExited) { Stop-Process -Id $p.Id -ErrorAction SilentlyContinue }
    Ok "연결 종료"
}

# ================================================================ 4. 라이브 시작
function Invoke-StartLives {
    if (-not (Test-Tools) -or -not (Test-MyKey)) { return }
    $sel = Select-Server
    if (-not $sel) { return }
    Step "$($sel[0].Name): 활성 라이브 중 안 도는 것만 시작 (31초 간격, 송출 중인 라이브는 그대로)"
    [void](Invoke-Remote $sel[0].Ip "/opt/livemax/start-lives.sh")
}

# ================================================================ 5. 상태 점검
function Invoke-Check {
    if (-not (Test-Tools) -or -not (Test-MyKey)) { return }
    $sel = Select-Server -AllowAll
    if (-not $sel) { return }
    foreach ($srv in $sel) {
        Step "$($srv.Name) ($($srv.Ip)) 상태 점검"
        $rc = Invoke-Remote $srv.Ip "/opt/livemax/healthcheck.sh"
        if ($rc -eq 0) { Ok "$($srv.Name): 송출 준비 OK" } else { Warn "$($srv.Name): FAIL/경고 항목 확인 필요" }
    }
}

# ================================================================ 6. 전체 서버 코드 업데이트
function Invoke-UpdateAll {
    if (-not (Test-Tools) -or -not (Test-MyKey)) { return }
    $sel = Select-Server -AllowAll
    if (-not $sel) { return }
    Say "  각 서버에서 git pull + install.sh 를 실행합니다. 송출 중인 라이브는 재시작하지 않으며,"
    Say "  새 코드는 각 라이브의 일일 재시작 때 반영됩니다."
    $result = @()
    foreach ($srv in $sel) {
        Step "$($srv.Name) ($($srv.Ip)) 업데이트"
        $rc = Invoke-Remote $srv.Ip "cd /opt/livemax && git pull --ff-only && ./install.sh"
        $state = "OK"
        if ($rc -ne 0) { $state = "실패" }
        $result += "  $($srv.Name): $state"
    }
    Step "업데이트 결과"
    $result | ForEach-Object { Say $_ }
}

# ================================================================ 7. 서버 접속
function Invoke-Ssh {
    if (-not (Test-Tools) -or -not (Test-MyKey)) { return }
    $sel = Select-Server
    if (-not $sel) { return }
    Say "  $($sel[0].Name) 에 접속합니다. 나올 때는 exit"
    & ssh @(Get-SshOpts) "root@$($sel[0].Ip)"
}

# ================================================================ 8. 서버 목록
function Invoke-Servers {
    Step "서버 목록 ($ServersFile)"
    Show-Servers
    $n = Read-Host "  기존 서버를 추가하려면 이름 입력 (그냥 Enter 면 닫기)"
    if (-not $n) { return }
    $addr = Read-Host "  IP"
    $tmp = $null
    if (-not [System.Net.IPAddress]::TryParse($addr, [ref]$tmp)) { Fail "IP 형식 오류"; return }
    Add-Server $n $addr
}

# ================================================================ 메뉴
function Show-Menu {
    while ($true) {
        Write-Host ""
        Write-Host "================ LiveMax 서버 관리 ================" -ForegroundColor Cyan
        Say " 1. PC 준비 (최초 1회)"
        Say " 2. 새 서버 설치"
        Say " 3. 대시보드 열기"
        Say " 4. 라이브 시작 (대시보드에서 설정한 라이브 송출 시작)"
        Say " 5. 상태 점검"
        Say " 6. 서버 코드 업데이트"
        Say " 7. 서버 접속 (SSH)"
        Say " 8. 서버 목록 보기 / 기존 서버 추가"
        Say " 0. 끝내기"
        $c = Read-Host "번호"
        switch ($c) {
            "1" { Invoke-Setup }
            "2" { Invoke-NewServer }
            "3" { Invoke-Dashboard }
            "4" { Invoke-StartLives }
            "5" { Invoke-Check }
            "6" { Invoke-UpdateAll }
            "7" { Invoke-Ssh }
            "8" { Invoke-Servers }
            "0" { return }
            default { Warn "0~8 중에서 고르세요" }
        }
    }
}

switch ($Command) {
    "setup"     { Invoke-Setup }
    "new"       { Invoke-NewServer }
    "dashboard" { Invoke-Dashboard }
    "start"     { Invoke-StartLives }
    "check"     { Invoke-Check }
    "update"    { Invoke-UpdateAll }
    "ssh"       { Invoke-Ssh }
    "servers"   { Invoke-Servers }
    default     { Show-Menu }
}
