import argparse
import asyncio
from datetime import datetime
import os
from typing import Callable, Any

from rich.console import Console
from rich.progress import (
    Progress,
    SpinnerColumn,
    BarColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TaskID
)

from services.parser import EngeeBlockDocumentationDownloader


BASE_RUNS_DIRECTORY = "./runs"


def setup() -> None:
    """
     Создаёт директорию для сохранения результатов запусков парсера
    """
    if not os.path.exists(BASE_RUNS_DIRECTORY):
        os.mkdir(BASE_RUNS_DIRECTORY)



def __create_dir(dir_path: str) -> None:
    """
    Создаёт папку для отдельного запуска
    :param dir_path: название папки для создания
    """
    try:
        os.mkdir(dir_path)
    except FileExistsError:
        raise FileExistsError(f"Directory for this run ({dir_path}) already exists.")

def get_current_run_dir_path() -> str:
    """
    Возвращает путь к папке
    :return: путь к папке текущего запуска парсера
    """
    datetime_now = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dir_path = f"{BASE_RUNS_DIRECTORY}/{datetime_now}"
    __create_dir(dir_path)
    return dir_path

def make_callback(progress: Progress, task_id: TaskID) -> Callable[[Any], None]:
    """
    Создаёт и возвращает функцию для продвижения прогресса с установленным
    :param progress: объект прогресс-бара
    :param task_id: ID задачи
    :return: функция для продвижения прогресса
    """
    def update_progress(advance: int) -> None:
        """
        Функция продвижения прогресса
        :param advance: значение продвижения
        """
        progress.update(task_id=task_id, advance=advance)
    return update_progress

async def main() -> None:
    """Точка входа. Создаёт прогресс-бар, отслеживает задачу парсинга и скачивания документации блоков"""
    console = Console(
        log_path=False
    )

    progress = Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    )

    setup()
    run_dir_path = get_current_run_dir_path()

    parser = EngeeBlockDocumentationDownloader(
        work_dir=run_dir_path,
    )

    all_libs = parser.get_all_libs()
    libs_indexes = list(all_libs.keys())

    console.log("Choose allowed libs for parsing...")
    console.log(all_libs)
    chosen_libs = console.input("Enter numbers of allowed lib, divided by comma (ex.: `0 1 2`). Enter `all` for choose all libs.\n--").strip()
    if chosen_libs == "all":
        console.log("Chosen all libs")
    else:
        chosen_libs = [int(x) for x in chosen_libs.split() if x.isnumeric() and (int(x) in libs_indexes)]
        if not chosen_libs:
            raise ValueError("Gets 0 chosen libs.")
        parser.choose_allowed_libs(chosen_libs)
        console.log(f"Chosen libs: {parser.get_allowed_libs()}")

    progress.start()

    parser_task_id = progress.add_task(
        "[cyan]Downloading documentation...[/cyan]",
        total=len(parser.parse_links())
    )

    parser.set_callback(make_callback(progress, parser_task_id))

    parser_result = await parser.main()

    progress.update(
        parser_task_id,
        description="[green]Documentation downloading completed. [/green]"
        )
    progress.stop()
    console.log(f"Total processed blocks: {parser_result.total}\nSuccess: {parser_result.successes}, Failed: {parser_result.failures}, Skipped: {parser_result.skipped}")

if __name__ == "__main__":
    asyncio.run(main())