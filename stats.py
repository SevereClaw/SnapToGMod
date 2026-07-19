"""
stats.py
--------
Статистика запусков: запись, агрегаты, экспорт.

Экспорт теперь только в CSV. Раньше был ещё JSON и HTML-отчёт с
графиками на чистом SVG — для большинства пользователей это лишнее:
CSV одинаково легко открыть в Excel/Таблицах и достаточно, чтобы
посмотреть историю или посчитать что-то самому.
"""
from __future__ import annotations

import collections
import csv
import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from config import STATS_FILE


@dataclass
class LaunchEntry:
    time: str
    server: str


@dataclass
class Stats:
    total_launches: int = 0
    history: list[LaunchEntry] = None

    def __post_init__(self):
        if self.history is None:
            self.history = []


def load_stats(logger) -> Stats:
    try:
        if STATS_FILE.exists():
            data = json.loads(STATS_FILE.read_text(encoding="utf-8"))
            history = [LaunchEntry(**e) for e in data.get("history", [])]
            return Stats(total_launches=data.get("total_launches", 0), history=history)
    except (OSError, json.JSONDecodeError, TypeError) as e:
        logger.warning("Не удалось прочитать stats.json: %s", e)
    return Stats()


def _save_stats(stats: Stats, logger) -> None:
    try:
        payload = {
            "total_launches": stats.total_launches,
            "history": [asdict(e) for e in stats.history],
        }
        STATS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logger.error("Не удалось сохранить stats.json: %s", e)


def record_launch(server_name: str, logger) -> None:
    stats = load_stats(logger)
    stats.total_launches += 1
    stats.history.append(LaunchEntry(time=time.strftime("%Y-%m-%d %H:%M:%S"), server=server_name))
    stats.history = stats.history[-500:]
    _save_stats(stats, logger)


def compute_summary(stats: Stats) -> dict:
    by_day = collections.OrderedDict()
    by_hour = collections.Counter()
    by_server = collections.Counter()
    for entry in stats.history:
        day = entry.time[:10]
        by_day[day] = by_day.get(day, 0) + 1
        by_server[entry.server] += 1
        try:
            by_hour[int(entry.time[11:13])] += 1
        except (ValueError, IndexError):
            pass
    top_server, top_server_count = (by_server.most_common(1)[0] if by_server else (None, 0))
    peak_hour = by_hour.most_common(1)[0][0] if by_hour else None
    return {
        "by_day": list(by_day.items())[-30:],
        "by_hour": by_hour,
        "by_server": by_server,
        "top_server": top_server,
        "top_server_count": top_server_count,
        "peak_hour": peak_hour,
    }


def format_stats_message(logger) -> str:
    stats = load_stats(logger)
    if stats.total_launches == 0:
        return "Пока не было ни одного срабатывания."
    summary = compute_summary(stats)
    lines = [f"Всего срабатываний: {stats.total_launches}"]
    if summary["top_server"]:
        lines.append(f"Самый частый сервер: {summary['top_server']} ({summary['top_server_count']})")
    if summary["peak_hour"] is not None:
        lines.append(f"Самое частое время: {summary['peak_hour']:02d}:00–{summary['peak_hour']:02d}:59")
    lines += ["", "Последние запуски:"]
    for entry in reversed(stats.history[-10:]):
        lines.append(f"  {entry.time} — {entry.server}")
    return "\n".join(lines)


def export_stats_csv(path: str, logger) -> None:
    stats = load_stats(logger)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["Дата и время", "Сервер"])
        for entry in stats.history:
            writer.writerow([entry.time, entry.server])
