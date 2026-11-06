<# :
@echo off
setlocal
title LiveMax Server Tool
set "LM_SELF=%~f0"
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "iex ([IO.File]::ReadAllText($env:LM_SELF, [Text.Encoding]::UTF8))"
exit /b
#>
# ============================================================================
#  LiveMax 서버 관리 도구 (PC 전용)
#
#  더블클릭 → 서버 번호 선택 → 할 일 번호 선택.
#  평소 운영은 웹(https://서버IP)에서 다 된다. 이 도구는 웹으로 안 되는 일(비밀번호 분실, 웹이 안 열릴 때,
#  서버 재부팅)과 서버 접속을 번호 하나로 하게 해 준다.
#
#  ▼ 아래 $Servers 에 서버를 적는다 (메모장으로 이 파일을 열어 수정 → 저장).
#    한 줄에 서버 하나. Name 은 아무 이름, Host 는 서버 IP.
#    User 는 접속 아이디: Contabo 는 root (생략 가능), OVH 는 ubuntu.
#    root 가 아니면 서버 명령 앞에 sudo 를 자동으로 붙인다 (비밀번호는 그 계정 비밀번호).
#    GitHub 에 있는 이 파일은 예시만 들어 있다. 진짜 IP 는 내 PC 의 사본에만 적는다.
# ============================================================================

$Servers = @(
    @{ Name = "LIVE 1"; Host = "서버IP를_여기에" }
    # @{ Name = "LIVE 2"; Host = "1.2.3.4"; User = "ubuntu" }   # OVH 서버 예시
    # @{ Name = "LIVE 3"; Host = "5.6.7.8" }
)
$User = "root"

# ---------------------------------------------------------------- 이 아래는 고치지 않아도 된다
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$ErrorActionPreference = "Continue"

function Line { Write-Host ("-" * 60) -ForegroundColor DarkGray }
function Ask($q) { Write-Host ""; return (Read-Host $q).Trim() }
function Pause-Key { Write-Host ""; Read-Host "Enter 를 누르면 메뉴로 돌아갑니다" | Out-Null }

function UserOf($srv) { if ($srv.User) { return $srv.User } else { return $User } }

function Run-Remote($srv, $cmd) {
    # -t: 비밀번호 재설정처럼 화면 입출력이 필요한 명령도 그대로 보이게
    $u = UserOf $srv
    Write-Host ""
    Write-Host "  > $($srv.Name) 에서 실행: $cmd" -ForegroundColor DarkGray
    Write-Host "  (비밀번호를 물으면 $u 비밀번호 입력 — 화면에 안 보이는 게 정상)" -ForegroundColor DarkGray
    if ($u -ne "root") {
        # root 가 아닌 계정(OVH 의 ubuntu): 관리자 권한으로 실행 — 명령 안의 ' 는 셸 규칙대로 감싼다
        $cmd = "sudo -- sh -c '" + $cmd.Replace("'", "'\''") + "'"
    }
    Line
    ssh -t "$u@$($srv.Host)" $cmd
    Line
    if ($LASTEXITCODE -eq 255) {
        Write-Host "  접속 실패. IP 가 맞는지, 서버가 켜져 있는지 확인하세요." -ForegroundColor Yellow
        Write-Host "  서버를 다시 설치한 뒤라면 메뉴의 [R] 을 먼저 실행하세요." -ForegroundColor Yellow
    }
}

function Confirm($msg) {
    Write-Host ""
    Write-Host "  $msg" -ForegroundColor Yellow
    return ((Read-Host "  정말 진행할까요? (y 입력 후 Enter)").Trim().ToLower() -eq "y")
}

if (-not (Get-Command ssh -ErrorAction SilentlyContinue)) {
    Write-Host "ssh 프로그램이 없습니다." -ForegroundColor Red
    Write-Host "Windows 설정 → 앱 → 선택적 기능 → 기능 추가 → 'OpenSSH 클라이언트' 설치 후 다시 실행하세요."
    Read-Host "Enter 로 종료" | Out-Null
    return
}

$valid = @($Servers | Where-Object { $_.Host -match '^[0-9a-zA-Z.\-:]+$' })
if (-not $valid.Count) {
    Write-Host ""
    Write-Host "  등록된 서버가 없습니다." -ForegroundColor Yellow
    Write-Host "  이 파일(LiveMax-SSH.bat)을 메모장으로 열어 맨 위 `$Servers 에 서버 IP 를 적고 저장하세요."
    Write-Host '  예:  @{ Name = "LIVE 1"; Host = "13.140.201.245" }'
    Write-Host ""
    Read-Host "Enter 로 종료" | Out-Null
    return
}

