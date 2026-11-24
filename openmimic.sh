#!/usr/bin/env bash
# ============================================================================
#  OpenMimic 실행 도구 — 켜기 · 끄기 · 상태 · 업데이트 · 로그 · 부팅 시 자동 시작
#
#    ./openmimic.sh start          켜기 (백그라운드, 로그는 openmimic.log)
#    ./openmimic.sh stop           끄기
#    ./openmimic.sh restart        다시 켜기
#    ./openmimic.sh status         켜져 있는지 · 접속 주소
#    ./openmimic.sh log            로그 보기 (Ctrl+C 로 나감 — OpenMimic 은 계속 돎)
#    ./openmimic.sh update         git pull 후 다시 켜기
#    ./openmimic.sh autostart on   로그인(부팅) 때 자동으로 켜기 — 이 계정만 (systemd --user)
#    ./openmimic.sh autostart off  자동 시작 끄기
#
#  설치 스크립트(lerobot_conda.sh)가 만든 activate.sh 로 환경을 켜므로, 캘리브레이션·데이터 폴더가
#  항상 설치 폴더(data/hf)로 맞춰집니다. ~/.bashrc 와 다른 사람 계정은 건드리지 않습니다.
# ============================================================================
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PIDF="${DIR}/openmimic_server.pid"
LOGF="${DIR}/openmimic.log"
PORT=8080
UNIT="openmimic.service"
UNITDIR="${HOME}/.config/systemd/user"

service_active() { systemctl --user is-active --quiet "${UNIT}" 2>/dev/null; }

need_env() {
  if [[ ! -f "${DIR}/activate.sh" ]]; then
    echo "activate.sh 가 없습니다 — 먼저 설치하세요: ${DIR}/lerobot_conda.sh"; exit 1
  fi
  # conda 활성화·비활성화 스크립트(예: XPU 휠과 함께 오는 oneAPI 의 mpivars.*.sh)는 정의 안 된 변수를 읽습니다.
  # 이 파일의 set -u 가 그대로 걸려 있으면 거기서 셸이 조용히 끝나므로, 환경을 켜는 동안만 풉니다.
  set +u
  # shellcheck disable=SC1091
  source "${DIR}/activate.sh"
  local rc=$?
  set -u
  if (( rc != 0 )) || [[ "${CONDA_DEFAULT_ENV:-}" != "openmimic" ]]; then
    echo "환경을 켜지 못했습니다 (activate.sh) — 터미널에서 source ${DIR}/activate.sh 가 되는지 확인하세요"; exit 1
  fi
  cd "${DIR}" || exit 1
}

running_pid() {
  local p
  p="$(cat "${PIDF}" 2>/dev/null)"
  # pid 가 재사용됐거나 다른 폴더의 OpenMimic 이면 끄지 않도록: 명령줄(main.py, --worker 아님)과 작업 폴더를 함께 봅니다
  if [[ -n "${p}" ]] && kill -0 "${p}" 2>/dev/null \
     && tr '\0' ' ' < "/proc/${p}/cmdline" 2>/dev/null | grep -q "main.py" \
     && ! tr '\0' ' ' < "/proc/${p}/cmdline" 2>/dev/null | grep -q -- "--worker" \
     && [[ "$(readlink -f "/proc/${p}/cwd" 2>/dev/null)" == "$(readlink -f "${DIR}")" ]]; then
    echo "${p}"
  fi
}

urls() {
  local ips
  ips="$(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.' | grep -v -E '^(127\.|172\.17\.)')"
  if [[ -z "${ips}" ]]; then echo "  http://<이 PC 의 IP>:${PORT}/"; return; fi
  for ip in ${ips}; do echo "  http://${ip}:${PORT}/"; done
}

do_start() {
  local p
  p="$(running_pid)"
  if [[ -n "${p}" ]]; then echo "이미 켜져 있습니다 (pid ${p})"; urls; return 0; fi
  if service_active; then
    echo "자동 시작 서비스로 켜져 있습니다 — ./openmimic.sh status 로 확인하세요"; return 0
  fi
  need_env
  nohup python -u main.py >> "${LOGF}" 2>&1 &
  echo $! > "${PIDF}"
  for _ in $(seq 1 40); do
    if curl -s -o /dev/null "http://127.0.0.1:${PORT}/jobs" 2>/dev/null; then break; fi
    if ! kill -0 "$(cat "${PIDF}")" 2>/dev/null; then
      echo "켜다가 멈췄습니다 — 로그 끝부분:"; tail -n 20 "${LOGF}"; rm -f "${PIDF}"; exit 1
    fi
    sleep 0.5
  done
  echo "켰습니다 (pid $(cat "${PIDF}"), 로그 ${LOGF}). 브라우저에서:"
  urls
  grep -a "^!! " "${LOGF}" | tail -n 3
}

