import os
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional

from aiohttp.abc import HTTPException
from html_to_markdown import convert_with_visitor
from bs4 import BeautifulSoup, Tag

import requests
import aiohttp
import asyncio
import json
import re


class CustomVisitor:
    """Класс для конвертации html в markdown формат с использованием кастомных правил
    (в данном случае очистка ссылок и пропуск изображений)"""
    def visit_link(self, ctx, href, text, title):
        if (".html" in href) or (".svg" in href):
            return {"type": "custom", "output": f"{text}"}
        else:
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
    status: PageStatus # Success / Failed
    reason: Optional[str]
    metadata: Optional[BlockMetadata]

class EngeeBlockDocumentationDownloader:
    """
    Класс парсера для скачивания документации блоков Engee и конвертации в markdown формат.
    """
    def __init__(self, work_dir: str = ".", max_concurrent_requests: int = 30) -> None:
        if not os.path.exists(work_dir):
            raise FileNotFoundError(f"Work dir `{work_dir}` does not exist`")
        self.work_dir: str = work_dir + "/documentation" + "/"
        if not os.path.exists(self.work_dir):
            os.mkdir(self.work_dir)

        self.max_concurrent_requests = max_concurrent_requests

        self.__links_count: Optional[int] = None
        self.__base_url: str = "https://engee.com/helpcenter/stable/ru-en/"
        self.__blocked_libs: list[str] = ["/interfaces/", "/ritm/"]

    @property
    def get_links_count(self) -> int:
        """
        :return: количество ссылок на страницы блоков документаций
        """
        return self.__links_count

    def get_all_libs(self) -> Optional[list[str]]:
        """
        Парсит все общие библиотеки (первого уровня, самые большие)
        :return: лист с названием библиотек
        """
        libs_list: list[str]
        response = requests.get(self.__base_url + "blocks-library-engee.html")
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, "html.parser")
            articles = soup.find("article", {"class": "doc ru-en"})
            root_ul = articles.find("ul")
            libs_list = [li.find("a").text for li in root_ul.find_all("li", recursive=False)]
            return libs_list
        return None

    @staticmethod
    def extract_article(content: bytes) -> Optional[Tag]:
        soup = BeautifulSoup(content, "html.parser")
        article = soup.find("article", {"class": "doc ru-en"})
        return article

    @staticmethod
    def convert_html_to_md(content: str) -> str:
        """
        Убирает с контента теги рисунков, обрезает текст по блоки с лишней информацией
        :param content: текст страницы
        :return: очищенный текст в md формате
        """
        md_text = convert_with_visitor(content, visitor=CustomVisitor())

        removing_pattern = re.compile(r'\[SVG Image\]\(data:image/svg\+xml;base64,[^)]+\)')
        target_words = ["(#дополнительные-возможности)", "(#примеры)", "(#смотрите-также)"]

        cleaned_text = re.sub(removing_pattern, "", md_text)

        for word in target_words:
            if word in cleaned_text:
                cursor = cleaned_text.rfind(word)
                cleaned_text = cleaned_text[:cursor]

        return cleaned_text

    @staticmethod
    def __get_block_metadata(markdown_text: str) -> Optional[BlockMetadata]:
        """
        Ищет в markdown тексте поля с названием блока и путём в библиотеке.
        :param markdown_text: документация блока в md формате
        :return: датакласс с полями: название класса, путь в библиотеке
        """
        path_pattern = re.compile(r"Путь в библиотеке:<br>\s*(/[^|]+)")
        block_path = re.search(path_pattern, markdown_text)
        if not block_path:
            return None

        block_path = block_path.group(1).rstrip()
        block_name = block_path.split("/")[-1]

        return BlockMetadata(block_name=block_name, block_path=block_path)

    def parse_links(self) -> list[str]:
        """
        Парсит ссылки на докуменатцию блоков с главной страницы библиотеки
        :return: валидные ссылки для запросов
        """
        documentation_links: list[str] = []
        response = requests.get(self.__base_url + "blocks-library-engee.html")
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, "html.parser")
            articles = soup.find_all("article", {"class": "doc ru-en"})
            if articles:
                article = articles[0]
                links = article.find_all("a", {"class": "xref page"})
                for link in links:
                    link = link.get("href")
                    if link:
                        link = self.__base_url + link
                        documentation_links.append(link)
        self.__links_count = len(documentation_links)
        return documentation_links

    def __is_allowed_block(self, body: Tag) -> bool:
        """
        Проверяет, является ли страница - документацией блоков, и содержится ли она в заблокированных библиотеках
        :param body: полученное тело документации, запаршенное bs4
        :return: True, если блок из разрешённой (из незапрещённой) библиотеки; False - иначе
        """
        body = body.text.lower()
        if "путь в библиотеке" in body:
            if not any((blocked_lib in body) for blocked_lib in self.__blocked_libs):
                return True
        return False

    def save_block_docs(self, content: str, metadata: BlockMetadata) -> None:
        """
        Сохраняет текст документации и метаданные в отдельные файлы
        :param content: обработанный текст документации
        :param metadata: метаданные
        """
        file_name = metadata.block_path.replace("/", ".")
        doc_file_path = self.work_dir + file_name + ".md"
        metadata_file_path = self.work_dir + file_name + ".json"

        with open(doc_file_path, "w", encoding="utf-8") as doc_file:
            doc_file.write(content)

        with open(metadata_file_path, "w", encoding="utf-8") as metadata_file:
            metadata_file.write(json.dumps(asdict(metadata)))

    async def process_page(self, session: aiohttp.ClientSession, link: str) -> PageProcessResult:
        try:
            async with session.get(link) as response:
                if response.status == 200:
                    content = await response.content.read()
                    content = self.extract_article(content)

                    if content is None:
                        return PageProcessResult(
                            url=link,
                            status=PageStatus.FAILED,
                            reason="Extract article failed, get None",
                            metadata=None
                        )

                    if not self.__is_allowed_block(content):
                        return PageProcessResult(
                            url=link,
                            status=PageStatus.SKIPPED,
                            reason="Block library is not allowed",
                            metadata=None
                        )

                    content = str(content)
                    clean_md = self.convert_html_to_md(content)
                    metadata = self.__get_block_metadata(clean_md)

                    if metadata is None:
                        return PageProcessResult(
                            url=link,
                            status=PageStatus.FAILED,
                            reason="Metadata extraction failed, get None",
                            metadata=None
                        )

                    self.save_block_docs(clean_md, metadata)

                else:
                    return PageProcessResult(
                        url=link,
                        status=PageStatus.FAILED,
                        reason="Response status code is not 200",
                        metadata=None
                    )

            return PageProcessResult(
                url=link,
                status=PageStatus.SUCCESS,
                reason=None,
                metadata=metadata
            )

        except HTTPException:
            return PageProcessResult(
                url=link,
                status=PageStatus.FAILED,
                reason="Request failed",
                metadata=None
            )

    async def main(self) -> None:
        """Запускает основной процесс (парсит ссылки на страницы и запускает скачивание файлов)"""
        doc_links: list[str] = self.parse_links()
        if not doc_links:
            raise ValueError("No links found")

        aio_connector = aiohttp.TCPConnector(limit=self.max_concurrent_requests)
        async with aiohttp.ClientSession(connector=aio_connector) as session:
            tasks = [
                self.process_page(session, link)
                for link in doc_links
            ]

            for task in asyncio.as_completed(tasks):
                await task




if __name__ == "__main__":
    parser = EngeeBlockDocumentationDownloader()

    # start = perf_counter()

    asyncio.run(parser.main())

    # end = perf_counter()

    # print(f"\nTOOK {end - start} seconds")

