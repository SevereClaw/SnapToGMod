"""
sound.py
--------
Проигрывание звука при разных событиях: щелчок обнаружен, успешный
запуск, ошибка запуска. Для каждого события можно задать свой .wav —
если не задан, используется стандартный системный сигнал.
"""
from __future__ import annotations

import os

from config import AppConfig

try:
    import winsound
except ImportError:
    winsound = None

_DEFAULT_BEEPS = {
    "detected": "MB_ICONASTERISK",
    "launch": "MB_OK",
    "error": "MB_ICONHAND",
}


def play_event_sound(cfg: AppConfig, event: str, logger) -> None:
    if not cfg.sound_enabled or winsound is None:
        return
    path = getattr(cfg.sounds, event, None)
    try:
        if path and os.path.exists(path):
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        else:
            beep = getattr(winsound, _DEFAULT_BEEPS.get(event, "MB_OK"))
            winsound.MessageBeep(beep)
    except Exception as e:
        logger.warning("Не удалось воспроизвести звук (%s): %s", event, e)
