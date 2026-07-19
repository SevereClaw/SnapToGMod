"""
logutil.py
----------
Настройка стандартного модуля logging вместо собственной реализации.
Ротация лога (переносить в .log.old при превышении ~2 МБ) теперь делает
logging.handlers.RotatingFileHandler — не нужно вручную считать вызовы
и проверять размер файла.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys

from config import LOG_FILE

MAX_LOG_BYTES = 2 * 1024 * 1024  # 2 МБ


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("snap_to_gmod")
    logger.setLevel(logging.INFO)
    logger.propagate = False

    if logger.handlers:  # setup_logging() уже вызывался — не дублируем хендлеры
        return logger

    fmt = logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=MAX_LOG_BYTES, backupCount=1, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # Собранный .exe с --noconsole не имеет stdout — тогда просто не пишем в консоль.
    if sys.stdout is not None:
        try:
            stream_handler = logging.StreamHandler(sys.stdout)
            stream_handler.setFormatter(fmt)
            logger.addHandler(stream_handler)
        except (OSError, ValueError):
            pass

    return logger


def tail_log_lines(max_lines: int = 300) -> list[str]:
    """Читает последние строки лог-файла, не загружая весь файл в память."""
    try:
        if not LOG_FILE.exists():
            return []
        chunk = 256 * 1024
        with open(LOG_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - chunk))
            data = f.read()
        lines = data.decode("utf-8", errors="replace").splitlines()
        return lines[-max_lines:]
    except OSError as e:
        return [f"[!] Не удалось прочитать журнал: {e}"]
