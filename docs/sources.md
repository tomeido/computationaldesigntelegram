# 정보 출처와 수집 기준

기본 출처는 `config/sources.json`에서 관리합니다. **2026-09-07 12:39 UTC**에 이 환경에서 기존 9개 피드를 실제 요청했고, 모두 HTTP 200과 RSS 파싱 성공을 확인했습니다. 같은 날 후속 검증으로 CDFAM 공식 피드를 추가하여 총 10개입니다. 정상 응답은 최신 기사가 매일 있다는 뜻이 아닙니다. 아래 발행일은 그때 피드가 제공한 가장 최근 `published`/`pubDate`입니다.

| 출처 | 용도 / 근거 범위 | 수집 건수 | 최근 발행일 (UTC) |
| --- | --- | ---: | --- |
| [Le Random](https://www.lerandom.art/editorial/rss.xml) | 생성예술 작가 인터뷰와 편집부 글. Web3·AI 관련성은 기사별 판정 | 80 / 최대 80 | 2026-09-07 |
| [Right Click Save](https://www.rightclicksave.com/article/rss.xml) | 디지털·온체인 예술, AI 창작 인터뷰와 비평 | 50 | 2026-09-04 |
| [Tezos Spotlight](https://spotlight.tezos.com/feed.xml) | Tezos 생태계 공식 콘텐츠 허브. 생성예술 관련 글만 선별 | 10 | 2026-09-04 |
| [Art Blocks 공식 도메인 검색](https://news.google.com/rss/search?q=site%3Aartblocks.io+%28generative+OR+algorithm+OR+art%29+when%3A60d&hl=en-US&gl=US&ceid=US%3Aen) | Google News 발견용 RSS, **제목만 확보**. Art Blocks가 운영하는 RSS가 아님 | 7 | 2026-09-03 |
| [Rhino 한국어](https://blog.kr.rhino3d.com/feeds/posts/default?alt=rss) | McNeel의 한국어 공지, Grasshopper·파라메트릭 설계·AI 플러그인 | 25 | 2026-08-05 |
| [Rhino 영문](https://blog.rhino3d.com/feeds/posts/default?alt=rss) | McNeel의 제품·설계 워크플로·플러그인 소식 | 25 | 2026-09-03 |
| [Blender Developers](https://code.blender.org/feed/) | Geometry Nodes, 절차적 모델링과 개발 소식 | 10 | 2026-07-30 |
| [Processing Foundation](https://medium.com/feed/processing-foundation) | 재단의 p5.js·창의적 코딩·교육 소식 | 10 | 2026-08-10 |
| [CDFAM](https://cdfam.com/feed/) | 컴퓨테이셔널 디자인 심포지엄 주최자의 AI 설계·시뮬레이션·구조 최적화 발표와 인터뷰 | 10 | 2026-07-11 |
| [Hugging Face](https://huggingface.co/blog/feed.xml) | 모델·도구 개발 글 중 3D·생성 설계·창의적 코딩에 해당하는 것만 선별 | 80 / 최대 80 | 2026-09-03 |

Le Random과 Right Click Save는 플랫폼 공식 발표를 대신하는 출처가 아니라, 자체 인터뷰·비평을 발행하는 예술 매체입니다. Right Click Save의 성격은 [매체 소개](https://www.rightclicksave.com/about), Tezos Spotlight의 운영 범위는 [공식 소개](https://spotlight.tezos.com/welcome-to-spotlight/), Processing Foundation의 발행 주체는 [공식 매체 페이지](https://medium.com/processing-foundation)에서 확인할 수 있습니다. Rhino의 Grasshopper 연계는 [McNeel 공식 홈페이지](https://www.rhino3d.com/)에 안내되어 있습니다.

## Web3 출처를 보완한 이유

- [Art Blocks Journal](https://www.artblocks.io/articles)은 접근 가능하지만 홈페이지에서 RSS 자동 발견 링크를 찾지 못했고 `/rss.xml`은 HTTP 404였습니다. 과거 [공식 Medium 피드](https://medium.com/feed/the-link-art-blocks)는 HTTP 200이어도 마지막 발행이 2022년이어서 기본 출처로 쓰지 않습니다. 대신 공식 도메인으로 검색을 제한한 발견용 피드를 사용합니다. 컬렉션 페이지도 섞일 수 있고 검색 색인 날짜가 원문 발행일과 다를 수 있으므로, 제목 외의 사실을 추정하거나 기사 본문을 확보한 것처럼 요약해서는 안 됩니다.
- [fxhash 공식 홈페이지](https://www.fxhash.xyz/)는 점검 당시 HTTP 402 `DEPLOYMENT_DISABLED`를 반환했습니다. 이 결과만으로 영구 폐쇄 여부를 단정하지 않습니다. 동작하는 공식 RSS를 확인하지 못했으므로 가짜 URL이나 오래된 글을 최신 피드로 등록하지 않았습니다. fxhash 관련 새 인터뷰·분석은 Le Random과 Right Click Save에서 기사별로 선별할 수 있습니다.
- Tezos Spotlight의 `/rss/`와 `/tag/art/rss/`는 HTTP 404였습니다. 페이지의 실제 RSS 링크인 `/feed.xml`을 사용합니다. 일반 DeFi·가격·스테이킹 기사까지 모두 추천하는 출처가 아닙니다.
- [Art Blocks 계약 릴리스 Atom](https://github.com/ArtBlocks/artblocks-contracts/releases.atom)도 HTTP 200을 확인했지만, 항목에 원래 발행일 없이 `updated`만 제공하므로 기본 출처에 넣지 않았습니다. 원래 날짜가 없는 항목을 최신 뉴스로 만드는 것을 피합니다.
- [CDFAM 공식 심포지엄 안내](https://cdfam.com/tokyo-2026/)는 컴퓨테이셔널 디자인·AI·머신러닝의 교차 분야를 명시합니다. 이 주최자의 직접 RSS를 추가해 일반 AI 뉴스와 구별되는 설계 근거를 확보합니다. 9월 7일 점검 당시 최신 피드 글은 7월 11일이므로 기본 7일 필터에서는 제외됩니다. 소식이 적다는 이유로 날짜를 바꾸거나 자동으로 기간을 늘리지 않습니다.

## 수집·추출 정책

- `topic`과 `weight`는 운영자가 출처를 설명하는 메타데이터입니다. 출처 이름이나 분야만으로 Web3·AI·컴퓨테이셔널 디자인 관련성을 부여하지 않습니다. **제목과 피드가 실제 제공한 내용**을 기준으로 후속 선별 단계에서 판단합니다.
- 피드 본문을 새로 웹 스크래핑하지 않습니다. RSS/Atom이 실제 제공하는 `content:encoded` 등 콘텐츠를 짧은 설명보다 우선하고, HTML을 제거해 최대 6,000자를 근거로 전달합니다. 콘텐츠가 없으면 설명을, 둘 다 없으면 제목만 전달합니다. Google News는 `discovery: true`이고 설명을 의도적으로 빈 문자열로 전달합니다.
- 원래 발행일만 사용합니다. 수정일 `updated`를 최신 발행일로 대체하지 않으며, 잘못되었거나 알 수 없는 날짜는 `None`입니다. 오래된 글과 날짜 미상 글의 게시 여부는 후속 선별 단계가 정합니다.
- 요청은 한 번에 최대 4개, 출처당 전체 작업 20초, 압축 해제한 피드 2 MiB, 출처당 앞 80개, 설정 파일 최대 40개 출처로 제한합니다. 개별 출처의 HTTP 오류·타임아웃·잘못된 XML은 다른 출처 수집을 중단시키지 않습니다.
- 추적용 `utm_*`, `fbclid`, `gclid` 등과 URL 앵커를 제거합니다. 의미 있는 쿼리 값은 보존하고, 동일 URL은 한 회차에 한 번만 반환합니다. HTTP(S)가 아닌 기사 링크와 인증정보가 포함된 URL은 제외합니다.
- 여러 매체가 서로 다른 URL로 같은 사건을 다루거나, 동일 글이 한국어·영어로 각각 발행되는 경우까지 의미적으로 중복 제거하지는 않습니다.

## 선별까지 포함한 후속 검증

2026-09-07 **12:42:37 UTC**에 10개 출처에서 307건을 수집했고 오류는 0건이었습니다. 수정된 콘텐츠 추출과 관련성 필터를 함께 적용하면 기본 최근 7일 기준 4건이 남았습니다. 그중 Web3·AI·디자인 교차 항목은 RCS의 [Serpentine 연구 펠로십 기사](https://www.rightclicksave.com/article/serpentine-names-first-cohort-of-fellows-researching-ai-s-effects-on-society)(9월 4일)입니다. 제목에 AI만 있던 기사가 RSS 본문 안의 실제 디자인 도구·NFT 관련 근거로 선별됩니다. 나머지는 9월 3일 생성형 로고 인터뷰, 9월 3일 Rhino·Grasshopper 과정, 9월 2일 Rhino 개발자 모임 안내입니다.

14일 설정을 별도로 점검하면 8월 24일 fxhash 비평과 8월 25일 Grasshopper·BIM·AI 교육 안내가 추가됩니다. 기본 7일은 유지합니다. 새 Web3·AI 기사가 없는 날은 같은 내용을 새 소식처럼 다시 만들거나 무관한 암호화폐 뉴스를 대신 게시하지 않습니다.

## 설정 변경 예시

```json
{
  "name": "내가 검증한 출처",
  "url": "https://example.org/feed.xml",
  "enabled": true,
  "topic": "computational_design",
  "discovery": false
}
```

항상 실제 RSS/Atom 응답과 최근 발행일을 확인하고 추가합니다. HTML 페이지·검색 결과 HTML·로그인 페이지를 피드 주소로 넣으면 정상 기사로 처리하지 않습니다.
