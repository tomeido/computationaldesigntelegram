"""Source sentence extraction and offline English-to-Korean translation.

The fixed Argos-packaged M2M100 model runs through CTranslate2 and SentencePiece, so
neither an API key nor PyTorch is needed. Network access happens only in the
explicit model installation command. Selection adds no inferred article facts.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import re
import shutil
import tempfile
import threading
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Protocol

import httpx

from .errors import SummaryError
from .models import RankedArticle, Summary

DEFAULT_MODEL_PATH = Path("data/models/m2m100")
MODEL_URL = "https://data.argosopentech.com/argospm/v2/translate-fairseq_m2m_100_418M.argosmodel"
MODEL_SHA256 = "8a63b8e9228f985d081dd166e336acde5f9083f1f4d7b39e14f7f2d57e5405b4"
MODEL_FILES = (
    "sentencepiece.model",
    "model/model.bin",
    "model/shared_vocabulary.txt",
    "metadata.json",
)
MODEL_METADATA = {
    "model": "m2m100-418M",
    "from_code": "en",
    "to_code": "ko",
    "archive_sha256": MODEL_SHA256,
    "source": MODEL_URL,
    "original_model": "https://huggingface.co/facebook/m2m100_418M",
    "license": "MIT",
}
CACHE_NAMESPACE = "local-m2m100-en-ko-guarded-v5"
_HANGUL = re.compile(r"[가-힣]")
_TOPICS = re.compile(
    r"computational|generative|algorithm|parametric|procedural|creative coding|"
    r"geometry|blockchain|on.chain|smart contract|web3|\bAI\b|machine learning|"
    r"p5\.js|touchdesigner|grasshopper|생성|알고리즘|컴퓨테이셔널|파라메트릭|"
    r"블록체인|온체인|인공지능|기하|크리에이티브",
    re.IGNORECASE,
)
_USEFUL = re.compile(
    r"open.source|source code|release|research|tool|workshop|tutorial|implement|"
    r"workflow|paper|공개|출시|연구|도구|튜토리얼|워크숍|구현",
    re.IGNORECASE,
)
_BOILERPLATE = re.compile(
    r"^(?:subscribe\b|sign up\b|read more\b|continue reading\b|copyright\b|"
    r"all rights reserved\b|the post .+ appeared first|arxiv:|구독|더 읽기)",
    re.IGNORECASE,
)
_CONTEXT_DEPENDENT = re.compile(
    r"^(?:he|she|it|they|his|her|their|its)\b|"
    r"^(?:this|these|those)\s+(?:is|are|was|were|has|have|allows?|enables?|"
    r"provides?|shows?|means?|can|could|will|would)\b|"
    r"^(?:그는|그녀는|그의|그들의|이것은|이들은|이는)\s",
    re.IGNORECASE,
)
# Each pair describes an announcement/contribution and a useful supporting
# detail. These signals only select existing sentences; they never supply facts.
_KIND_SIGNALS = {
    "paper": (
        re.compile(
            r"\b(?:propose[sd]?|present[sd]?|introduce[sd]?|develop(?:ed|s)?)\b|"
            r"\b(?:method|framework|algorithm|model|system)\s+"
            r"(?:uses?|combines?|learns?|generates?|optimizes?)\b|"
            r"제안|제시|개발|방법론|프레임워크",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:outperform\w*|benchmark\w*|evaluat\w*|experiment\w*|results?|"
            r"accuracy|reduces?|reduced|improv\w*|limitations?|fails?|cannot)\b|"
            r"\blimited to\b|평가|실험|성능|결과|한계|감소|개선",
            re.IGNORECASE,
        ),
    ),
    "funding": (
        re.compile(
            r"\b(?:raises?|raised|secures?|secured|funding|financing|investment|"
            r"invested|grants?)\b|\b(?:seed round|series [a-e]|led by)\b|"
            r"투자|유치|지원금|보조금|선정",
            re.IGNORECASE,
        ),
        re.compile(
            r"[$€£₩]\s*\d|\b\d[\d,.]*\s*(?:million|billion|USD|EUR|KRW)\b|"
            r"\b(?:applications?|eligible|eligibility|deadline|closes?|apply by)\b|"
            r"신청|마감|자격|지원 대상|모집|\d[\d,.]*\s*(?:억|만)\s*원",
            re.IGNORECASE,
        ),
    ),
    "showcase": (
        re.compile(
            r"\b(?:uses?|using|built|combines?|maps?|generates?|renders?|shaders?)\b|"
            r"\b(?:made with|powered by|runs? on)\b|"
            r"사용|활용|구현|제작|결합|생성|렌더링|셰이더",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:source code|open.source|demo|interactive|browser|repository|"
            r"try it|try the|play with)\b|소스 코드|오픈소스|데모|체험|브라우저|저장소",
            re.IGNORECASE,
        ),
    ),
}


class Translator(Protocol):
    def translate(self, text: str) -> str: ...


def check_model(model_path: Path = DEFAULT_MODEL_PATH) -> None:
    """Check prerequisites without loading the model or contacting a service."""
    if any(importlib.util.find_spec(name) is None for name in ("ctranslate2", "sentencepiece")):
        raise SummaryError("로컬 번역 패키지가 없습니다. pip install -e '.[local]'로 설치하세요.")
    try:
        if not all((model_path / name).is_file() for name in MODEL_FILES):
            raise ValueError("missing files")
        metadata = json.loads((model_path / "metadata.json").read_text(encoding="utf-8"))
        if any(
            metadata.get(key) != MODEL_METADATA[key]
            for key in ("from_code", "to_code", "model", "archive_sha256")
        ):
            raise ValueError("wrong language pair")
    except (OSError, ValueError, AttributeError):
        raise SummaryError(
            "영어→한국어 로컬 모델이 없습니다. compdesign setup-translator를 먼저 실행하세요."
        ) from None


def _download_model(destination: Path) -> None:
    digest = hashlib.sha256()
    size = 0
    request = urllib.request.Request(MODEL_URL, headers={"User-Agent": "CompDesignBrief/1.0"})
    with urllib.request.urlopen(request, timeout=30) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            size += len(chunk)
            if size > 500 * 1024 * 1024:
                raise SummaryError("번역 모델 다운로드가 예상 크기를 초과했습니다.")
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != MODEL_SHA256:
        raise SummaryError("번역 모델 체크섬 검증에 실패했습니다. 모델을 설치하지 않았습니다.")


def setup_model(model_path: Path = DEFAULT_MODEL_PATH) -> Path:
    """Download the pinned official model and install only its known data files."""
    model_path = Path(model_path)
    if (model_path / "model/model.bin").is_file():
        check_model(model_path)
        return model_path
    if model_path.exists() and (not model_path.is_dir() or any(model_path.iterdir())):
        raise SummaryError("모델 경로에 다른 파일이 있습니다. LOCAL_MODEL_PATH에 빈 경로를 지정하세요.")
    try:
        model_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".en-ko-", dir=model_path.parent) as staging:
            staging_path = Path(staging)
            archive_path = staging_path / "model.argosmodel"
            _download_model(archive_path)
            unpacked = staging_path / "unpacked"
            with zipfile.ZipFile(archive_path) as archive:
                # Named members only: never extract arbitrary archive paths or code.
                for name in MODEL_FILES[:-1]:
                    target = unpacked / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with (
                        archive.open(f"translate-fairseq_m2m_100_418M/{name}") as source,
                        target.open("wb") as output,
                    ):
                        shutil.copyfileobj(source, output)
            (unpacked / "metadata.json").write_text(json.dumps(MODEL_METADATA), encoding="utf-8")
            check_model(unpacked)
            if model_path.exists():
                model_path.rmdir()  # Only the empty directory checked above can be replaced.
            os.replace(unpacked, model_path)
    except (OSError, ValueError, KeyError, urllib.error.URLError, zipfile.BadZipFile):
        raise SummaryError("로컬 번역 모델 설치에 실패했습니다. 네트워크·저장 공간을 확인하세요.") from None
    return model_path


class OfflineTranslator:
    """Lazy, CPU-only translation using the Argos-packaged Meta M2M100 model."""

    def __init__(self, model_path: Path = DEFAULT_MODEL_PATH):
        self.model_path = model_path
        self._engine = None
        self._tokenizer = None
        self._lock = threading.Lock()

    def translate(self, text: str) -> str:
        with self._lock:
            if self._engine is None:
                check_model(self.model_path)
                try:
                    import ctranslate2
                    import sentencepiece

                    self._tokenizer = sentencepiece.SentencePieceProcessor(
                        model_file=str(self.model_path / "sentencepiece.model")
                    )
                    self._engine = ctranslate2.Translator(
                        str(self.model_path / "model"),
                        device="cpu",
                        compute_type="int8",
                        inter_threads=1,
                        intra_threads=2,
                    )
                except (ImportError, OSError, RuntimeError, ValueError):
                    raise SummaryError(
                        "로컬 번역 모델을 불러오지 못했습니다. 설치 상태를 확인하세요."
                    ) from None
            try:
                tokens = self._tokenizer.encode(text, out_type=str)
                if len(tokens) > 512:
                    raise SummaryError("번역할 문장이 너무 깁니다. 이번 기사는 발행하지 않습니다.")
                result = self._engine.translate_batch(
                    [["__en__", *tokens]],
                    target_prefix=[["__ko__"]],
                    beam_size=4,
                    replace_unknowns=True,
                    length_penalty=1.0,
                    max_input_length=512,
                    max_decoding_length=256,
                )[0]
                return self._tokenizer.decode(result.hypotheses[0][1:]).strip()
            except (RuntimeError, ValueError, IndexError, TypeError):
                raise SummaryError("로컬 번역에 실패했습니다. 이번 기사는 발행하지 않습니다.") from None


def _is_korean(text: str) -> bool:
    # Tool/artist names may remain English, but a single Korean tag must not make
    # a whole English sentence bypass translation.
    hangul = len(_HANGUL.findall(text))
    latin = len(re.findall(r"[A-Za-z]", text))
    return hangul >= 2 and hangul >= latin * 0.3


def _bounded_korean(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    if not _HANGUL.search(text):
        raise SummaryError("한국어 번역 결과를 확인하지 못했습니다. 이번 기사는 발행하지 않습니다.")
    return text


def validate_translation(source: str, translation: str) -> None:
    """Reject detectable additions before truncation can hide warning signs.

    This is a conservative filter, not a guarantee of semantic equivalence.
    In particular, malformed RSS tables can trigger unrelated memorized text in
    translation models. They must never turn into contact details or policies.
    """
    if not _HANGUL.search(translation):
        raise SummaryError("한국어 번역 결과를 확인하지 못했습니다.")
    if len(translation) > max(100, len(source) * 1.6):
        raise SummaryError("번역문이 원문보다 과도하게 길어 발행하지 않습니다.")
    source_numbers = set(re.findall(r"\d+", source))
    # English date names and number words can legitimately become Korean digits.
    for index, month in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    ):
        if re.search(rf"\b{month[:3]}(?:{month[3:]})?\b", source, re.IGNORECASE):
            source_numbers.add(str(index))
    for index, word in enumerate(
        ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")
    ):
        if re.search(rf"\b{word}\b", source, re.IGNORECASE):
            source_numbers.add(str(index))
    if not set(re.findall(r"\d+", translation)).issubset(source_numbers):
        raise SummaryError("원문에 없는 숫자가 번역문에 포함되어 발행하지 않습니다.")
    suspicious = (
        (r"쿠키|개인정보|개인 정보|이용약관|이용 약관", r"cookie|privacy|personal data|terms"),
        (r"사무국|팩스|고객센터|고객 센터|연락처|\bFAX\b", r"secretariat|fax|contact|customer|office"),
        (r"https?://|www\.", r"https?://|www\."),
        (r"리뷰 보기|리뷰를 보|후기 보기|더 읽기|계속 읽기", r"review|read more|continue reading"),
        (r"<unk>|__[a-z]{2}__", r"<unk>|__[a-z]{2}__"),
    )
    for output_pattern, source_pattern in suspicious:
        if re.search(output_pattern, translation, re.IGNORECASE) and not re.search(
            source_pattern, source, re.IGNORECASE
        ):
            raise SummaryError("원문과 무관한 안내 문구가 번역문에 포함되어 발행하지 않습니다.")
    if re.search(r"(\S{2,})(?:\s+\1){2,}", translation):
        raise SummaryError("번역문에 비정상적인 반복이 있어 발행하지 않습니다.")


def _terms_in_korean(source: str, translation: str, *, kind: str = "news") -> str:
    """Normalize established terms only when the corresponding source phrase exists."""
    glossary = (
        (
            r"\bcomputational design\b",
            r"computational\s*(?:디자인|설계)|컴퓨터 디자인",
            "컴퓨테이셔널 디자인",
        ),
        (r"\bparametric\b", r"parametric", "파라메트릭"),
        (r"\bgenerative art\b", r"generative art|유전 예술|창조적 예술|생식 예술", "생성형 예술"),
        (r"\bgenerative design\b", r"generative design|유전 설계|유전자 디자인", "생성형 디자인"),
        (r"\bopen.source\b", r"open.source", "오픈소스"),
        (r"\btoolkit\b", r"toolkit", "툴키트"),
        (
            r"\bmodels? in the wild\b",
            r"야생(?:의|에서의)?\s*모델|자연 상태의 모델|models? in the wild",
            "실제 환경의 모델",
        ),
        (
            r"\bnon[\s‐‑–-]?manifold\b",
            r"비\s*다중|비\s*매니폴드|non[\s‐‑–-]?manifold",
            "비다양체",
        ),
        (
            r"\bnon[\s‐‑–-]?watertight\b",
            (
                r"방수(?:가)?\s*(?:되지\s*않는|되지\s*않은|안\s*되는)|"
                r"비\s*방수|non[\s‐‑–-]?watertight"
            ),
            "밀폐되지 않은",
        ),
        (
            r"\bcomputational framework\b",
            r"컴퓨테이셔널(?:\s*디자인)?\s*프레임워크|컴퓨터\s*프레임워크|computational framework",
            "계산 프레임워크",
        ),
    )
    for source_pattern, translated_pattern, replacement in glossary:
        if re.search(source_pattern, source, re.IGNORECASE):
            translation = re.sub(translated_pattern, replacement, translation, flags=re.IGNORECASE)
    if kind == "paper":
        if re.search(r"\bwe\b", source, re.IGNORECASE):
            translation = re.sub(r"우리는|저희는", "연구진은", translation)
            translation = re.sub(r"우리가|저희가", "연구진이", translation)
        if re.search(r"\bour\b", source, re.IGNORECASE):
            translation = re.sub(r"우리의|저희의", "연구진의", translation)
    return translation


def extract_sentences(title: str, excerpt: str, kind: str = "news") -> list[str]:
    """Choose source sentences, favoring the useful details for each content kind.

    Papers pair a contribution with evidence/limitations, funding pairs an
    announcement with amounts/terms, and showcases pair technique with a demo.
    Missing details are never inferred, and selected text stays in source order.
    """
    normalized_title = re.sub(r"\W+", "", title).casefold()
    candidates: list[tuple[int, str, int, bool, bool]] = []
    seen: set[str] = set()
    signals = _KIND_SIGNALS.get(kind)
    for index, sentence in enumerate(re.split(r"(?<=[.!?。！？])\s+|[\r\n]+", excerpt[:6000])):
        sentence = " ".join(sentence.split())
        normalized = re.sub(r"\W+", "", sentence).casefold()
        if (
            len(sentence) < 15
            or len(sentence) > 320
            or sentence.count(",") >= 6
            or sentence.count("|") >= 2
            or sentence.endswith(("...", "…"))
            or not normalized
            or normalized == normalized_title
            or normalized in seen
            or _BOILERPLATE.search(sentence)
        ):
            continue
        seen.add(normalized)
        score = 3 * len(_TOPICS.findall(sentence)) + 2 * len(_USEFUL.findall(sentence))
        primary = bool(signals and signals[0].search(sentence))
        detail = bool(signals and signals[1].search(sentence))
        context_dependent = bool(_CONTEXT_DEPENDENT.search(sentence))
        if signals:
            # A keyword-heavy introduction must not crowd out an actual result,
            # deadline or explanation of how a project works.
            score = min(score, 12) + 16 * primary + 16 * detail
        if context_dependent:
            score -= 24
        candidates.append(
            (index, sentence, score, primary and not context_dependent, detail and not context_dependent)
        )
    ordered = sorted(candidates, key=lambda item: (-item[2], item[0]))
    if not ordered:
        return []
    first = next((candidate for candidate in ordered if candidate[3]), ordered[0])
    remaining = [candidate for candidate in ordered if candidate[0] != first[0]]
    selected = [first]
    if remaining:
        selected.append(next((candidate for candidate in remaining if candidate[4]), remaining[0]))
    return [candidate[1] for candidate in sorted(selected)]


class LocalSummarizer:
    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        model_path: Path = DEFAULT_MODEL_PATH,
        translator: Translator | None = None,
    ):
        # The optional client keeps the pipeline interface compatible; inference
        # deliberately has no HTTP dependency or remote fallback.
        self.translator = translator if translator is not None else OfflineTranslator(model_path)

    async def summarize(self, item: RankedArticle) -> Summary | None:
        article = item.article
        if not article.title.strip():
            return None
        sentences = extract_sentences(article.title, article.summary, article.kind)
        source_texts = [article.title, *(sentences or [article.title])]

        def translate_all() -> tuple[list[str], bool, bool]:
            translated: dict[str, str | None] = {}
            used_translation = False
            for text in source_texts:
                if text in translated:
                    continue
                if _is_korean(text):
                    translated[text] = text
                else:
                    result = _terms_in_korean(text, self.translator.translate(text), kind=article.kind)
                    try:
                        validate_translation(text, result)
                    except SummaryError:
                        if text == article.title:
                            raise
                        translated[text] = None
                        continue
                    translated[text] = result
                    used_translation = True
            title = translated[article.title]
            facts = [translated[text] for text in sentences if translated[text] is not None]
            return [title, *(facts or [title])], used_translation, bool(facts)

        texts, translated, has_facts = await asyncio.to_thread(translate_all)
        evidence = "RSS 발췌" if has_facts else "제목만 확인 · 원문 확인 필요"
        if translated:
            evidence += "·기계번역 (로컬 영어→한국어)"
        else:
            evidence += " (한국어 원문)"
        return Summary(
            title=_bounded_korean(texts[0], 65),
            bullets=tuple(_bounded_korean(text, 160 if article.kind == "paper" else 120) for text in texts[1:]),
            why="세부 조건과 정확한 표현은 원문을 확인하세요.",
            evidence=evidence,
        )
