# 정보 출처와 수집 기준

기본 구성은 **RSS/Atom 출처 14개와 공식 GitHub 저장소 5개**입니다. 각각 `config/sources.json`과 `config/repositories.json`에서 관리합니다. 수집한 항목 전체를 게시하지 않고, 분야 관련성·발행일·본문 근거를 확인한 후보만 번역합니다. 최근 자료가 부족해도 무관한 소식으로 채우거나 자동으로 기간을 늘리지 않습니다.

## RSS/Atom 출처

| 출처 | 용도와 근거 범위 |
| --- | --- |
| [Le Random](https://www.lerandom.art/editorial/rss.xml) | 생성예술 작가 인터뷰와 편집부 글. Web3·AI 관련성은 기사별 판정 |
| [Right Click Save](https://www.rightclicksave.com/article/rss.xml) | 디지털·온체인 예술, AI 창작 인터뷰와 비평 |
| [Tezos Spotlight](https://spotlight.tezos.com/feed.xml) | Tezos 생태계 공식 콘텐츠 허브. 생성예술·제작 기술 관련 글만 선별 |
| [Art Blocks 공식 도메인 검색](https://news.google.com/rss/search?q=site%3Aartblocks.io+%28generative+OR+algorithm+OR+art%29+when%3A60d&hl=en-US&gl=US&ceid=US%3Aen) | Google News 발견용 RSS. Art Blocks 운영 RSS가 아니며 제목만 확보하므로 자동 발행하지 않음 |
| [Rhino 한국어](https://blog.kr.rhino3d.com/feeds/posts/default?alt=rss) | McNeel의 한국어 공지와 Grasshopper·파라메트릭 설계 도구 |
| [Rhino 영문](https://blog.rhino3d.com/feeds/posts/default?alt=rss) | McNeel의 제품·설계 워크플로·플러그인 소식 |
| [Blender Developers](https://code.blender.org/feed/) | Geometry Nodes, 절차적 모델링과 개발 소식 |
| [Processing Foundation](https://medium.com/feed/processing-foundation) | 재단의 p5.js·창의적 코딩 소식 |
| [CDFAM](https://cdfam.com/feed/) | 컴퓨테이셔널 디자인 심포지엄 주최자의 AI 설계·시뮬레이션·구조 최적화 발표와 인터뷰 |
| [Hugging Face](https://huggingface.co/blog/feed.xml) | 모델·도구 개발 글 중 3D·생성 설계·창의적 코딩에 해당하는 내용 |
| [arXiv 그래픽스 Atom 검색](https://export.arxiv.org/api/query?search_query=cat:cs.GR&sortBy=submittedDate&sortOrder=descending&max_results=40) | 연구자가 등록한 그래픽스 논문 제목·초록. 설계·제작·형상·애니메이션 관련 연구를 선별 |
| [NVIDIA 공식 블로그](https://blogs.nvidia.com/feed/) | 설계 기술과 관련 기업의 투자·인수·협력 발표 후보 |
| [Codrops](https://tympanus.net/codrops/feed/) | 창작자가 설명하는 Three.js·WebGPU·셰이더·인터랙티브 작업과 제작 튜토리얼 |
| [설계·생성예술 투자 동향 검색](https://news.google.com/rss/search?q=%28%22computational+design%22+OR+%22generative+design%22+OR+%22generative+art%22+OR+%223D+modeling%22%29+%28funding+OR+raises+OR+investment+OR+grant+OR+acquisition%29+when%3A60d&hl=en-US&gl=US&ceid=US%3Aen) | Google News 발견용 RSS. 제목만 확보하므로 운영자의 추가 확인 대상이며 자동 발행하지 않음 |

Le Random과 Right Click Save는 자체 인터뷰·비평을 발행하는 예술 매체입니다. 플랫폼의 공식 발표나 기술 검증 결과를 대신하지 않습니다. 기업의 직접 발표에도 자사 홍보 관점이 포함될 수 있습니다.

## 공식 GitHub 릴리스와 추천 도구

| 저장소 | 활용 방향 |
| --- | --- |
| [ArtBlocks/artblocks-contracts](https://github.com/ArtBlocks/artblocks-contracts) | 온체인 생성예술 발행 구조와 스마트컨트랙트 구현 |
| [fxhash/onchfs](https://github.com/fxhash/onchfs) | 웹 기반 생성예술 코드와 파일의 블록체인 보존 |
| [compas-dev/compas](https://github.com/compas-dev/compas) | Python 기하 처리와 Rhino·Grasshopper·Blender 연계 설계 |
| [processing/p5.js](https://github.com/processing/p5.js) | 브라우저 생성예술·인터랙션과 공식 예제 실습 |
| [NVIDIA/warp](https://github.com/NVIDIA/warp) | 물리 시뮬레이션·기하 처리·최적화를 활용한 설계 실험 |

GitHub REST API에서 저장소 정보와 최근 공개 릴리스 최대 5개를 읽습니다. 초안·시험판, 비공개·보관·비활성 저장소는 자동 게시 후보에서 제외합니다. 릴리스의 실제 `published_at`을 사용하며 커밋 날짜·별 개수·저장소 수정일을 새 소식의 날짜로 대체하지 않습니다. 기존 Atom 릴리스 피드는 `updated`만 제공해 사용하지 않았고, 현재는 원래 발행일을 제공하는 API를 사용합니다.

변경 기록 중 제작·계산 작업에 유용한 기능이나 수정 내용이 있어야 품질 기준을 통과합니다. 의존성 갱신·오탈자 수정·기여자 목록만 있는 릴리스는 제외합니다. 번역하는 문장은 실제 릴리스 원문에서 고릅니다. 운영자가 `why`에 작성한 일반적인 도구 활용법은 게시물의 **활용** 항목으로 따로 표시하며, 새 버전에서 생긴 기능으로 소개하지 않습니다.

저장소·공식 문서·예제 링크와 GitHub API의 SPDX 라이선스 표기를 제공합니다. 라이선스가 없거나 식별되지 않으면 **확인 필요**로 표시합니다. 이 표기는 저장소 수준의 정보이며 개별 릴리스·파일·종속성의 모든 이용 조건을 확인한 결과는 아닙니다. 실제 코드 실행이나 운영체제·버전별 호환성 검증은 하지 않습니다.

API 응답은 6시간 캐시하고 별도 GitHub API 키 없이 요청합니다. 오류·사용량 제한에는 대기 시간을 적용합니다. 캐시는 기본 `data/bot.github.json`에 저장되며 발행 기록은 기존 SQLite를 사용합니다. 자동 게시에는 RSS와 동일한 기본 7일 필터와 회당 최대 게시 수가 적용됩니다.

봇 개인 대화의 **`/tools`**는 이 설정 파일의 저장소·활용법·문서·예제를 보여 줍니다. 오래된 도구도 볼 수 있고, AI API나 GitHub API를 호출하지 않습니다. 최신 릴리스가 없는 저장소를 새로운 뉴스처럼 게시하지 않고도 유용한 자료를 찾을 수 있습니다.

`REPOSITORIES_FILE`의 기본값은 `config/repositories.json`입니다. `REPOSITORIES_FILE=`로 비우면 GitHub 릴리스 수집과 `/tools` 목록을 끄고 RSS 수집은 유지합니다. 저장소별 `enabled: false`로 해당 항목만 끌 수도 있습니다.

## 분야와 본문 근거 선별

1. **분야·날짜:** Web3·블록체인과 컴퓨테이셔널 디자인, AI 디자인, 일반 컴퓨테이셔널 디자인 순으로 정렬합니다. Web3와 디자인이 제목이나 같은 원문 문장에서 연결되어야 합니다. 작가 약력의 NFT 언급만으로 우선순위를 부여하지 않습니다. 공식 릴리스에 한해 운영자가 관리하는 저장소 분야 설명으로 관련성을 보완하지만, 실제 변경 기록의 품질 검사는 별도로 통과해야 합니다. 날짜 미상과 기본 최근 7일 범위 밖의 항목은 제외합니다.
2. **본문 근거:** 제목 반복, 본문이 너무 짧은 글, 모음·뉴스레터, 구체적인 구현 설명이 없는 홍보, 등록 안내만 있는 행사·강좌는 자동 발행하지 않습니다. 단순한 관련 키워드나 링크 개수만으로 통과시키지 않습니다.
3. **종류별 확인:** 논문은 초록의 구체적인 방법·평가 설명을, 투자·지원은 실제 발표와 금액·참여사·자격·마감 등의 근거를, 작품은 구현 방법을, 릴리스는 유용한 기능·동작 변화를 확인합니다. Web3 우선순위를 유지하면서 같은 우선순위 안에서 종류를 번갈아 선택합니다.
4. **번역·게시:** 통과한 자료만 핵심 문장 최대 2개와 제목을 번역합니다. 이미 게시한 글은 제외하고, 번역 이상 징후가 있으면 게시를 보류합니다. `/latest`와 채널 자동 게시에 같은 품질 기준을 적용합니다.

이 규칙은 제공된 텍스트의 구체성과 근거 범위를 확인하는 자동 선별입니다. **논문의 과학적 타당성, 실험 재현성, 코드 실행, 투자 가치를 검증하지 않습니다.** arXiv에 수록되었다고 동료심사를 통과한 것으로 표시하지 않으며, 투자 정보에서 매수·매도 추천이나 원문에 없는 수익률을 만들지 않습니다.

`python -m compdesign_bot collect --json`으로 통과 후보와 `excluded`의 제외 이유를 확인할 수 있습니다. 이 명령은 RSS·GitHub에 접근하지만 번역 API 호출이나 텔레그램 게시를 하지 않습니다. 수집 건수와 품질 통과 건수는 서로 다릅니다.

## 수집·링크 추출 정책

- RSS의 `topic`·`weight`는 출처 설명용 메타데이터이며 주제 관련성이나 품질 통과를 보장하지 않습니다. `kind`는 `news`, `paper`, `funding`, `showcase`, `release`입니다. 여러 종류가 섞인 기업 블로그는 `news`를 유지하고 개별 기사에서 분류합니다.
- 웹페이지 전체를 새로 스크래핑하지 않습니다. RSS/Atom의 `content:encoded` 등 본문을 짧은 설명보다 우선하고, HTML을 제거한 최대 6,000자를 사용합니다. 본문이 없으면 설명을 확인하지만 제목만 남는 항목은 자동 발행하지 않습니다. Google News의 `discovery: true` 항목은 설명도 비워 둡니다.
- arXiv는 [공식 검색 API](https://info.arxiv.org/help/api/user-manual.html)의 `cs.GR` 분류를 최초 제출일 순으로 최대 40건 요청하고 제목·초록을 읽습니다. 논문 전체를 읽는 것은 아니며, 수정일 `updated`로 오래된 논문을 새 글로 바꾸지 않습니다.
- RSS 본문·논문 초록·arXiv 저자 코멘트에 실제로 적힌 GitHub 코드·데모·프로젝트·문서 링크를 최대 3개 보존합니다. 코드와 데모를 우선하고 **원문 링크**로 표시합니다. 저자 코멘트는 링크 추출에만 쓰고 번역 본문에 합치지 않습니다. 제목으로 저장소를 추측하거나 저자 공식 코드임을 단정하지 않습니다.
- 링크 추출은 추가 HTTP 요청 없이 수행합니다. 인증정보·사설 호스트·IP 주소·위험한 URL 형식을 제외하고, GitHub 프로필·이슈·PR·탐색 페이지를 코드 저장소로 소개하지 않습니다. 원문 속 링크의 실제 작동 여부와 소유 관계까지 확인하는 기능은 아닙니다.
- 원래 발행일만 사용합니다. RSS 요청은 동시에 최대 4개, 출처당 전체 작업 20초, 압축 해제한 피드 2 MiB, 출처당 앞 80개, 설정 최대 40개 출처로 제한합니다. 한 출처의 실패가 다른 출처 수집을 중단시키지 않습니다.
- 추적용 `utm_*`, `fbclid`, `gclid` 등을 제거하고 의미 있는 쿼리는 보존합니다. URL·제목을 기준으로 중복을 줄이지만, 다른 언어나 제목으로 같은 사건을 다룬 모든 기사를 의미적으로 중복 제거하지는 않습니다.

## 개선 후 수집 점검

2026-09-07 15:47 UTC에 RSS/Atom 14곳과 GitHub 저장소 5곳에서 총 **411건**을 수집했고 수집 오류는 **0건**이었습니다. 최근 7일의 관련 후보 17건 중 본문 기준을 통과한 자료는 12건, 보류한 자료는 5건이었습니다. 이는 발행 건수가 아니라 수집 시점의 후보 수입니다.

Art Blocks 1.4.0의 실제 기능 변경과 LayoutShop 등 연구·제작 자료는 통과했습니다. Rhino 개발자 모임·예정 강좌·발표자 소개는 이미 공개된 강연이나 튜토리얼 근거가 없어 보류했습니다. 결과는 당시 제공된 발췌를 기준으로 하며 글 자체의 가치를 평가한 등급은 아닙니다.

## 과거 점검 기록

다음은 **품질 선별과 GitHub 수집을 추가하기 전인 2026-09-07의 기록**입니다. 현재 피드 상태·수집량·통과 후보 수를 뜻하지 않습니다. 당시에는 행사·강좌나 이력 속 NFT 언급으로 선정된 글도 있어, 현재 기준과 결과가 달라질 수 있습니다.

- 12:42 UTC 무렵 10개 출처에서 307건을 수집했고, 당시 관련성 필터의 최근 7일 후보는 4건이었습니다.
- 15:10 UTC 무렵 14개 출처에서 약 393~394건을 수집했고, 당시 관련성 필터의 최근 7일 후보는 16건이었습니다. 이는 현재의 본문 품질 검사를 통과한 숫자가 아닙니다.
- 당시 Art Blocks 홈페이지의 RSS를 확인하지 못해 공식 도메인 발견 검색을 추가했습니다. 이제 이 검색 결과는 제목만 있는 자동 게시물이 아니라 추가 확인할 자료로 남습니다. Art Blocks의 실제 기술 업데이트는 별도의 공식 GitHub API 수집으로 보완합니다.
- 당시 일부 fxhash·a16z·Autodesk 피드와 홈페이지의 오류·빈 응답을 확인했지만, 이 과거 결과로 현재 서비스의 운영 상태를 판단하지 않습니다.

## 설정 변경 예시

RSS 출처는 실제 RSS/Atom 응답과 발행일을 확인한 뒤 `config/sources.json`에 추가합니다. HTML 페이지·검색 결과 HTML·로그인 페이지를 피드 주소로 넣지 마세요.

```json
{
  "name": "내가 확인한 출처",
  "url": "https://example.org/feed.xml",
  "enabled": true,
  "topic": "computational_design",
  "kind": "news",
  "discovery": false
}
```

GitHub 목록은 저장소 소유자·이름과 활용 근거를 확인해 `config/repositories.json`의 `repositories` 배열에 추가합니다. 최대 10개까지 지원합니다. `why`는 운영자의 활용 설명이며 실제 릴리스 변경 사항을 대신할 수 없습니다.

```json
{
  "owner": "processing",
  "name": "p5.js",
  "why": "브라우저에서 생성예술과 인터랙션을 만들고 공식 예제를 변형하며 실험할 수 있습니다.",
  "topic": "creative coding generative art computational design",
  "docs_url": "https://p5js.org/reference/",
  "example_url": "https://p5js.org/examples/",
  "enabled": true
}
```
