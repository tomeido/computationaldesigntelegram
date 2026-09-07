#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"
python_bin="${PYTHON_BIN:-python3}"

"$python_bin" -c 'import sys; sys.exit("Python 3.12 이상이 필요합니다.") if sys.version_info < (3, 12) else None'

if [[ ! -x .venv/bin/python ]]; then
  if ! "$python_bin" -m venv .venv; then
    printf '%s\n' '기본 venv 생성이 실패했습니다. 시스템 pip를 사용하는 방식으로 다시 시도합니다.'
    if ! "$python_bin" -m venv --without-pip .venv; then
      printf '%s\n' 'Python venv 모듈을 설치한 뒤 다시 실행하세요. Ubuntu/Debian 예: sudo apt install python3-venv' >&2
      exit 1
    fi
  fi
fi

.venv/bin/python -c 'import sys; sys.exit("기존 .venv는 Python 3.12 이상이어야 합니다. 새 가상환경을 만든 뒤 다시 실행하세요.") if sys.version_info < (3, 12) else None'

if .venv/bin/python -m pip --version >/dev/null 2>&1; then
  .venv/bin/python -m pip install -r requirements.lock -e '.[local]'
else
  if ! "$python_bin" -m pip --python .venv/bin/python install pip -r requirements.lock -e '.[local]'; then
    printf '%s\n' '설치에 실패했습니다. Python venv/ensurepip 또는 --python을 지원하는 시스템 pip가 필요합니다.' >&2
    exit 1
  fi
fi

if [[ ! -f .env ]]; then
  (umask 077; cp .env.example .env)
fi
chmod 600 .env
mkdir -p data

printf '%s\n' \
  '설치 완료. 다음 명령으로 예시를 확인하세요:' \
  '  source .venv/bin/activate' \
  '  python -m compdesign_bot demo' \
  '번역 모델을 한 번 설치하세요:' \
  '  python -m compdesign_bot setup-translator' \
  '.env에 텔레그램 봇 토큰을 입력한 뒤 README.md의 연결 절차를 진행하세요.'
