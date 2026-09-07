# 컴퓨트 디자인 브리핑 | Web3 · AI

**한국어 컴퓨테이셔널 디자인 정보봇**입니다. Web3·블록체인과 디자인이 겹치는 소식을 우선 수집하고, AI 디자인·온체인 생성예술·크리에이티브 코딩 소식을 짧게 전합니다. 번역은 **Google AI Studio의 Gemini API** 또는 **서버의 로컬 모델** 중 선택합니다. OpenAI API는 사용하지 않습니다.

연결할 봇: **[@ComputationalDesign_bot](https://t.me/ComputationalDesign_bot)**. 실행 중인 봇과 개인 대화에서 `/start`를 누르거나 `/latest`로 브리핑을 요청할 수 있습니다. 채널을 연결하면 **한국시간 오전 9시·오후 6시**, 회당 최대 5건을 자동 게시합니다.

## 설치와 번역 방식 선택

Python **3.12 이상**과 Bash가 필요합니다. Linux 서버용 구성이며, Windows에서는 WSL 또는 Docker를 사용하세요. 프로젝트 폴더에서 실행합니다.

```bash
bash scripts/setup.sh
source .venv/bin/activate

# 네트워크·토큰·번역 모델 없이 게시물 형식 확인
python -m compdesign_bot demo
```

설치 스크립트는 `.venv`를 만들고 `requirements.lock`의 패키지와 로컬 번역 기능을 설치합니다. 기존 `.env`가 없으면 설정 예시를 복사하며, 기존 값은 덮어쓰지 않습니다. 아래 두 방식 중 하나를 `.env`에 설정하세요.

### Gemini 번역

[Google AI Studio](https://aistudio.google.com/apikey)에서 발급한 키를 로컬 `.env`에 입력합니다. 모델 다운로드는 필요하지 않습니다.

```dotenv
TRANSLATION_PROVIDER=gemini
GEMINI_API_KEY=Google_AI_Studio에서_받은_키
GEMINI_MODEL=gemini-3.5-flash-lite
```

Gemini에는 기사 제목과 프로그램이 미리 고른 핵심 문장 최대 2개만 보내 한국어로 번역합니다. 수집·주제 선별·우선순위·예약 발행은 프로그램이 처리합니다. 기사 한 건의 번역을 한 요청으로 묶고, 번역 결과는 공급자와 모델별로 캐시합니다. 한국어 원문은 그대로 사용합니다.

기본 모델의 Standard API는 무료 등급을 제공하지만, **무료로 발급받은 키라고 모든 호출이 무료인 것은 아닙니다.** 실제 요금은 키가 속한 프로젝트의 결제 등급에 따릅니다. [Google 공식 요금표](https://ai.google.dev/gemini-api/docs/pricing), [결제 안내](https://ai.google.dev/gemini-api/docs/billing)를 확인하세요. 사용 한도는 프로젝트·모델별로 다르며 [AI Studio에서 현재 한도를 확인](https://ai.google.dev/gemini-api/docs/rate-limits)할 수 있습니다. 인증·할당량·통신 오류가 발생하면 해당 회차의 추가 API 호출을 중단합니다. 프로그램이 결제를 활성화하거나 다른 모델로 자동 전환하지는 않습니다.

### 로컬 번역

텔레그램 봇 토큰만으로 운영하려면 기본값인 `local`을 사용합니다. 별도 AI API 키가 필요하지 않습니다.

```dotenv
TRANSLATION_PROVIDER=local
LOCAL_MODEL_PATH=data/models/m2m100
```

```bash
# 영어 → 한국어 번역 모델 설치: 최초 한 번 다운로드
python -m compdesign_bot setup-translator
```

`setup-translator`는 [Argos가 패키징한 Meta M2M100 418M 모델](https://data.argosopentech.com/argospm/v2/translate-fairseq_m2m_100_418M.argosmodel)을 내려받아 기본 `data/models/m2m100`에 설치합니다. 다운로드는 약 **437 MiB**, 설치 후 모델 파일은 약 **494 MB**입니다. 로컬 번역을 선택했다면 실행 전에 설치하세요. `run`은 모델을 자동으로 내려받지 않습니다.

설치한 모델을 CPU에서 실행하며, 번역할 때 외부 AI API를 호출하지 않습니다. 뉴스 수집과 텔레그램 통신에는 인터넷 연결이 필요합니다. 원본 모델은 [Meta M2M100 418M](https://huggingface.co/facebook/m2m100_418M)이며 MIT 라이선스입니다. 배포 모델과 실행 환경은 [Argos 안내](https://www.argosopentech.com/), [CTranslate2 문서](https://opennmt.net/CTranslate2/installation.html)를 참고하세요.

### 번역 미리보기

선택한 방식으로 실제 번역이 되는지 확인합니다. Gemini 모드에서는 이 명령이 API를 호출하며, 텔레그램에는 게시하지 않습니다.

```bash
# 실제 피드의 제목·발췌를 선별하고 한국어로 번역해 출력
python -m compdesign_bot preview
```

## 봇 토큰 연결

새 봇을 만들 경우 텔레그램 공식 [@BotFather](https://t.me/BotFather)에서 `/newbot`으로 토큰을 발급받습니다. 기존 봇을 운영한다면 그 봇의 토큰을 사용하세요. [Telegram 공식 안내](https://core.telegram.org/bots/features#botfather)

로컬 `.env`에 토큰을 입력합니다. 토큰 값은 채팅·스크린샷·저장소에 올리지 마세요.

```dotenv
TELEGRAM_BOT_TOKEN=BotFather에서_받은_토큰
# 개인 대화 기능만 사용할 때는 비워 두어도 됩니다.
TELEGRAM_CHANNEL_ID=
```

`.env` 권한은 설치 스크립트에서 `600`으로 설정합니다. 수동으로 만들었다면 `chmod 600 .env`를 실행하세요.

다른 봇의 토큰으로 교체했다면 실행 중인 봇을 재시작하세요. 새 계정에 맞춰 메시지 수신 위치를 초기화하며, 기존 채널 발행 기록과 번역 모델은 유지합니다. 같은 봇의 토큰을 재발급받은 경우에는 수신 위치도 유지됩니다.

```bash
# 번역 설정과 Telegram 연결 확인
python -m compdesign_bot doctor

# 봇 이름·소개·명령어 메뉴를 실제로 설정
python -m compdesign_bot configure-bot

# 개인 대화 응답 시작, 채널이 설정되어 있으면 예약 게시도 시작
python -m compdesign_bot run
```

봇과의 개인 대화에서 사용할 수 있는 명령어입니다.

| 명령어 | 동작 |
| --- | --- |
| `/start` | 정보봇 소개와 이용 방법 |
| `/help` | 명령어 안내 |
| `/latest` | 한국어 브리핑 최대 3건 |
| `/sources` | 수집원 안내 |

`/latest`는 최근 결과를 최대 1시간 캐시해 사용합니다. 같은 대화의 요청 간격은 최소 60초이며, 새 피드 수집에도 공통 대기 시간이 있어 연속 요청이 새 수집으로 이어지지는 않습니다. 자유 대화형 질의응답 기능은 포함하지 않습니다.

## 자동 게시 채널 연결

1. 텔레그램 앱에서 새 채널을 만듭니다. 이름·설명·고정 게시물 문구는 [채널 운영 안내](docs/channel.md)에 준비되어 있습니다. 채널 생성 API는 사용자 계정 전용이므로 봇 토큰으로 채널을 만들 수는 없습니다. [공식 문서](https://core.telegram.org/method/channels.createChannel)
2. [채널에 봇 추가하기](https://t.me/ComputationalDesign_bot?startchannel&admin=post_messages)를 열어 대상 채널을 선택하고 **메시지 게시 / Post Messages** 권한을 부여합니다. 직접 추가하려면 채널의 **관리자 → 관리자 추가**에서 `@ComputationalDesign_bot`을 검색하세요. 다른 봇 토큰을 사용했다면 해당 봇을 추가하세요.
3. 공개 채널은 실제 사용자 이름을 `.env`에 입력합니다. `t.me/compute_design_kr`라면 `TELEGRAM_CHANNEL_ID=@compute_design_kr`입니다. 예시 이름의 사용 가능 여부는 앱에서 확인하세요.
4. 비공개 채널이라면 봇 추가 후 채널에 짧은 글을 직접 게시하고 아래 명령에서 채널 ID를 확인합니다.

```bash
python -m compdesign_bot discover-channel
```

표시된 대상 채널의 `-100`으로 시작하는 숫자 ID를 `.env`의 `TELEGRAM_CHANNEL_ID`에 입력하세요. 이 명령은 기록된 채널 정보나 최근 업데이트를 읽으며, 채널에 글을 게시하지 않습니다. 주소 형식은 [Telegram sendMessage 문서](https://core.telegram.org/bots/api#sendmessage)에도 나와 있습니다.

```bash
# 대상 채널과 게시 권한 확인
python -m compdesign_bot doctor

# 기존 채널의 이름·설명 변경이 필요할 때만 실행
python -m compdesign_bot configure-channel

# 즉시 한 차례 실제 게시
python -m compdesign_bot publish
```

`configure-channel`은 봇에 채널 정보 변경 권한도 있어야 사용할 수 있습니다. `.env`를 변경했다면 실행 중인 `run` 프로세스를 재시작하세요. 채널이 연결된 `run`은 개인 대화 응답과 예약 게시를 함께 처리합니다.

예약 시각부터 5분 안에 게시를 시도하며, 관련성 있는 새 소식이 없으면 쉬어 갑니다. 컴퓨터가 꺼지거나 잠들면 봇도 멈춥니다. 지난 회차를 한꺼번에 발행하지 않으므로 즉시 게시하려면 `publish`를 실행하세요. 봇 하나에는 수신 프로세스 하나만 실행해야 합니다. 기존 웹훅이 설정된 봇은 현재 운영 설정을 먼저 정리하거나 전용 봇을 사용하세요.

## 무엇을 어떻게 선별하나요?

Web3·블록체인과 컴퓨테이셔널 디자인의 접점이 최우선입니다. Web3·AI·디자인이 겹치는 소식, AI·컴퓨테이셔널 디자인, 일반 컴퓨테이셔널 디자인을 이어서 다룹니다. 일반 코인 시황·가격 예측·에어드롭 홍보는 제외하는 방향으로 필터링합니다.

설정된 RSS/Atom 피드의 제목과 발췌에서 핵심 문장을 고르고, 선택한 번역기로 영어를 한국어로 번역합니다. **번역에 오역이 있거나 발췌에서 맥락이 빠질 수 있습니다.** 원문에 없는 숫자·주소, 관련 없는 안내 문구, 과도한 반복 등 이상 징후를 검사합니다. 검사를 통과하지 못한 번역은 게시하지 않습니다. 이 검사가 번역 정확성을 보장하지는 않습니다.

원문 전체를 읽고 분석하거나 사실을 독립적으로 검증하는 기능은 없습니다. 날짜가 없거나 너무 오래된 항목은 게시 후보에서 제외합니다. 제목만 확보하거나 제공하는 소식은 근거 범위를 표시합니다. 수집원과 추가 방법은 [수집원 안내](docs/sources.md)를 참고하세요.

## 터미널 명령어

| 명령어 | 동작 |
| --- | --- |
| `demo` | 고정 예시 출력 |
| `setup-translator` | 로컬 번역 모델 다운로드·설치 |
| `collect` / `collect --json` | 실제 피드 수집 및 후보 확인, 번역·게시 없음 |
| `preview` | 선택한 번역기로 한국어 브리핑 미리보기, 게시 없음 |
| `doctor` | 번역 설정·Telegram 연결 확인 |
| `configure-bot` | 봇 이름·소개·명령어 메뉴 변경 |
| `discover-channel` | 채널 ID 확인 |
| `configure-channel` | 기존 채널 이름·설명 변경 |
| `publish` | 신규 후보를 한 차례 실제 게시 |
| `listen` | 개인 대화 명령어 응답만 실행 |
| `run` | 개인 대화 응답 + 연결된 채널 예약 게시 |
| `status` | 로컬 발송 기록 확인 |
| `resolve-delivery ID --sent` | 채널 확인 후 불확실한 발송을 완료 처리 |
| `resolve-delivery ID --retry` | 채널 확인 후 재시도 허용 |

전체 옵션은 `python -m compdesign_bot --help`로 확인하세요. 발송 기록은 SQLite에 저장합니다. 전송 응답을 받지 못해 성공 여부가 불확실하면 자동 재전송하지 않습니다. `status`에 표시된 ID와 실제 채널 게시 여부를 확인한 뒤 `resolve-delivery`로 처리하세요. 기록을 삭제하면 기존 글이 다시 게시될 수 있습니다.

## 설정

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | 비어 있음 | BotFather에서 발급한 토큰 |
| `TELEGRAM_CHANNEL_ID` | 비어 있음 | 채널 예약 게시를 사용할 때 입력 |
| `TRANSLATION_PROVIDER` | `local` | 번역 방식: `local` 또는 `gemini` |
| `GEMINI_API_KEY` | 비어 있음 | Gemini 번역을 선택했을 때 필요한 Google AI Studio 키 |
| `GEMINI_MODEL` | `gemini-3.5-flash-lite` | Gemini 번역 모델 |
| `LOCAL_MODEL_PATH` | `data/models/m2m100` | 로컬 번역을 선택했을 때 모델 경로 |
| `CHANNEL_NAME` | `컴퓨트 디자인 브리핑 \| Web3 · AI` | 채널 설정 명령에서 사용할 이름 |
| `TIMEZONE` | `Asia/Seoul` | 발행 시간대 |
| `POST_TIMES` | `09:00,18:00` | 발행 시각, 쉼표로 구분 |
| `MAX_POSTS` | `5` | 회당 최대 게시 수, 1~10 |
| `MAX_AGE_DAYS` | `7` | 게시 후보 최대 나이, 1~30일 |
| `MAX_CANDIDATES` | `15` | 회당 검토할 최대 후보 수, 1~30 |
| `SOURCES_FILE` | `config/sources.json` | 수집원 설정 경로 |
| `DATABASE_PATH` | `data/bot.sqlite3` | 발송 기록 경로 |

기존 환경변수가 `.env`보다 우선합니다. 수신 진행 위치와 최근 채널 정보는 기본 `data/bot.updates.json`에 저장합니다. 이 기록에는 채널 식별 정보가 포함되므로 `data/`는 공개하지 마세요.

## Docker로 계속 운영하기

Docker와 Docker Compose가 설치된 서버에서 `.env`에 봇 토큰과 번역 방식을 먼저 설정하세요. 아래 구성은 `config/`를 읽기 전용으로 연결하고, **번역 캐시·발송 기록·수신 진행 위치·설치한 로컬 모델**을 `bot-data` 볼륨에 보존합니다. Docker에서는 경로 설정 기본값을 유지하세요. 컨테이너는 **메모리 1,536 MiB·CPU 2개 분량**으로 제한합니다. 로컬 모델 실행 과정에서 약 945 MB의 메모리를 사용한 측정값이 있습니다. Gemini 모드에서는 로컬 모델을 로드하지 않습니다.

```bash
docker compose build

# local 모드만: Docker 볼륨에 번역 모델 설치, Gemini는 이 줄을 생략
docker compose run --rm bot python -m compdesign_bot setup-translator
docker compose run --rm bot python -m compdesign_bot doctor
docker compose run --rm bot python -m compdesign_bot preview

# 봇 이름·소개·명령어 메뉴 설정
docker compose run --rm bot python -m compdesign_bot configure-bot

# 개인 대화 수신, 채널이 연결되어 있으면 예약 게시도 시작
docker compose up -d
docker compose logs --tail=100 -f bot
```

로컬 번역을 사용한다면 호스트에 이미 모델을 설치했어도 Docker 볼륨 안에 별도로 설치해야 합니다. Gemini 번역에는 이 과정이 필요하지 않습니다. `docker compose down`은 봇을 중지하고 데이터를 보존합니다. `.env`를 변경했다면 `docker compose up -d --force-recreate`로 반영하세요. `docker compose down -v`는 모델과 발송 기록 볼륨까지 삭제합니다.

## 개발 확인

```bash
python -m pip install -e '.[local,dev]'
python -m pytest
python -m ruff check .
```

자동 테스트는 외부 응답과 번역기를 대체해 검사합니다. 운영 환경에서는 번역 방식을 설정한 뒤 `preview`로 실제 번역 결과를, `doctor`로 설정과 텔레그램 연결을 확인하세요. `publish`는 실제 채널 게시입니다.
