import json

from html_to_markdown import convert_with_visitor
from bs4 import BeautifulSoup
from os.path import exists
from os import mkdir
from time import perf_counter

import requests
import aiohttp
import asyncio
import re


class MyVisitor:
    """Класс для конвертации html в markdown формат с использованием кастомных правил
    (в данном случае очистка ссылок и пропуск изображений)"""
    def visit_link(self, ctx, href, text, title):
        if (".html" in href) or (".svg" in href):
            return {"type": "custom", "output": f"{text}"}
        else:
            return {"type": "continue"}

    def visit_image(self, ctx, src, alt, title):
        return {"type": "skip"}


class EngeeBlockDocumentationDownloader:
    """Класс парсера для скачивания документации блоков Engee и конвертации в markdown формат """
    def __init__(self) -> None:
        self.__base_url: str = "https://engee.com/helpcenter/stable/ru-en/"
        self.__raw_links: list[str] = []
        self.__blocked_libs: list[str] = []
        self.__doc_dir: str = "documentation/"

        if not exists(self.__doc_dir):
            mkdir(self.__doc_dir)

    def set_blocked_libs(self) -> None:
        """Устанавливает типы библиотек, страницы которых не будут устанавливаться (заблокированные библиотеки)"""
        INTERFACES = "/interfaces/"
        RITM = "/ritm/"
        self.__blocked_libs = [INTERFACES, RITM]

    def get_all_libs(self) -> list[str] | None:
        """Получает типы библиотек блоков"""
        response = requests.get(self.__base_url + "blocks-library-engee.html")
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, "html.parser")
            articles = soup.find("article", {"class": "doc ru-en"})
            root_ul = articles.find("ul")
            libs = [li.find("a").text for li in root_ul.find_all("li", recursive=False)]
            return libs

        return None

    @staticmethod
    def pretiffy_data(markdown_text: str) -> str:
        """Убирает лишнюю информацию из текста:
        - часть текста с вставленными изображениями
        - часть текста с примерами"""
        removing_pattern = re.compile(r'\[SVG Image\]\(data:image/svg\+xml;base64,[^)]+\)')

        TARGET_WORDS = ["#дополнительные-возможности", "дополнительные возможности", "#примеры", "примеры", "#смотрите-также", "смотрите также"]

        clean_text = re.sub(removing_pattern, "", markdown_text)

        cursor = -1

        for word in TARGET_WORDS:
            if word in clean_text.lower():
                cursor = clean_text.rfind(word)
                if cursor != -1:
                    return clean_text[:cursor]

        return clean_text

    @staticmethod
    def __get_block_metadata(markdown_text: str) -> dict[str, str]:
        block_name = markdown_text.split("\n")[0].replace("#", "").replace("/", "-").strip()

        path_pattern = re.compile(r"Путь в библиотеке:<br>\s*(/[^|]+)")
        block_path = re.search(path_pattern, markdown_text).group(1)

        metadata = {"block_name": block_name,
                    "block_path": block_path}

        return metadata

    def __save_block_metadata(self, metadata: dict[str, str]) -> None:
        block_path = metadata.get("block_path").replace("/", ".")
        if block_path:
            open(f"{self.__doc_dir + block_path.rstrip()}.json", "w", encoding="utf-8").write(json.dumps(metadata))
            return
        else:
            raise ValueError("'block_path' is not exists in metadata")

    def __save_md(self, body: str) -> bool:
        """Конвертирует файл в md формат и сохраняет в директории"""
        markdown_text = convert_with_visitor(body, visitor=MyVisitor())
        markdown = self.pretiffy_data(markdown_text)

        metadata = self.__get_block_metadata(markdown)
        self.__save_block_metadata(metadata)
        block_path = metadata.get("block_path").replace("/", ".")

        try:
            with open(f"{self.__doc_dir + block_path.rstrip()}.md", "w", encoding="utf-8") as f:
                f.write(markdown)
            return True
        except:
            return False

    def __validate_page(self, body: BeautifulSoup) -> bool:
        """Проверяет, является ли страница - документацией блоков, и содержится ли она в заблокированных библиотеках"""
        body = body.text.lower()
        if "путь в библиотеке" in body:
            if any((blocked_type in body) for blocked_type in self.__blocked_libs):
                return False
            else:
                return True
        return False

    def parse_links(self) -> bool:
        """Парсит ссылки со страницы документации блоков"""
        response = requests.get(self.__base_url + "blocks-library-engee.html")
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, "html.parser")
            articles = soup.find_all("article", {"class": "doc ru-en"})
            if articles:
                article = articles[0]
                links = article.find_all("a", {"class": "xref page"})
                self.__raw_links.extend([link.get("href") for link in links])
                return True

        return False

    async def catch_and_convert(self, session: aiohttp.ClientSession, link: str) -> bool:
        """Парсит страницу блока, вызывает валидацию и сохранение"""
        url: str = self.__base_url + link
        async with session.get(url) as response:
            if response.status == 200:
                content = await response.content.read()
                soup = BeautifulSoup(content, "html.parser")
                article = soup.find("article", {"class": "doc ru-en"})
                if self.__validate_page(article):

                    article = str(article)

                    if self.__save_md(article):
                        return True

            return False

    async def main(self) -> None:
        """Запускает основной процесс (парсит ссылки на страницы и запускает скачивание файлов)"""
        self.parse_links()
        print("LINKS PARSING STARTED...")
        async with aiohttp.ClientSession() as session:
            tasks = [self.catch_and_convert(session, link) for link in self.__raw_links]
            results = await asyncio.gather(*tasks)
            print("CORRECTLY DOWNLOADED DOCS: ", results.count(True))
            print("ALL DOCS COUNT: ", len(results))


if __name__ == "__main__":
    parser = EngeeBlockDocumentationDownloader()

    start = perf_counter()

    asyncio.run(parser.main())

    end = perf_counter()

    print(f"\nTOOK {end - start} seconds")