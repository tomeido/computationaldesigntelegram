# 컴퓨트 디자인 브리핑 | Web3 · AI

Web3·블록체인 관련 **컴퓨테이셔널 디자인** 정보를 우선 수집해 핵심을 한국어로 정리하고, 텔레그램 채널에 자동 게시하는 Python 봇입니다. 온체인 생성예술·크리에이티브 코딩·AI 디자인 도구를 다룹니다.

수집 → 관련성 선별 → 한국어 요약 → 원문 링크와 함께 게시하는 흐름입니다. 기본 발행 시간은 **한국시간 오전 9시·오후 6시**, 회당 최대 5건입니다. 관련성 있는 새 소식이 없으면 게시하지 않습니다.

## 바로 실행해 보기

Python 3.11 이상과 Bash가 필요합니다. Linux 서버용 구성이며, Windows에서는 WSL 또는 Docker를 사용하세요. 프로젝트 폴더에서 실행하세요.

```bash
bash scripts/setup.sh
source .venv/bin/activate
python -m compdesign_bot demo
python -m compdesign_bot collect
```

`demo`는 네트워크·API 키 없이 게시물 형식 예시를 보여줍니다. `collect`는 실제 피드를 읽고 선별한 후보를 보여주며, OpenAI와 Telegram에는 요청하지 않습니다. 설치 스크립트는 `.venv`를 만들고 `requirements.lock`의 버전을 설치한 뒤, 기존 `.env`가 없다면 설정 예시를 복사합니다. 기존 설정은 덮어쓰지 않습니다.

## 텔레그램 연결

