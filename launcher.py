"""
launcher.py
-----------
Запуск Garry's Mod через steam://connect и вся логика "что делать при
срабатывании": проверка, что игра уже не запущена, проверка, что Steam
не занят обновлением, необязательная проверка доступности сервера,
обратный отсчёт, звук, запуск, уведомления.
"""
from __future__ import annotations

import csv
import io
import os
import platform
import subprocess
from typing import Callable, Optional

from config import AppConfig, Server
import servers as servers_mod
import stats as stats_mod

GMOD_PROCESS_NAMES = {"gmod.exe", "hl2.exe", "garrysmod.exe"}
STEAM_PROCESS_NAMES = {"steam.exe"}
# Процессы, которые Steam держит открытыми только во время скачивания и
# распаковки обновления самого клиента — если один из них есть в списке
# задач одновременно со steam.exe, велика вероятность, что Steam сейчас
# обновляется, а не просто работает в фоне.
STEAM_UPDATE_HINT_PROCESSES = {"steamservice.exe", "steamwebhelper.exe"}


def _tasklist() -> set[str]:
    if platform.system() != "Windows":
        return set()
    try:
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5, creationflags=creationflags,
        )
        found = set()
        for row in csv.reader(io.StringIO(result.stdout)):
            if row:
                found.add(row[0].strip().lower())
        return found
    except (OSError, subprocess.TimeoutExpired):
        return set()


def is_gmod_running(logger) -> bool:
    """Сравниваем ТОЧНОЕ имя процесса, а не подстроку — иначе, например,
    "SnapToGMod.exe" само содержит "gmod.exe" на конце."""
    found = _tasklist() & GMOD_PROCESS_NAMES
    if found:
        logger.info("Обнаружен запущенный процесс GMod: %s", ", ".join(found))
        return True
    return False


def is_steam_updating(logger) -> bool:
    """Лучшее доступное приближение без официального API Steam: если в
    реестре Windows есть путь установки Steam, читаем время последней
    строки его bootstrap-лога — если она свежее нескольких секунд и
    содержит признаки установки пакета, считаем, что Steam обновляется.
    Если ничего определить не удалось, по умолчанию считаем, что Steam
    свободен (чтобы не блокировать запуск ложноположительно)."""
    if platform.system() != "Windows":
        return False
    try:
        import winreg
        import time as _time

        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        steam_path, _ = winreg.QueryValueEx(key, "SteamPath")
        winreg.CloseKey(key)
        log_path = os.path.join(steam_path.replace("/", os.sep), "logs", "bootstrap_log.txt")
        if not os.path.exists(log_path):
            return False
        mtime = os.path.getmtime(log_path)
        if (_time.time() - mtime) > 5:
            return False  # лог давно не менялся — Steam не в процессе обновления
        with open(log_path, "rb") as f:
            f.seek(max(0, os.path.getsize(log_path) - 4096))
            tail = f.read().decode("utf-8", errors="ignore").lower()
        return any(word in tail for word in ("verifying", "installing", "update", "patch"))
    except Exception as e:
        logger.debug("Не удалось определить, обновляется ли Steam: %s", e)
        return False


def launch_gmod_and_connect(server: Server, logger) -> bool:
    connect_uri = f"steam://connect/{server.ip}:{server.port}"
    logger.info("Открываю %s (%s) ...", connect_uri, server.name)
    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(connect_uri)
        elif system == "Darwin":
            subprocess.run(["open", connect_uri], check=True)
        else:
            subprocess.run(["xdg-open", connect_uri], check=True)
        logger.info("Команда отправлена в Steam.")
        return True
    except OSError as e:
        logger.error("Не удалось открыть steam:// ссылку: %s", e)
        logger.error("Убедитесь, что Steam установлен и является обработчиком steam:// по умолчанию.")
        return False


class TriggerHooks:
    """Колбэки в UI-слой (tray.py), чтобы launcher.py не знал про pystray/tkinter."""

    def __init__(
        self,
        flash_heard: Callable[[str], None],
        flash_trigger: Callable[[str], None],
        show_countdown: Callable[[int, str], bool],
        play_sound: Callable[[str], None],  # event: "detected" | "launch" | "error"
        send_discord: Callable[[Server], None],
        windows_notify: Callable[[str, str], None],
    ):
        self.flash_heard = flash_heard
        self.flash_trigger = flash_trigger
        self.show_countdown = show_countdown
        self.play_sound = play_sound
        self.send_discord = send_discord
        self.windows_notify = windows_notify


def perform_trigger(cfg: AppConfig, logger, save_config: Callable[[], None], hooks: TriggerHooks) -> None:
    """Общая логика срабатывания — вызывается и по щелчку, и по горячей
    клавише. Вызывать из отдельного потока: может блокироваться на
    несколько секунд (обратный отсчёт, проверка доступности сервера)."""
    if cfg.skip_if_running and is_gmod_running(logger):
        logger.info("GMod уже запущен — срабатывание проигнорировано.")
        hooks.flash_heard("Snap-to-GMod: вы уже в игре, срабатывание проигнорировано")
        return

    if cfg.skip_if_steam_updating and is_steam_updating(logger):
        logger.info("Steam сейчас обновляется — запуск отложен, срабатывание проигнорировано.")
        hooks.flash_heard("Snap-to-GMod: Steam обновляется, запуск пропущен")
        return

    server = cfg.active_server()
    hooks.play_sound("detected")

    if cfg.detection_only:
        logger.info("Режим «только обнаружение»: щелчок распознан, игра не запускается.")
        hooks.flash_heard(f"Snap-to-GMod: обнаружен щелчок ({server.name})")
        hooks.windows_notify("Snap-to-GMod", f"Обнаружен щелчок — сервер «{server.name}» готов, запуск не выполнялся.")
        return

    if cfg.check_availability_before_launch:
        availability = servers_mod.check_server_availability(server)
        if not availability["reachable"]:
            logger.warning("Сервер «%s» недоступен (нет ответа на ping/запрос) — запуск отменён.", server.name)
            hooks.play_sound("error")
            hooks.flash_heard(f"Snap-to-GMod: сервер «{server.name}» недоступен")
            return

    countdown = int(cfg.countdown_seconds or 0)
    if countdown > 0 and not hooks.show_countdown(countdown, server.name):
        return

    logger.info("Запускаю GMod -> %s", server.name)
    hooks.flash_trigger(f"Snap-to-GMod: запускаю {server.name}!")
    ok = launch_gmod_and_connect(server, logger)
    if ok:
        hooks.play_sound("launch")
        stats_mod.record_launch(server.name, logger)
        servers_mod.record_recent_server(cfg, server.name)
        save_config()
        hooks.send_discord(server)
        if cfg.notify_enabled:
            hooks.windows_notify("Удачной игры! \U0001F3AE", f"Snap-to-GMod: {server.name}")
    else:
        hooks.play_sound("error")
