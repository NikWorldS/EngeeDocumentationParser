import argparse
import asyncio
from datetime import datetime
import os
from typing import Any, Callable
from dotenv import load_dotenv

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from services.converter import (
    DEFAULT_API_URL,
    DEFAULT_MAX_CHARS_PER_CHUNK,
    DEFAULT_MODEL,
    DEFAULT_TEMPERATURE,
    DEFAULT_TIMEOUT_SECONDS,
    EngeeDocumentationConverter,
    build_converter_settings,
)
from services.parser import EngeeBlockDocumentationDownloader

load_dotenv()
BASE_RUNS_DIRECTORY = "./runs"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Engee documentation and convert it to a simplified form."
    )
    parser.add_argument(
        "--api-url",
        default=DEFAULT_API_URL,
        help="OpenAI-compatible chat/completions API URL.",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="LLM API key. If omitted, environment variables are used.",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Converter model name (default: {DEFAULT_MODEL}).",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=DEFAULT_TEMPERATURE,
        help=f"Converter temperature (default: {DEFAULT_TEMPERATURE}).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"Converter HTTP timeout in seconds (default: {DEFAULT_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--max-chars-per-chunk",
        type=int,
        default=DEFAULT_MAX_CHARS_PER_CHUNK,
        help=(
            "Split large markdown files into chunks no larger than this value "
            f"before sending them to the model (default: {DEFAULT_MAX_CHARS_PER_CHUNK})."
        ),
    )
    parser.add_argument(
        "--overwrite-converted",
        action="store_true",
        help="Overwrite files in converted_docs if they already exist.",
    )
    parser.add_argument(
        "--parser-max-concurrent-requests",
        type=int,
        default=10,
        help="Maximum number of simultaneous documentation requests (default: 10).",
    )
    parser.add_argument(
        "--parser-timeout-seconds",
        type=int,
        default=60,
        help="Total timeout for one documentation request in seconds (default: 60).",
    )
    parser.add_argument(
        "--parser-max-request-retries",
        type=int,
        default=2,
        help="Number of retries for failed or timed out documentation requests (default: 2).",
    )
    return parser.parse_args()


def setup() -> None:
    if not os.path.exists(BASE_RUNS_DIRECTORY):
        os.mkdir(BASE_RUNS_DIRECTORY)


def _create_dir(dir_path: str) -> None:
    try:
        os.mkdir(dir_path)
    except FileExistsError as exc:
        raise FileExistsError(f"Directory for this run ({dir_path}) already exists.") from exc


def get_current_run_dir_path() -> str:
    datetime_now = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    dir_path = f"{BASE_RUNS_DIRECTORY}/{datetime_now}"
    _create_dir(dir_path)
    return dir_path


def make_callback(progress: Progress, task_id: TaskID) -> Callable[[Any], None]:
    def update_progress(advance: int) -> None:
        progress.update(task_id=task_id, advance=advance)

    return update_progress


async def main() -> None:
    args = parse_args()
    console = Console(log_path=False)

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
    console.log(f"Current run directory: {run_dir_path}")

    try:
        converter_settings = build_converter_settings(
            api_url=args.api_url,
            api_key=args.api_key,
            model=args.model,
            temperature=args.temperature,
            timeout_seconds=args.timeout_seconds,
            max_chars_per_chunk=args.max_chars_per_chunk,
            overwrite=args.overwrite_converted,
        )
    except ValueError as exc:
        console.log(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc

    parser = EngeeBlockDocumentationDownloader(
        work_dir=run_dir_path,
        max_concurrent_requests=args.parser_max_concurrent_requests,
        request_timeout_seconds=args.parser_timeout_seconds,
        max_request_retries=args.parser_max_request_retries,
    )
    converter = EngeeDocumentationConverter(
        run_dir=run_dir_path,
        settings=converter_settings,
    )

    all_libs = parser.get_all_libs()
    if all_libs is None:
        raise ValueError("Failed to get available Engee libraries.")

    libs_indexes = list(all_libs.keys())

    console.log("Choose allowed libs for parsing...")
    console.log(all_libs)
    chosen_libs = console.input(
        "Enter numbers of allowed libs separated by spaces (for example: `0 1 2`). "
        "Enter `all` to process every library.\n--"
    ).strip()

    if chosen_libs == "all":
        console.log("Chosen all libs")
    else:
        selected_indexes = [
            int(value)
            for value in chosen_libs.split()
            if value.isnumeric() and int(value) in libs_indexes
        ]
        if not selected_indexes:
            raise ValueError("Gets 0 chosen libs.")
        parser.choose_allowed_libs(selected_indexes)
        console.log(f"Chosen libs: {parser.get_allowed_libs()}")

    progress.start()
    try:
        parser_task_id = progress.add_task(
            "[cyan]Downloading documentation...[/cyan]",
            total=len(parser.parse_links()),
        )
        parser.set_callback(make_callback(progress, parser_task_id))
        parser_result = await parser.main()
        progress.update(
            parser_task_id,
            description="[green]Documentation downloading completed.[/green]",
        )

        converter_total = converter.get_total_files()
        if converter_total > 0:
            converter_task_id = progress.add_task(
                "[magenta]Converting documentation...[/magenta]",
                total=converter_total,
            )
            converter.set_callback(make_callback(progress, converter_task_id))
            converter_result = await converter.convert()
            progress.update(
                converter_task_id,
                description="[green]Documentation conversion completed.[/green]",
            )
        else:
            converter_result = await converter.convert()
    finally:
        progress.stop()

    console.log(
        "Parser result:\n"
        f"Total processed blocks: {parser_result.total}\n"
        f"Success: {parser_result.successes}, Failed: {parser_result.failures}, Skipped: {parser_result.skipped}"
    )
    console.log(
        "Converter result:\n"
        f"Total files: {converter_result.total}\n"
        f"Success: {converter_result.success}, Failed: {converter_result.failed}, Skipped: {converter_result.skipped}"
    )


if __name__ == "__main__":
    asyncio.run(main())
