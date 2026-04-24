import asyncio
import json
import os
import re
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Callable, Optional

import aiohttp
import requests
from bs4 import BeautifulSoup, Tag
from html_to_markdown import convert_with_visitor


class CustomVisitor:
    """Custom rules for converting Engee HTML docs to markdown."""

    def visit_link(self, ctx, href, text, title):
        if (".html" in href) or (".svg" in href):
            return {"type": "custom", "output": f"{text}"}
        return {"type": "continue"}

    def visit_image(self, ctx, src, alt, title):
        return {"type": "skip"}


@dataclass
class BlockMetadata:
    block_name: str
    block_path: str


class PageStatus(Enum):
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass
class PageProcessResult:
    url: str
    status: PageStatus
    reason: Optional[str]
    metadata: Optional[BlockMetadata]


@dataclass
class ParserRunResult:
    total: int
    successes: int
    failures: int
    skipped: int


class EngeeBlockDocumentationDownloader:
    def __init__(
        self,
        work_dir: str = ".",
        max_concurrent_requests: int = 10,
        request_timeout_seconds: int = 60,
        max_request_retries: int = 2,
    ) -> None:
        if not os.path.exists(work_dir):
            raise FileNotFoundError(f"Work dir `{work_dir}` does not exist")

        self.work_dir = os.path.join(work_dir, "documentation")
        if not os.path.exists(self.work_dir):
            os.mkdir(self.work_dir)

        self.max_concurrent_requests = max_concurrent_requests
        self.request_timeout_seconds = request_timeout_seconds
        self.max_request_retries = max_request_retries

        self._callback: Optional[Callable[[Any], None]] = None
        self.__base_url = "https://engee.com/helpcenter/stable/ru-en/"
        self.__allowed_libs: Optional[list[str]] = None

    def set_callback(self, callback: Callable[[Any], None]) -> None:
        self._callback = callback

    def get_all_libs(self) -> Optional[dict[int, str]]:
        libs_list: list[str]
        response = requests.get(self.__base_url + "blocks-library-engee.html", timeout=30)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, "html.parser")
            article = soup.find("article", {"class": "doc ru-en"})
            if article is None:
                return None

            root_ul = article.find("ul")
            if root_ul is None:
                return None

            libs_list = ["/" + li.find("a").text.lower() + "/" for li in root_ul.find_all("li", recursive=False)]

            if "/оборудование/" in libs_list:
                libs_list.remove("/оборудование/")
                libs_list.append("/interfaces/")
            return dict(zip(range(0, len(libs_list)), libs_list))
        return None

    def get_allowed_libs(self) -> Optional[list[str]]:
        return self.__allowed_libs

    def choose_allowed_libs(self, allowed_libs_indexes: list[int]) -> None:
        libs_dict = self.get_all_libs()
        if libs_dict is None:
            raise ValueError("Failed to load libraries list.")

        allowed_libs = [lib for index, lib in libs_dict.items() if index in allowed_libs_indexes]
        if not allowed_libs:
            raise ValueError("Gets 0 allowed libs, nothing to parse.")

        self.__allowed_libs = allowed_libs

    @staticmethod
    def extract_article(content: bytes) -> Optional[Tag]:
        soup = BeautifulSoup(content, "html.parser")
        return soup.find("article", {"class": "doc ru-en"})

    @staticmethod
    def convert_html_to_md(content: str) -> str:
        md_text = convert_with_visitor(content, visitor=CustomVisitor())

        removing_pattern = re.compile(r"\[SVG Image\]\(data:image/svg\+xml;base64,[^)]+\)")
        target_words = [
            "(#дополнительные-возможности)",
            "(#примеры)",
            "(#смотрите-также)",
        ]

        cleaned_text = re.sub(removing_pattern, "", md_text)

        for word in target_words:
            if word in cleaned_text:
                cursor = cleaned_text.rfind(word)
                cleaned_text = cleaned_text[:cursor]

        return cleaned_text

    @staticmethod
    def __get_block_metadata(markdown_text: str) -> Optional[BlockMetadata]:
        path_pattern = re.compile(r"Путь в библиотеке:<br>\s*(/[^|]+)")
        block_path = re.search(path_pattern, markdown_text)
        if not block_path:
            return None

        normalized_block_path = block_path.group(1).rstrip()
        block_name = normalized_block_path.split("/")[-1]

        return BlockMetadata(block_name=block_name, block_path=normalized_block_path)

    def parse_links(self) -> list[str]:
        documentation_links: list[str] = []
        response = requests.get(self.__base_url + "blocks-library-engee.html", timeout=30)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, "html.parser")
            articles = soup.find_all("article", {"class": "doc ru-en"})
            if articles:
                article = articles[0]
                links = article.find_all("a", {"class": "xref page"})
                for link in links:
                    href = link.get("href")
                    if href:
                        documentation_links.append(self.__base_url + href)
        return documentation_links

    def __is_allowed_block(self, body: Tag) -> bool:
        body_text = body.text.lower()
        if "путь в библиотеке" in body_text:
            if self.__allowed_libs is None:
                return True
            return any(lib in body_text for lib in self.__allowed_libs)
        return False

    def save_block_docs(self, content: str, metadata: BlockMetadata) -> None:
        file_name = metadata.block_path.replace("/", ".")
        doc_file_path = os.path.join(self.work_dir, file_name + ".md")
        metadata_file_path = os.path.join(self.work_dir, file_name + ".json")

        with open(doc_file_path, "w", encoding="utf-8") as doc_file:
            doc_file.write(content)

        with open(metadata_file_path, "w", encoding="utf-8") as metadata_file:
            metadata_file.write(json.dumps(asdict(metadata), ensure_ascii=False))

    async def _emit_progress(self, advance: float = 1) -> None:
        if self._callback is None:
            return

        result = self._callback(advance)
        if asyncio.iscoroutine(result):
            await result

    async def process_page(self, session: aiohttp.ClientSession, link: str) -> PageProcessResult:
        attempts_total = self.max_request_retries + 1

        for attempt in range(1, attempts_total + 1):
            try:
                async with session.get(link) as response:
                    if response.status != 200:
                        return PageProcessResult(
                            url=link,
                            status=PageStatus.FAILED,
                            reason=f"Response status code is {response.status}",
                            metadata=None,
                        )

                    content = await response.content.read()
                    article = self.extract_article(content)

                    if article is None:
                        return PageProcessResult(
                            url=link,
                            status=PageStatus.FAILED,
                            reason="Extract article failed, get None",
                            metadata=None,
                        )

                    if not self.__is_allowed_block(article):
                        return PageProcessResult(
                            url=link,
                            status=PageStatus.SKIPPED,
                            reason="Block library is not allowed",
                            metadata=None,
                        )

                    clean_md = self.convert_html_to_md(str(article))
                    metadata = self.__get_block_metadata(clean_md)

                    if metadata is None:
                        return PageProcessResult(
                            url=link,
                            status=PageStatus.FAILED,
                            reason="Metadata extraction failed, get None",
                            metadata=None,
                        )

                    self.save_block_docs(clean_md, metadata)
                    return PageProcessResult(
                        url=link,
                        status=PageStatus.SUCCESS,
                        reason=None,
                        metadata=metadata,
                    )
            except asyncio.TimeoutError:
                if attempt == attempts_total:
                    return PageProcessResult(
                        url=link,
                        status=PageStatus.FAILED,
                        reason=f"Request timed out after {attempts_total} attempts",
                        metadata=None,
                    )
            except aiohttp.ClientError as exc:
                if attempt == attempts_total:
                    return PageProcessResult(
                        url=link,
                        status=PageStatus.FAILED,
                        reason=f"Request failed: {exc.__class__.__name__}: {exc}",
                        metadata=None,
                    )

            await asyncio.sleep(min(attempt, 3))

        return PageProcessResult(
            url=link,
            status=PageStatus.FAILED,
            reason="Request failed for an unknown reason",
            metadata=None,
        )

    async def main(self) -> ParserRunResult:
        doc_links = self.parse_links()
        if not doc_links:
            raise ValueError("No links found")

        successes_cnt = 0
        skipped_cnt = 0
        failed_cnt = 0

        aio_connector = aiohttp.TCPConnector(limit=self.max_concurrent_requests)
        timeout = aiohttp.ClientTimeout(total=self.request_timeout_seconds)
        async with aiohttp.ClientSession(connector=aio_connector, timeout=timeout) as session:
            for batch_start in range(0, len(doc_links), self.max_concurrent_requests):
                batch_links = doc_links[batch_start:batch_start + self.max_concurrent_requests]
                tasks = [
                    asyncio.create_task(self.process_page(session, link))
                    for link in batch_links
                ]

                for task in asyncio.as_completed(tasks):
                    result = await task

                    if result.status == PageStatus.SUCCESS:
                        successes_cnt += 1
                    elif result.status == PageStatus.SKIPPED:
                        skipped_cnt += 1
                    elif result.status == PageStatus.FAILED:
                        failed_cnt += 1

                    await self._emit_progress(1)

        return ParserRunResult(
            total=successes_cnt + skipped_cnt + failed_cnt,
            successes=successes_cnt,
            failures=failed_cnt,
            skipped=skipped_cnt,
        )


if __name__ == "__main__":
    parser = EngeeBlockDocumentationDownloader()
    parser_result = asyncio.run(parser.main())
    print(parser_result)
