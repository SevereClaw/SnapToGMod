"""
updates.py
----------
Проверка новой версии на GitHub Releases и (на собранном .exe) полностью
автоматическое обновление: скачать -> подождать закрытия текущего
процесса -> заменить -> перезапустить.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import threading
import urllib.request
from typing import Optional

from config import APP_VERSION, FROZEN, GITHUB_REPO, SETTINGS_DIR


def _parse_version(v: str) -> tuple:
    v = (v or "").strip().lstrip("vV")
    parts = []
    for piece in v.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def check_for_updates(logger, silent: bool, on_update_available) -> None:
    """on_update_available(latest_tag, latest_url, asset_url) вызывается,
    только если найдена более новая версия — окно/сообщение рисует
    вызывающая сторона (tray.py), чтобы этот модуль не зависел от tkinter."""
    from system import show_info_box, show_error_box

    if not GITHUB_REPO or "yourusername" in GITHUB_REPO:
        logger.info("Проверка обновлений не настроена (GITHUB_REPO не указан).")
        if not silent:
            show_info_box("Snap-to-GMod", "Проверка обновлений не настроена в коде программы.")
        return
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": "SnapToGMod"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latest_tag = data.get("tag_name", "")
        latest_url = data.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases"
        if not latest_tag:
            raise ValueError("пустой tag_name в ответе GitHub")
        asset_url = next(
            (a.get("browser_download_url") for a in data.get("assets", [])
             if str(a.get("name", "")).lower().endswith(".exe")),
            None,
        )
        if _parse_version(latest_tag) > _parse_version(APP_VERSION):
            logger.info("Доступна новая версия: %s (у вас %s)", latest_tag, APP_VERSION)
            on_update_available(latest_tag, latest_url, asset_url)
        else:
            logger.info("Установлена последняя версия (%s).", APP_VERSION)
            if not silent:
                show_info_box("Snap-to-GMod", f"У вас последняя версия: {APP_VERSION}")
    except Exception as e:
        logger.warning("Не удалось проверить обновления: %s", e)
        if not silent:
            show_error_box("Snap-to-GMod", f"Не удалось проверить обновления:\n{e}")


def check_for_updates_async(logger, silent: bool, on_update_available) -> None:
    threading.Thread(target=check_for_updates, args=(logger, silent, on_update_available), daemon=True).start()


def apply_auto_update(asset_url: str, logger) -> tuple[bool, str]:
    """Скачивает новый .exe и готовит его замену через маленький .bat,
    который ждёт закрытия текущего процесса, подменяет файл и
    перезапускает программу (запущенный .exe нельзя перезаписать напрямую
    в Windows)."""
    try:
        current_exe = sys.executable
        update_dir = SETTINGS_DIR / "update"
        update_dir.mkdir(parents=True, exist_ok=True)
        new_exe = update_dir / "SnapToGMod_new.exe"

        req = urllib.request.Request(asset_url, headers={"User-Agent": "SnapToGMod"})
        with urllib.request.urlopen(req, timeout=30) as resp, open(new_exe, "wb") as f:
            f.write(resp.read())

        bat_path = update_dir / "apply_update.bat"
        exe_name = os.path.basename(current_exe)
        bat_contents = (
            "@echo off\r\n"
            ":wait\r\n"
            f'tasklist /fi "imagename eq {exe_name}" | find /i "{exe_name}" >nul\r\n'
            "if not errorlevel 1 (\r\n"
            "  timeout /t 1 /nobreak >nul\r\n"
            "  goto wait\r\n"
            ")\r\n"
            f'copy /y "{new_exe}" "{current_exe}" >nul\r\n'
            f'start "" "{current_exe}"\r\n'
            f'del "{new_exe}" >nul 2>&1\r\n'
            'del "%~f0" >nul 2>&1\r\n'
        )
        bat_path.write_text(bat_contents, encoding="utf-8")

        subprocess.Popen(
            ["cmd", "/c", "start", "", "/min", str(bat_path)],
            creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS,
            close_fds=True,
        )
        logger.info("Автообновление запущено, ждёт закрытия %s.", current_exe)
        return True, ""
    except Exception as e:
        logger.error("Автообновление не удалось: %s", e)
        return False, str(e)
