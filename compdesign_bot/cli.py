import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime

import httpx

from .config import Settings
from .feeds import collect_articles, load_sources
from .formatting import CHANNEL_DESCRIPTION, render_post
from .models import Article, RankedArticle, Summary
from .pipeline import run_digest
from .ranking import rank_articles
from .scheduler import serve
from .storage import Store, job_lock
from .telegram import Telegram


def demo():
    print("발행 형식 예시 — 실제 수집 뉴스가 아닙니다.\n")
    article = Article(
        "The Art Blocks Generator",
        "https://docs.artblocks.io/protocol/on-chain-generator/",
        "Art Blocks 공식 문서",
        "",
        None,
    )
    summary = Summary(
        "온체인 생성 예술은 어떻게 화면에 나타날까?",
        (
            "Art Blocks Generator는 작가의 코드와 토큰 해시, 의존성 라이브러리를 HTML로 조합합니다.",
            "블록체인에 저장된 데이터를 바탕으로 작품을 브라우저에서 표시합니다.",
        ),
        "생성 예술의 재현성과 코드 보존 방식을 살펴볼 수 있습니다.",
        "형식 예시 · 공식 문서 참고",
    )
    print(render_post(RankedArticle(article, 1, 100, "Web3 × 디자인"), summary))


async def collect(settings: Settings, as_json: bool):
    async with httpx.AsyncClient(follow_redirects=False) as client:
        report = await collect_articles(load_sources(settings.sources_file), client)
    ranked = rank_articles(report.articles, max_age_days=settings.max_age_days)
    items = [
        {
            "priority": r.priority,
            "category": r.category,
            "score": r.score,
            "title": r.article.title,
            "url": r.article.url,
            "source": r.article.source,
            "published_at": r.article.published_at.isoformat() if r.article.published_at else None,
            "evidence": "RSS 발췌" if r.article.summary else "제목만 확인",
        }
        for r in ranked[: settings.max_candidates]
    ]
    if as_json:
        print(
            json.dumps(
                {
                    "collected_at": datetime.now(UTC).isoformat(),
                    "fetched": len(report.articles),
                    "ranked": len(ranked),
                    "errors": report.errors,
                    "candidates": items,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"수집 {len(report.articles)}건 / 관련 후보 {len(ranked)}건 (최근 {settings.max_age_days}일)")
        for item in items:
            print(
                f"\n[{item['category']}] {item['title']}\n{item['source']} · {item['evidence']}\n{item['url']}"
            )
        for error in report.errors:
            print(f"피드 오류: {error}", file=sys.stderr)
    if not report.articles and report.errors:
        raise RuntimeError("피드를 수집하지 못했습니다.")


async def doctor(settings: Settings):
    settings.require_token()
    sources = load_sources(settings.sources_file)
    print(f"활성 소스: {sum(s.enabled for s in sources)}개")
    print(f"예약: {', '.join(t.strftime('%H:%M') for t in settings.post_times)} ({settings.timezone})")
    async with httpx.AsyncClient() as client:
        bot = Telegram(client, settings.bot_token, settings.channel_id)
        me = await bot.call("getMe")
        print(f"봇 연결 정상: @{me['username']}")
        if settings.channel_id:
            access = await bot.check_access()
        else:
            access = None
    if access:
        print(f"텔레그램 연결 정상: @{access['bot']} → {access['channel']} ({access['id']})")
    else:
        print("채널 미연결: 봇 개인 대화는 이용 가능합니다. 채널 발행에는 관리자 추가가 필요합니다.")
    settings.require_summary()
    print("로컬 한국어 번역 모델 정상. 외부 AI API를 사용하지 않습니다.")


async def configure_bot(settings: Settings):
    settings.require_token()
    description = (
        "Web3·블록체인과 컴퓨테이셔널 디자인의 접점을 먼저 전합니다. "
        "온체인 생성 예술, AI 디자인 도구, 파라메트릭 디자인, 크리에이티브 코딩 소식을 "
        "한국어 발췌·번역과 원문 링크로 확인하세요.\n\n"
        "/latest 최신 브리핑\n/sources 정보 출처\n/help 이용 안내"
    )
    commands = [
        {"command": command, "description": text}
        for command, text in (
            ("start", "컴퓨트 디자인 브리핑 시작"),
            ("latest", "최신 한국어 디자인 브리핑"),
            ("sources", "정보 출처 확인"),
            ("help", "이용 안내와 채널 연결 방법"),
        )
    ]
    async with httpx.AsyncClient() as client:
        bot = Telegram(client, settings.bot_token, settings.channel_id)
        me = await bot.call("getMe")
        for language in ("", "ko"):
            await bot.call("setMyName", name="컴퓨트 디자인 브리핑 | Web3 · AI", language_code=language)
            await bot.call("setMyDescription", description=description, language_code=language)
            await bot.call(
                "setMyShortDescription",
                short_description="Web3·AI·컴퓨테이셔널 디자인의 핵심을 한국어로. 생성 예술·크리에이티브 코딩 소식과 원문 링크.",
                language_code=language,
            )
            await bot.call(
                "setMyCommands",
                commands=commands,
                language_code=language,
                scope={"type": "all_private_chats"},
            )
    print(f"봇 이름·소개·명령 설정 완료: https://t.me/{me['username']}")


async def discover_channel(settings: Settings):
    from .bot_runtime import listener_lock, read_listener_state

    if not settings.bot_token:
        raise ValueError(".env에 TELEGRAM_BOT_TOKEN을 먼저 입력하세요.")
    known = read_listener_state(settings.database_path).get("channels", [])
    channels = {chat["id"]: chat.get("title", "") for chat in known}
    if channels:
        for channel_id, title in channels.items():
            print(f"{title}: TELEGRAM_CHANNEL_ID={channel_id}")
        return
    with listener_lock(settings.database_path):
        async with httpx.AsyncClient() as client:
            bot = Telegram(client, settings.bot_token, "")
            info = await bot.call("getWebhookInfo")
            if info.get("url"):
                raise ValueError(
                    "이 봇에는 웹훅이 설정되어 있습니다. 새 전용 봇을 사용하거나 기존 운영 설정을 확인하세요."
                )
            updates = await bot.call("getUpdates", timeout=0)
    for update in updates:
        event = update.get("channel_post") or update.get("my_chat_member") or {}
        chat = event.get("chat", {})
        if chat.get("type") == "channel":
            channels[chat["id"]] = chat.get("title", "")
    for channel_id, title in channels.items():
        print(f"{title}: TELEGRAM_CHANNEL_ID={channel_id}")
    if not channels:
        print("채널 기록이 없습니다. 봇을 관리자로 추가하고 채널에 새 글을 쓴 뒤 다시 실행하세요.")


async def dispatch(args, settings: Settings):
    if args.command == "collect":
        await collect(settings, args.json)
    elif args.command == "doctor":
        await doctor(settings)
    elif args.command == "discover-channel":
        await discover_channel(settings)
    elif args.command == "configure-bot":
        await configure_bot(settings)
    elif args.command == "setup-translator":
        from .local_summary import setup_model

        await asyncio.to_thread(setup_model, settings.local_model_path)
        print("로컬 영어→한국어 번역 모델 설치 완료. 외부 AI API 키가 필요하지 않습니다.")
    elif args.command == "configure-channel":
        settings.require_telegram()
        async with httpx.AsyncClient() as client:
            bot = Telegram(client, settings.bot_token, settings.channel_id)
            await bot.check_access()
            await bot.call("setChatTitle", chat_id=settings.channel_id, title=settings.channel_name)
            await bot.call("setChatDescription", chat_id=settings.channel_id, description=CHANNEL_DESCRIPTION)
        print(f"채널 이름·소개 설정 완료: {settings.channel_name}")
    elif args.command in ("run", "listen"):
        from .bot_runtime import listen

        settings.require_token()
        if args.command == "run" and settings.channel_id:
            await asyncio.gather(listen(settings), serve(settings))
        else:
            print("봇 개인 대화 응답을 시작합니다. 채널 발행은 채널 연결 후 활성화됩니다.", flush=True)
            await listen(settings)
    elif args.command in ("preview", "publish"):
        result = await run_digest(settings, publish=args.command == "publish")
        if args.command == "preview":
            print("미리보기 — 텔레그램에는 보내지 않았습니다.\n")
            for post in result.messages:
                print(post + "\n\n")
        print(
            f"수집 {result.fetched}건 / 후보 {result.ranked}건 / "
            f"{'발행' if args.command == 'publish' else '요약'} {len(result.messages)}건"
        )
        if not result.messages and not result.failed:
            print("새로 제공할 소식이 없습니다.")
        if result.failed:
            raise RuntimeError(f"{result.failed}건을 요약·서식 오류로 건너뛰었습니다.")
    elif args.command in ("status", "resolve-delivery"):
        with job_lock(settings.database_path):
            store = Store(settings.database_path)
            try:
                if args.command == "status":
                    print(
                        json.dumps(
                            {"counts": store.status_counts(), "unresolved": store.unresolved()},
                            ensure_ascii=False,
                            indent=2,
                        )
                    )
                else:
                    if args.id not in {row["id"] for row in store.unresolved()}:
                        raise ValueError("해당 ID의 미확인 전송 기록이 없습니다.")
                    if args.retry:
                        store.release(args.id)
                        print("재발행 대기 대상으로 돌렸습니다. 다음 publish 또는 예약 실행에서 처리합니다.")
                    else:
                        store.sent(args.id)
                        print("발행 완료로 기록했습니다.")
            finally:
                store.close()


def main():
    parser = argparse.ArgumentParser(description="Web3·AI 컴퓨테이셔널 디자인 한국어 브리핑")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("demo", help="키 없이 발행 형식 예시 보기")
    p = sub.add_parser("collect", help="RSS 수집·순위 확인 (AI 호출·발행 없음)")
    p.add_argument("--json", action="store_true")
    sub.add_parser("preview", help="로컬 한국어 발췌·번역 미리보기")
    sub.add_parser("publish", help="지금 채널에 실제 발행")
    sub.add_parser("run", help="봇 대화 응답 및 연결된 채널에 예약 발행")
    sub.add_parser("listen", help="봇 개인 대화 응답 시작 (채널 없이 가능)")
    sub.add_parser("setup-translator", help="무료 로컬 한국어 번역 모델 설치")
    sub.add_parser("configure-bot", help="봇 이름·소개·명령 설정")
    sub.add_parser("doctor", help="설정·텔레그램 권한 확인")
    sub.add_parser("discover-channel", help="봇이 추가된 채널 ID 확인")
    sub.add_parser("configure-channel", help="기존 채널 이름·소개 변경")
    sub.add_parser("status", help="발행 기록과 미확인 전송 보기")
    p = sub.add_parser("resolve-delivery", help="채널 확인 후 미확인 전송 기록 처리")
    p.add_argument("id", type=int)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--retry", action="store_true", help="글이 없음을 확인한 경우 다시 발행 가능하게 변경")
    group.add_argument("--sent", action="store_true", help="글이 있음을 확인한 경우 발행 완료로 표시")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # HTTP request logging embeds Telegram tokens in the request URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        if args.command == "demo":
            demo()
            return
        asyncio.run(dispatch(args, Settings.from_env()))
    except (ValueError, RuntimeError, OSError) as error:
        print(f"오류: {error}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("\n종료했습니다.")
