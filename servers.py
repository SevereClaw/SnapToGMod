"""
servers.py
----------
Всё, что касается списка серверов: добавление/удаление, избранное, поиск
по имени, последние использованные сервера, массовая очистка списка, и
проверка доступности сервера (ping + запрос Source Engine) перед
запуском.

Импорт из Steam Favorites убран: он вытаскивал регуляркой вообще любой
"ip:port" из .vdf-файла, из-за чего список серверов быстро раздувался
мусором. Список серверов больше НЕ подтягивается автоматически из
интернета — для небольшого числа серверов ручное добавление проще и
надёжнее и не зависит от чужого URL с непонятным временем жизни.
"""
from __future__ import annotations

import platform
import re
import socket
import struct
import subprocess
from dataclasses import dataclass
from typing import Optional

from config import AppConfig, MAX_RECENT_SERVERS, Server


def add_server(cfg: AppConfig, name: str, ip: str, port: str) -> None:
    cfg.servers = [s for s in cfg.servers if s.name != name]
    cfg.servers.append(Server(name=name, ip=ip, port=port))
    cfg.active_server_name = name


def remove_server(cfg: AppConfig, name: str) -> bool:
    if len(cfg.servers) <= 1:
        return False
    cfg.servers = [s for s in cfg.servers if s.name != name]
    if cfg.active_server_name == name and cfg.servers:
        cfg.active_server_name = cfg.servers[0].name
    return True


def toggle_favorite(cfg: AppConfig, name: str) -> None:
    for server in cfg.servers:
        if server.name == name:
            server.favorite = not server.favorite
            return


def sorted_servers(cfg: AppConfig) -> list[Server]:
    """Избранные сначала, затем остальные — в порядке добавления."""
    return sorted(cfg.servers, key=lambda s: not s.favorite)


def search_servers(cfg: AppConfig, query: str) -> list[Server]:
    q = query.strip().lower()
    if not q:
        return list(cfg.servers)
    return [s for s in cfg.servers if q in s.name.lower() or q in s.ip]


def record_recent_server(cfg: AppConfig, name: str) -> None:
    """Хранит последние 20-30 использованных серверов (без дублей, самый
    свежий — в конце)."""
    recent = [n for n in cfg.recent_servers if n != name]
    recent.append(name)
    cfg.recent_servers = recent[-MAX_RECENT_SERVERS:]


def clear_non_favorite_servers(cfg: AppConfig) -> int:
    """Удаляет из списка все сервера, кроме избранных и текущего активного
    (чтобы не остаться совсем без выбранного сервера). Возвращает, сколько
    серверов было удалено. Пригодится, если список раздулся, например,
    после старого импорта из Steam Favorites."""
    keep_names = {cfg.active_server_name}
    before = len(cfg.servers)
    cfg.servers = [s for s in cfg.servers if s.favorite or s.name in keep_names]
    if not cfg.servers:
        cfg.servers = [Server("Shinri Trial", "80.66.82.229", "27103")]
        cfg.active_server_name = cfg.servers[0].name
    cfg.recent_servers = [n for n in cfg.recent_servers if n in {s.name for s in cfg.servers}]
    return before - len(cfg.servers)


def ping_host(ip: str, timeout: float = 1.0) -> Optional[float]:
    """Пингует IP через системную утилиту ping. Возвращает время в мс или
    None, если хост не ответил."""
    system = platform.system()
    count_flag = "-n" if system == "Windows" else "-c"
    timeout_flag = "-w" if system == "Windows" else "-W"
    timeout_value = str(int(timeout * 1000)) if system == "Windows" else str(max(1, int(timeout)))
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        result = subprocess.run(
            ["ping", count_flag, "1", timeout_flag, timeout_value, ip],
            capture_output=True, text=True, timeout=timeout + 2,
            creationflags=creationflags,
        )
        match = re.search(r"time[=<]([\d.]+)\s*ms", result.stdout, re.IGNORECASE)
        if match:
            return float(match.group(1))
        return None
    except (OSError, subprocess.TimeoutExpired):
        return None


@dataclass
class SourceServerInfo:
    online: bool
    name: Optional[str] = None
    map: Optional[str] = None
    players: Optional[int] = None
    max_players: Optional[int] = None


def query_source_server(ip: str, port: str, timeout: float = 1.5) -> SourceServerInfo:
    """Отправляет A2S_INFO запрос движку Source по UDP — тот же протокол,
    которым пользуется список серверов внутри самой игры. Не требует
    сторонних библиотек."""
    request = b"\xFF\xFF\xFF\xFFTSource Engine Query\x00"
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(request, (ip, int(port)))
            data, _ = sock.recvfrom(4096)
    except (OSError, ValueError, socket.timeout):
        return SourceServerInfo(online=False)

    try:
        # Заголовок: 4 байта 0xFF + 1 байт типа ответа ('I' = 0x49 для A2S_INFO).
        if len(data) < 6 or data[4:5] != b"I":
            return SourceServerInfo(online=True)  # ответил, но формат не разобрали
        offset = 6  # пропускаем FF FF FF FF 'I' protocol_version
        def read_cstr() -> str:
            nonlocal offset
            end = data.index(b"\x00", offset)
            s = data[offset:end].decode("utf-8", errors="replace")
            offset = end + 1
            return s
        server_name = read_cstr()
        read_cstr()  # map
        read_cstr()  # game directory
        read_cstr()  # game description
        offset += 2  # app id (short)
        players = data[offset]
        offset += 1
        max_players = data[offset]
        return SourceServerInfo(online=True, name=server_name, players=players, max_players=max_players)
    except (IndexError, ValueError, struct.error):
        return SourceServerInfo(online=True)


def check_server_availability(server: Server) -> dict:
    """Быстрая сводная проверка перед запуском: пинг + запрос движка."""
    ping_ms = ping_host(server.ip)
    info = query_source_server(server.ip, server.port)
    return {
        "reachable": ping_ms is not None or info.online,
        "ping_ms": ping_ms,
        "online": info.online,
        "players": info.players,
        "max_players": info.max_players,
    }
