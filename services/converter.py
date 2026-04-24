from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PROMPT_TEMPLATE = """Ты перерабатываешь markdown-страницы документации Engee по блокам библиотеки.
Нужно упростить описание блока из документации, сохранив только технически полезную информацию, чтобы итоговый текст было проще читать и потом использовать для автоматической обработки.

Контекст:
- входной текст обычно содержит название блока, тип блока, путь в библиотеке, описание, порты, параметры, ограничения, примечания и иногда большие примеры;
- текст может быть перегружен повторениями, длинными пояснениями, деталями редактора, длинными примерами кода и служебной markdown-разметкой.

Главная цель:
- сохранить суть блока и все важные технические сведения;
- убрать шум и сделать структуру более стабильной;
- оставить только ту информацию, которая реально помогает понять блок и его настройку.

Сохраняй обязательно:
- название блока;
- тип блока;
- путь в библиотеке, если он указан;
- краткое назначение блока;
- принцип работы блока;
- входные и выходные порты, их назначение, допустимые формы сигналов и типы данных;
- параметры блока, их смысл, тип, значения по умолчанию, зависимости и ограничения;
- важные формулы, правила, режимы работы, предупреждения и ограничения;
- особенности, которые влияют на использование блока в модели Engee.

Можно сокращать или убирать:
- повторяющиеся объяснения;
- слишком длинные вводные абзацы;
- служебные anchor-вставки вида [](#...);
- декоративные таблицы, если их смысл можно передать обычным текстом;
- длинные примеры кода, если без них смысл не теряется;
- второстепенные подробности интерфейса, если они не влияют на понимание блока.

Требования к результату:
- сохраняй исходный язык текста;
- не придумывай новую информацию;
- не добавляй разделы, которых невозможно заполнить по исходнику;
- сохраняй markdown;
- делай формулировки короче и яснее;
- если раздел есть в исходнике, по возможности сохрани его в более компактной форме;
- верни только итоговый упрощённый markdown без пояснений от себя.

Предпочтительная структура результата:
- Заголовок с названием блока.
- Краткое назначение.
- Тип блока.
- Путь в библиотеке.
- Описание.
- Порты.
- Параметры.
- Ограничения, особенности и важные замечания.

Правила по содержанию:
- если блок простой, результат должен быть коротким;
- если блок сложный, оставляй только ключевые технические детали;
- если в исходнике есть большие примеры, оставляй только краткий вывод из них;
- если информации о портах или параметрах нет, не выдумывай её.

Это часть {chunk_index} из {chunk_total}.

Исходный markdown:
{content}
"""

SYSTEM_MESSAGE = "Ты аккуратно упрощаешь документацию по блокам Engee и превращаешь её в компактное, структурированное и технически точное описание блока."
DEFAULT_API_URL = os.getenv(
    "LLM_API_URL",
    "https://openrouter.ai/api/v1/chat/completions",
)
DEFAULT_MODEL = os.getenv("LLM_MODEL", "openai/gpt-4o-mini")
DEFAULT_TIMEOUT_SECONDS = int(os.getenv("LLM_TIMEOUT_SECONDS", "120"))
DEFAULT_MAX_CHARS_PER_CHUNK = int(os.getenv("LLM_MAX_CHARS_PER_CHUNK", "12000"))
DEFAULT_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.2"))
API_KEY = ""


@dataclass(frozen=True)
class ConverterSettings:
    api_url: str
    api_key: str
    model: str
    temperature: float
    timeout_seconds: int
    max_chars_per_chunk: int
    overwrite: bool = False


@dataclass(frozen=True)
class ConverterRunResult:
    total: int
    success: int
    failed: int
    skipped: int