while ($true) {
    Clear-Host
    Write-Host ""
    Write-Host "  LiveMax 서버 관리" -ForegroundColor Green
    Line
    for ($i = 0; $i -lt $valid.Count; $i++) {
        Write-Host ("   {0}) {1,-12} {2}" -f ($i + 1), $valid[$i].Name, $valid[$i].Host)
    }
    Write-Host "   0) 종료"
    $n = Ask "  서버 번호"
    if ($n -eq "0" -or $n -eq "") { break }
    if (-not ($n -match '^\d+$') -or [int]$n -lt 1 -or [int]$n -gt $valid.Count) { continue }
    $srv = $valid[[int]$n - 1]

    while ($true) {
        Clear-Host
        Write-Host ""
        Write-Host "  $($srv.Name)  ($($srv.Host))" -ForegroundColor Green
        Line
        Write-Host "   1) 서버 접속 (명령창 열기 — 나올 때는 exit)"
        Write-Host "   2) 웹 관리 화면 열기 (https://$($srv.Host))"
        Write-Host "   3) 상태 보기 (LiveMax 가 켜져 있는지)"
        Write-Host "   4) 관리 프로그램 로그 (최근 200줄)"
        Write-Host "   5) 라이브 송출 로그 (라이브 번호 입력)"
        Write-Host ""
        Write-Host "   6) 관리자 비밀번호 재설정 (웹 비밀번호를 잊었을 때)" -ForegroundColor Cyan
        Write-Host "   7) LiveMax 다시 시작 (웹이 안 열릴 때 — 라이브 1~2분 끊김)" -ForegroundColor Cyan
        Write-Host "   8) 업데이트 (웹의 업데이트 버튼과 같음)" -ForegroundColor Cyan
        Write-Host "   9) 서버 재부팅 ('재부팅 필요' 알림이 떴을 때 — 라이브 1~2분 끊김)" -ForegroundColor Cyan
        Write-Host ""
        Write-Host "   U) 호스트 명령 최신화 + 옛 이미지 정리 (한 번이면 됨)"
        Write-Host "   R) 서버를 다시 설치해서 접속이 안 될 때 (PC 에 저장된 서버 지문 지우기)"
        Write-Host "   0) 서버 목록으로"
        $c = (Ask "  번호").ToUpper()
        switch ($c) {
            "1" {
                Write-Host ""
                if ((UserOf $srv) -ne "root") { Write-Host "  접속 후 관리자 권한이 필요하면: sudo -i" -ForegroundColor DarkGray }
                ssh "$(UserOf $srv)@$($srv.Host)"; Pause-Key
            }
            "2" { Start-Process "https://$($srv.Host)" }
            "3" { Run-Remote $srv "livemax status; echo; df -h / | tail -1; echo; uptime"; Pause-Key }
            "4" { Run-Remote $srv "livemax logs"; Pause-Key }
            "5" {
                $lv = Ask "  라이브 번호 (예: 1 → live1, 그냥 Enter 면 1)"
                if ($lv -eq "") { $lv = "1" }
                if ($lv -match '^\d{1,4}$') {
                    Run-Remote $srv "tail -n 150 /opt/livemax/data/logs/live-live$lv.log 2>/dev/null || echo '로그 파일이 없습니다 (라이브 번호 확인)'"
                    Write-Host "  로그 시각은 UTC 입니다 (한국 시간 = +9시간). 웹의 로그 보기에서는 한국 시간으로 볼 수 있어요." -ForegroundColor DarkGray
                }
                Pause-Key
            }
            "6" {
                if (Confirm "웹 관리자 비밀번호를 새 임시 비밀번호로 바꿉니다. 로그인된 기기는 모두 로그아웃됩니다.") {
                    Run-Remote $srv "livemax password"
                    Write-Host "  위에 나온 임시 비밀번호로 웹에 로그인하면 새 비밀번호로 바꾸라는 창이 뜹니다." -ForegroundColor Green
                }
                Pause-Key
            }
            "7" { if (Confirm "LiveMax 를 다시 시작합니다. 모든 라이브가 1~2분 끊겼다가 순서대로 돌아옵니다.") { Run-Remote $srv "livemax restart" }; Pause-Key }
            "8" { if (Confirm "최신 버전으로 업데이트합니다. 모든 라이브가 1~2분 끊겼다가 돌아옵니다.") { Run-Remote $srv "livemax update" }; Pause-Key }
            "9" {
                if (Confirm "서버를 재부팅합니다. 모든 라이브가 1~2분 끊겼다가 자동으로 돌아옵니다.") {
                    Run-Remote $srv "reboot"
                    Write-Host "  재부팅 중입니다. 1~2분 뒤 웹(https://$($srv.Host))을 열어 확인하세요." -ForegroundColor Green
                }
                Pause-Key
            }
            "U" {
                Run-Remote $srv "curl -fsSL https://raw.githubusercontent.com/alligogo/LiveMax/main/host/livemax -o /usr/local/bin/livemax && chmod 755 /usr/local/bin/livemax && docker image prune -f && echo '완료'"
                Pause-Key
            }
            "R" {
                ssh-keygen -R $srv.Host | Out-Null
                Write-Host ""
                Write-Host "  지웠습니다. 다음 접속 때 처음처럼 질문이 나오면 yes 를 입력하세요." -ForegroundColor Green
                Pause-Key
            }
            "0" { break }
            default { }
        }
        if ($c -eq "0") { break }
    }
}
