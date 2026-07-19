"""
hotkeys.py
----------
Регистрация резервных горячих клавиш (запуск игры, быстрая пауза) через
библиотеку keyboard. Опциональна: если библиотека не установлена,
программа продолжает работать без хоткеев.
"""
from __future__ import annotations

import platform
from typing import Callable

from config import AppConfig

try:
    import keyboard
except ImportError:
    keyboard = None

import system


def _add_hotkey_safe(hotkey: str, callback: Callable[[], None], label: str, logger) -> bool:
    try:
        keyboard.parse_hotkey(hotkey)
    except Exception as e:
        logger.warning("Некорректный формат горячей клавиши %s '%s': %s", label, hotkey, e)
        return False
    try:
        keyboard.add_hotkey(hotkey, callback)
        logger.info("Горячая клавиша %s зарегистрирована: %s", label, hotkey)
        return True
    except Exception as e:
        logger.warning("Не удалось зарегистрировать горячую клавишу %s '%s': %s", label, hotkey, e)
        return False


def reregister_all_hotkeys(cfg: AppConfig, on_launch: Callable[[], None], on_pause_toggle: Callable[[], None], logger) -> bool:
    if keyboard is None:
        logger.info("Библиотека 'keyboard' не установлена — горячие клавиши недоступны.")
        return False
    try:
        keyboard.unhook_all_hotkeys()
    except Exception:
        pass
    launch_ok = _add_hotkey_safe(cfg.hotkey or "ctrl+alt+g", on_launch, "запуска", logger)
    pause_ok = _add_hotkey_safe(cfg.pause_hotkey or "ctrl+alt+p", on_pause_toggle, "быстрой паузы", logger)
    if (launch_ok or pause_ok) and platform.system() == "Windows" and not system.is_admin():
        logger.info(
            "Программа запущена БЕЗ прав администратора. Если GMod или Steam запущены "
            "от имени администратора, горячие клавиши не будут срабатывать поверх игры. "
            "Решение: пункт меню трея «Перезапустить от имени администратора»."
        )
    return launch_ok and pause_ok