def resolve_api_key(cli_value: str | None = None) -> str:
    return (
        cli_value
        or API_KEY.strip()
        or os.getenv("LLM_API_KEY")
        or os.getenv("OPENROUTER_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    )


def build_converter_settings(
    *,
    api_url: str = DEFAULT_API_URL,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    temperature: float = DEFAULT_TEMPERATURE,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    max_chars_per_chunk: int = DEFAULT_MAX_CHARS_PER_CHUNK,
    overwrite: bool = False,
) -> ConverterSettings:
    resolved_api_key = resolve_api_key(api_key)
    if not resolved_api_key:
        raise ValueError(
            "LLM API key is not set. Pass it explicitly or configure LLM_API_KEY / OPENROUTER_API_KEY / OPENAI_API_KEY."
        )

    return ConverterSettings(
        api_url=api_url,
        api_key=resolved_api_key,
        model=model,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
        max_chars_per_chunk=max_chars_per_chunk,
        overwrite=overwrite,
    )


def iter_markdown_files(input_dir: Path) -> list[Path]:
    return sorted(path for path in input_dir.glob("*.md") if path.is_file())


def read_text_with_fallbacks(path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "cp1251"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("utf-8", b"", 0, 1, f"Cannot decode file {path}")


def pack_parts(parts: Sequence[str], max_chars: int, separator: str) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    separator_length = len(separator)

    for raw_part in parts:
        part = raw_part.strip()
        if not part:
            continue

        part_length = len(part)
        projected = part_length if not current else current_length + separator_length + part_length

        if current and projected > max_chars:
            chunks.append(separator.join(current).strip())
            current = [part]
            current_length = part_length
            continue

        current.append(part)
        current_length = projected

    if current:
        chunks.append(separator.join(current).strip())

    return chunks


def split_large_block(block: str, max_chars: int) -> list[str]:
    if len(block) <= max_chars:
        return [block.strip()]

    paragraphs = [paragraph for paragraph in re.split(r"\n{2,}", block) if paragraph.strip()]
    if len(paragraphs) > 1:
        paragraph_chunks = pack_parts(paragraphs, max_chars, "\n\n")
        if paragraph_chunks:
            return paragraph_chunks

    lines = [line for line in block.splitlines() if line.strip()]
    if len(lines) > 1:
        line_chunks = pack_parts(lines, max_chars, "\n")
        if line_chunks:
            return line_chunks

    return [
        block[index : index + max_chars].strip()
        for index in range(0, len(block), max_chars)
        if block[index : index + max_chars].strip()
    ]


def split_markdown(text: str, max_chars: int) -> list[str]:
    normalized = text.replace("\r\n", "\n").strip()
    if not normalized:
        return []

    if len(normalized) <= max_chars:
        return [normalized]

    sections = [section for section in re.split(r"(?m)(?=^#{1,6}\s)", normalized) if section.strip()]
    if not sections:
        sections = [normalized]

    prepared_sections: list[str] = []
    for section in sections:
        prepared_sections.extend(split_large_block(section, max_chars))

    chunks = pack_parts(prepared_sections, max_chars, "\n\n")
    if chunks:
        return chunks

    return split_large_block(normalized, max_chars)


def build_headers(api_url: str, api_key: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if "openrouter.ai" in api_url:
        referer = os.getenv("OPENROUTER_SITE_URL")
        title = os.getenv("OPENROUTER_APP_NAME")
        if referer:
            headers["HTTP-Referer"] = referer
        if title:
            headers["X-Title"] = title
    return headers


def request_simplified_markdown(
    *,
    chunk: str,
    chunk_index: int,
    chunk_total: int,
    settings: ConverterSettings,
) -> str:
    prompt = PROMPT_TEMPLATE.format(
        chunk_index=chunk_index,
        chunk_total=chunk_total,
        content=chunk,
    )
    payload = {
        "model": settings.model,
        "temperature": settings.temperature,
        "messages": [
            {"role": "system", "content": SYSTEM_MESSAGE},
            {"role": "user", "content": prompt},
        ],
    }
    request = Request(
        settings.api_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=build_headers(settings.api_url, settings.api_key),
        method="POST",
    )

    try:
        with urlopen(request, timeout=settings.timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {error_body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Connection error: {exc.reason}") from exc

    data = json.loads(body)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected API response: {body}") from exc

    if isinstance(content, list):
        text_parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") in {None, "text"}
        ]
        content = "".join(text_parts)

    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"Model returned an empty response: {body}")

    return content.strip()


def ensure_parent_directory(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def process_file(source_path: Path, target_path: Path, settings: ConverterSettings) -> None:
    source_text = read_text_with_fallbacks(source_path)
    chunks = split_markdown(source_text, settings.max_chars_per_chunk)

    if not chunks:
        ensure_parent_directory(target_path)
        target_path.write_text("", encoding="utf-8")
        return

    simplified_chunks: list[str] = []
    for chunk_index, chunk in enumerate(chunks, start=1):
        simplified_chunks.append(
            request_simplified_markdown(
                chunk=chunk,
                chunk_index=chunk_index,
                chunk_total=len(chunks),
                settings=settings,
            )
        )

    ensure_parent_directory(target_path)
    target_text = "\n\n".join(part.strip() for part in simplified_chunks if part.strip()).strip()
    if target_text:
        target_text += "\n"
    target_path.write_text(target_text, encoding="utf-8")


class EngeeDocumentationConverter:
    def __init__(self, run_dir: str | Path, settings: ConverterSettings) -> None:
        self.run_dir = Path(run_dir)
        if not self.run_dir.exists():
            raise FileNotFoundError(f"Run dir `{self.run_dir}` does not exist")

        self.input_dir = self.run_dir / "documentation"
        self.output_dir = self.run_dir / "converted_docs"
        self.settings = settings
        self._callback: Optional[Callable[[Any], None]] = None

    def set_callback(self, callback: Callable[[Any], None]) -> None:
        self._callback = callback

    def get_total_files(self) -> int:
        if not self.input_dir.exists() or not self.input_dir.is_dir():
            return 0
        return len(iter_markdown_files(self.input_dir))

    async def _emit_progress(self, advance: float = 1) -> None:
        if self._callback is None:
            return

        result = self._callback(advance)
        if asyncio.iscoroutine(result):
            await result

    async def convert(self) -> ConverterRunResult:
        if not self.input_dir.exists():
            return ConverterRunResult(total=0, success=0, failed=0, skipped=0)

        if not self.input_dir.is_dir():
            raise NotADirectoryError(f"Input path `{self.input_dir}` is not a directory")

        markdown_files = iter_markdown_files(self.input_dir)
        if not markdown_files:
            return ConverterRunResult(total=0, success=0, failed=0, skipped=0)

        success_count = 0
        failed_count = 0
        skipped_count = 0

        for source_path in markdown_files:
            relative_path = source_path.relative_to(self.input_dir)
            target_path = self.output_dir / relative_path

            if target_path.exists() and not self.settings.overwrite:
                skipped_count += 1
                await self._emit_progress(1)
                continue

            try:
                process_file(source_path, target_path, self.settings)
            except Exception:
                failed_count += 1
                await self._emit_progress(1)
                continue

            success_count += 1
            await self._emit_progress(1)

        return ConverterRunResult(
            total=len(markdown_files),
            success=success_count,
            failed=failed_count,
            skipped=skipped_count,
        )