do_stop() {
  local p
  if service_active; then
    systemctl --user stop "${UNIT}" && echo "자동 시작 서비스를 껐습니다 (다음 로그인 때 다시 켜집니다)"
  fi
  p="$(running_pid)"
  if [[ -z "${p}" ]]; then rm -f "${PIDF}"; echo "꺼져 있습니다"; return 0; fi
  kill "${p}"
  for _ in $(seq 1 20); do kill -0 "${p}" 2>/dev/null || break; sleep 0.5; done
  if kill -0 "${p}" 2>/dev/null; then kill -9 "${p}"; fi
  rm -f "${PIDF}"
  echo "껐습니다 (수집·학습 같은 작업은 각자 계속 돌고, 다시 켜면 작업 탭에 그대로 보입니다)"
}

do_status() {
  local p
  p="$(running_pid)"
  if [[ -n "${p}" ]]; then
    echo "켜져 있음 (pid ${p})"; urls
  elif service_active; then
    echo "켜져 있음 (자동 시작 서비스)"; urls
  else
    echo "꺼져 있음 — ./openmimic.sh start"
  fi
  if systemctl --user is-enabled --quiet "${UNIT}" 2>/dev/null; then echo "자동 시작: 켜짐"; else echo "자동 시작: 꺼짐"; fi
}

do_update() {
  cd "${DIR}" || exit 1
  if ! git diff --quiet || ! git diff --cached --quiet; then
    echo "고친 파일이 있어 업데이트를 멈춥니다 — git status 로 확인하세요"; exit 1
  fi
  git pull --ff-only || { echo "git pull 실패 — 위 메시지를 확인하세요"; exit 1; }
  do_restart
  echo "브라우저는 강력 새로고침(Ctrl+Shift+R) 하세요"
}

write_unit() {
  # KillMode=process: 서비스를 끄거나 다시 켤 때 main.py 만 끕니다. 기본값(control-group)이면 같은 cgroup 의
  # 수집·학습 작업 프로세스까지 함께 죽어, 다시 켠 뒤 작업 탭에서 이어 볼 수 없습니다 (nohup 실행과 같게).
  mkdir -p "${UNITDIR}"
  cat > "${UNITDIR}/${UNIT}" <<EOF
[Unit]
Description=OpenMimic (${DIR})
After=network-online.target

[Service]
Type=simple
ExecStart=/bin/bash -c 'source "${DIR}/activate.sh" && cd "${DIR}" && exec python -u main.py'
KillMode=process
Restart=on-failure
RestartSec=5
StandardOutput=append:${LOGF}
StandardError=append:${LOGF}

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
}

# 예전 버전이 만든 서비스 파일(KillMode 없음)을 지금 내용으로 바꿉니다
refresh_unit() {
  if [[ -f "${UNITDIR}/${UNIT}" ]] && ! grep -q "^KillMode=process" "${UNITDIR}/${UNIT}"; then
    write_unit && echo "자동 시작 서비스 파일을 새 버전으로 바꿨습니다 (KillMode=process)"
  fi
}

do_restart() {
  if service_active; then
    refresh_unit
    systemctl --user restart "${UNIT}" && echo "다시 켰습니다 (자동 시작 서비스)"; urls
  else
    do_stop; do_start
  fi
}

do_autostart() {
  case "${1:-}" in
    on)
      if ! command -v systemctl >/dev/null; then echo "systemd 가 없는 시스템입니다"; exit 1; fi
      if ! systemctl --user show-environment >/dev/null 2>&1; then
        echo "이 계정의 systemd 사용자 세션에 연결하지 못했습니다 — 바탕화면으로 로그인한 터미널에서 다시 해 보세요"; exit 1
      fi
      write_unit
      do_stop >/dev/null 2>&1
      systemctl --user enable --now "${UNIT}" && echo "자동 시작을 켰습니다 — 이 계정으로 로그인하면 OpenMimic 이 켜집니다"
      if loginctl show-user "${USER:-$(id -un)}" -p Linger 2>/dev/null | grep -q "yes"; then
        echo "로그인하지 않아도 부팅 때 켜집니다 (linger 켜짐)"
      else
        echo "로그인하지 않은 상태에서도 부팅 때 켜려면 한 번: sudo loginctl enable-linger ${USER:-$(id -un)}"
      fi
      ;;
    off)
      systemctl --user disable --now "${UNIT}" 2>/dev/null
      rm -f "${UNITDIR}/${UNIT}"; systemctl --user daemon-reload 2>/dev/null
      echo "자동 시작을 껐습니다"
      ;;
    *) echo "사용법: ./openmimic.sh autostart on | off"; exit 1 ;;
  esac
}

case "${1:-}" in
  start)     do_start ;;
  stop)      do_stop ;;
  restart)   do_restart ;;
  status)    do_status ;;
  log)       tail -n 50 -f "${LOGF}" ;;
  update)    do_update ;;
  autostart) do_autostart "${2:-}" ;;
  *) sed -n '3,13p' "$0" | sed 's/^#//'; exit 1 ;;
esac