1. 텔레그램의 공식 [@BotFather](https://t.me/BotFather)에서 `/newbot`을 실행해 봇을 만들고 토큰을 발급받습니다. [공식 안내](https://core.telegram.org/bots/features#botfather)
2. 텔레그램 앱에서 새 채널을 만듭니다. 이름·설명·고정 게시물 문구는 [채널 운영 안내](docs/channel.md)에 준비되어 있습니다. 채널 생성은 사용자 계정에서 진행해야 합니다. [공식 API 문서](https://core.telegram.org/method/channels.createChannel)
3. 새 봇을 채널 **관리자**로 추가하고 **메시지 게시 / Post Messages** 권한을 부여합니다.
4. 공개 채널에 사용자 이름을 지정합니다. `t.me/compute_design_kr`를 선택했다면 채널 ID는 `@compute_design_kr`입니다. 실제 사용 가능한 이름을 입력하세요.
5. 로컬 `.env`를 편집해 다음 값을 채웁니다. 키와 토큰은 채팅이나 저장소에 올리지 마세요.

```dotenv
TELEGRAM_BOT_TOKEN=BotFather에서_받은_토큰
TELEGRAM_CHANNEL_ID=@실제_채널_사용자이름
OPENAI_API_KEY=OpenAI_API_키
```

`.env` 권한은 설치 스크립트에서 `600`으로 설정합니다. 수동으로 만들었다면 `chmod 600 .env`를 실행하세요. 공개 채널의 `@사용자이름`과 비공개 채널의 숫자 ID를 모두 지원하며, [Telegram의 sendMessage 문서](https://core.telegram.org/bots/api#sendmessage)에 주소 형식이 나와 있습니다.

비공개 채널이라면 먼저 `.env`에 봇 토큰을 저장하고 봇을 관리자로 추가한 뒤, 채널에 짧은 글을 하나 직접 게시하세요. 다음 명령으로 봇이 받은 채널 업데이트의 ID와 이름을 확인할 수 있습니다.

```bash
python -m compdesign_bot discover-channel
```

대상 채널의 `-100`으로 시작하는 숫자 ID를 `.env`의 `TELEGRAM_CHANNEL_ID`에 입력하세요. 이 명령은 최근 업데이트를 읽기만 하며, 글을 게시하거나 업데이트를 삭제하지 않습니다. 봇을 다른 서비스에서도 사용 중이면 해당 서비스가 업데이트를 먼저 처리했거나 웹훅을 설정했을 수 있습니다. 이 경우 이 채널용 봇을 별도로 만들어 다시 연결하세요.

## 연결 확인과 첫 게시

```bash
# 설정 및 Telegram 봇·채널 접근 권한 확인: 게시하지 않음
python -m compdesign_bot doctor

# 실제 소스를 한국어로 요약해 터미널에서 확인: OpenAI API 사용, 게시하지 않음
python -m compdesign_bot preview

# 기존 채널의 이름·설명 변경이 필요할 때만 실행
python -m compdesign_bot configure-channel

# 채널에 실제 게시
python -m compdesign_bot publish

# 켜 둔 동안 정해진 시각에 자동 게시
python -m compdesign_bot run
```

`preview`, `publish`, `run`의 요약 단계는 OpenAI API를 사용하므로 API 이용료가 발생할 수 있습니다. `OPENAI_MODEL`의 기본값은 `gpt-4.1-mini`이며, 계정에서 사용할 수 있는 모델을 지정해야 합니다. API 키 발급·결제 설정은 운영자의 OpenAI 계정에서 진행하세요. 실제 게시에는 Telegram 토큰, 대상 채널, 관리자 권한이 모두 필요합니다.

`run`을 실행하는 컴퓨터가 꺼지거나 잠들면 자동 게시도 멈춥니다. 계속 운영하려면 켜져 있는 서버에서 실행하거나 아래 Docker 구성을 사용하세요.

스케줄러는 예약 시각부터 5분 안에 실행을 시도합니다. 이미 지난 회차를 한꺼번에 발행하지 않으며, 즉시 한 차례 발행하려면 `publish`를 실행하세요.

## 명령어

명령어는 서버의 터미널에서 실행합니다. 텔레그램 채팅 명령어가 아닙니다.

| 명령어 | 동작 | 외부 게시 |
| --- | --- | --- |
| `demo` | 고정 예시 출력, API 키 불필요 | 없음 |
| `collect` / `collect --json` | 피드 수집 및 우선순위 후보 출력 | 없음 |
| `preview` | 실제 후보를 한국어로 요약해 출력 | 없음 |
| `doctor` | 설정 및 Telegram 봇·채널·관리자 권한 확인 | 없음 |
| `discover-channel` | 최근 업데이트에서 채널 ID·이름 확인, 토큰만 필요 | 없음 |
| `configure-channel` | 기존 채널의 이름·설명 변경 | 채널 정보 변경 |
| `publish` | 신규 후보 요약 및 한 차례 게시 | 있음 |
| `run` | 설정 시각마다 신규 후보 게시 | 있음 |
| `status` | 로컬 발송 기록 확인 | 없음 |
| `resolve-delivery ID --sent` | 불확실한 발송을 확인 후 발송 완료 처리 | 없음 |
| `resolve-delivery ID --retry` | 불확실한 발송을 확인 후 재시도 허용 | 다음 발행 때 가능 |

전체 옵션은 `python -m compdesign_bot --help`로 확인하세요.

## 어떤 정보를 다루나요?

우선순위는 Web3·블록체인과 컴퓨테이셔널 디자인이 함께 등장하는 소식이 가장 높습니다. Web3·AI·디자인이 겹치는 소식, AI·컴퓨테이셔널 디자인, 일반 컴퓨테이셔널 디자인을 이어서 선별합니다. 일반 코인 시황·가격 예측·에어드롭 홍보는 제외하는 방향으로 필터링합니다.

봇은 설정된 RSS/Atom 피드의 제목과 발췌를 근거로 요약합니다. 원문 전체를 읽거나 사실을 독립적으로 검증하는 기능은 없습니다. 날짜가 없거나 너무 오래된 항목은 게시 후보에서 제외합니다. 소스 구분과 추가 방법은 [수집원 안내](docs/sources.md)를 참고하세요.

게시 기록은 SQLite에 저장합니다. 전송 응답을 받지 못해 성공 여부가 불확실한 경우에는 즉시 재전송하지 않고 기록을 남깁니다. `status`를 확인한 뒤 실제 채널에서 게시 여부를 확인하고 다음 중 하나를 실행하세요.

```bash
# ID는 status가 보여주는 해당 발송 기록 ID로 바꾸세요.
python -m compdesign_bot resolve-delivery ID --sent
# 실제 채널에 게시되지 않은 것이 확인되었을 때:
python -m compdesign_bot resolve-delivery ID --retry
```

발송 기록을 삭제하면 기존 항목이 다시 게시될 수 있습니다. 운영 중인 인스턴스는 하나만 유지하고 데이터베이스를 보존하세요.

## 설정

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | 비어 있음 | BotFather에서 발급한 토큰 |
| `TELEGRAM_CHANNEL_ID` | 비어 있음 | 공개 채널 `@username` 또는 비공개 채널 숫자 ID |
| `OPENAI_API_KEY` | 비어 있음 | 한국어 요약용 API 키 |
| `OPENAI_MODEL` | `gpt-4.1-mini` | 요약 모델 |
| `CHANNEL_NAME` | `컴퓨트 디자인 브리핑 \| Web3 · AI` | 채널 설정 명령에서 사용할 이름 |
| `TIMEZONE` | `Asia/Seoul` | 발행 시간대 |
| `POST_TIMES` | `09:00,18:00` | 하루 발행 시각, 쉼표로 구분 |
| `MAX_POSTS` | `5` | 회당 최대 게시 수, 1~10 |
| `MAX_AGE_DAYS` | `7` | 게시 후보의 최대 나이, 1~30일 |
| `MAX_CANDIDATES` | `15` | 한 회차에서 검토할 최대 후보 수, 1~30 |
| `SOURCES_FILE` | `config/sources.json` | 수집원 설정 파일 |
| `DATABASE_PATH` | `data/bot.sqlite3` | 발송 기록 경로 |

환경변수가 이미 설정되어 있으면 `.env`보다 우선합니다. 설정을 바꾼 뒤 실행 중인 프로세스를 재시작하세요.

## Docker로 계속 운영하기

Docker와 Docker Compose가 설치된 서버에서 `.env`를 먼저 작성한 뒤 실행하세요. 아래 구성은 호스트의 `config/`를 읽기 전용으로 연결하고, 발송 기록은 `bot-data` 볼륨에 보관합니다. Docker에서는 `SOURCES_FILE`과 `DATABASE_PATH` 기본값을 유지하세요.

```bash
docker compose build
docker compose run --rm bot python -m compdesign_bot doctor
docker compose run --rm bot python -m compdesign_bot preview
docker compose up -d
docker compose logs --tail=100 -f bot
```

중지는 `docker compose down`, 재시작은 `docker compose restart`입니다. `.env`를 변경했다면 `docker compose up -d --force-recreate`로 컨테이너를 다시 만드세요. `docker compose down -v`는 발송 기록 볼륨까지 삭제하므로 기록을 보존할 때 사용하지 마세요.

## 개발 확인

```bash
python -m pip install -e '.[dev]'
python -m pytest
python -m ruff check .
```

테스트는 가짜 외부 응답을 사용합니다. 테스트 통과만으로 실제 Telegram 채널 연결이나 OpenAI 계정 사용 가능 여부가 확인되지는 않습니다. 운영 계정 설정 후 `doctor`, `preview`, `publish` 순서로 확인하세요.
