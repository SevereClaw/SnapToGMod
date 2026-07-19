"""
audio.py
--------
Распознавание щелчка/хлопка по звуку микрофона и основной цикл
прослушивания. Никакого tkinter здесь нет — только сигнал и состояние;
все окна живут в tray.py.
"""
from __future__ import annotations

import collections
import threading
import time
import traceback
from typing import Callable, Optional

import numpy as np
import sounddevice as sd

from config import AppConfig, SAMPLE_RATE, BLOCK_SIZE


def snap_peak_if_shaped(audio_block: np.ndarray, min_peak: float = 0.03) -> Optional[float]:
    """Проверяет "форму" звука (резкость + широкополосность), без проверки
    порога чувствительности. Возвращает пиковую громкость, если звук похож
    на щелчок/хлопок, иначе None."""
    peak = float(np.max(np.abs(audio_block)))
    if peak < min_peak:
        return None

    loud_ratio = np.mean(np.abs(audio_block) > (peak * 0.5))
    if loud_ratio >= 0.25:
        return None

    spectrum = np.abs(np.fft.rfft(audio_block))
    freqs = np.fft.rfftfreq(len(audio_block), d=1.0 / SAMPLE_RATE)
    total_energy = float(np.sum(spectrum)) + 1e-9
    high_ratio = float(np.sum(spectrum[freqs > 2000])) / total_energy
    if high_ratio <= 0.12:
        return None

    return peak


class TriggerState:
    """Общий кулдаун между щелчком-триггером и горячей клавишей запуска."""

    def __init__(self):
        self._lock = threading.Lock()
        self._last_trigger = 0.0

    def try_consume(self, cooldown: float) -> bool:
        with self._lock:
            now = time.time()
            if now - self._last_trigger > cooldown:
                self._last_trigger = now
                return True
            return False


def list_input_devices() -> list[tuple[int, str]]:
    """Возвращает список входных устройств для меню выбора микрофона.

    PortAudio (через который работает sounddevice) видит один и тот же
    физический микрофон отдельно для каждого звукового API Windows
    (MME, DirectSound, WASAPI, WDM-KS) — без фильтрации в меню было бы
    по 3-4 одинаковых на вид пункта на каждое устройство. Здесь сначала
    берём устройства через API по умолчанию (обычно самый совместимый
    вариант) и убираем дубли по имени, а затем добавляем те устройства,
    которых в этом API вообще не было (доступны только через другой API)."""
    try:
        devices = sd.query_devices()
    except Exception:
        return []

    try:
        default_hostapi = sd.default.hostapi
        if isinstance(default_hostapi, (list, tuple)):
            default_hostapi = default_hostapi[0]
    except Exception:
        default_hostapi = 0

    seen_names: set[str] = set()
    result: list[tuple[int, str]] = []

    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) > 0 and d.get("hostapi") == default_hostapi:
            name = d["name"]
            if name not in seen_names:
                seen_names.add(name)
                result.append((i, name))

    for i, d in enumerate(devices):
        if d.get("max_input_channels", 0) > 0:
            name = d["name"]
            if name not in seen_names:
                seen_names.add(name)
                result.append((i, name))

    return result


def print_audio_devices(logger) -> None:
    logger.info("-" * 60)
    logger.info("Доступные аудиоустройства:")
    try:
        logger.info(str(sd.query_devices()))
        logger.info("Устройство ввода по умолчанию: #%s", sd.default.device[0])
    except Exception as e:
        logger.warning("Не удалось получить список устройств: %s", e)
    logger.info("-" * 60)


def audio_loop(
    cfg: AppConfig,
    logger,
    stop_event: threading.Event,
    pause_event: threading.Event,
    trigger_state: TriggerState,
    calibration_state: dict,
    mic_test_state: dict,
    on_snap_detected: Callable[[], None],
    on_calibration_progress: Callable[[], None],
    on_calibration_done: Callable[[], None],
    on_heard: Callable[[], None],
    save_config: Callable[[], None],
) -> None:
    """Работает в фоновом потоке: слушает микрофон и вызывает переданные
    колбэки — сам поток ничего не знает про иконку трея или tkinter,
    только про звук и конфиг."""
    last_heard_flash = 0.0
    recent_peaks: collections.deque = collections.deque(maxlen=12)  # ~250-300 мс истории
    ambient_floor: collections.deque = collections.deque(maxlen=200)  # ~4 сек фонового шума
    last_adaptive_update = 0.0

    def callback(indata, frames, time_info, status):
        nonlocal last_heard_flash, last_adaptive_update
        if status:
            logger.warning(str(status))
        if pause_event.is_set():
            return

        audio_block = indata[:, 0]
        now = time.time()
        peak_now = float(np.max(np.abs(audio_block)))

        if mic_test_state.get("active"):
            mic_test_state["peak"] = peak_now
            return

        if calibration_state.get("active"):
            peak = snap_peak_if_shaped(audio_block)
            if peak is not None and (now - calibration_state.get("last_capture", 0)) > 1.0:
                calibration_state["last_capture"] = now
                calibration_state["peaks"].append(peak)
                on_calibration_progress()
                if len(calibration_state["peaks"]) >= calibration_state["target"]:
                    new_threshold = max(0.05, min(0.5, min(calibration_state["peaks"]) * 0.7))
                    cfg.threshold = round(new_threshold, 3)
                    save_config()
                    calibration_state["active"] = False
                    logger.info("Калибровка завершена, новый порог: %s", cfg.threshold)
                    on_calibration_done()
            return

        threshold = cfg.threshold
        cooldown = cfg.cooldown

        shaped_peak = snap_peak_if_shaped(audio_block)
        is_candidate = shaped_peak is not None and shaped_peak >= threshold

        if cfg.adaptive_sensitivity and not is_candidate:
            ambient_floor.append(peak_now)
            if len(ambient_floor) >= 40 and (now - last_adaptive_update) > 2.0:
                last_adaptive_update = now
                floor = float(np.median(ambient_floor))
                new_threshold = max(0.06, min(0.45, floor * 4.0))
                if abs(new_threshold - cfg.threshold) > 0.01:
                    cfg.threshold = round(new_threshold, 3)
                    save_config()
                    logger.info("Адаптивная чувствительность: фон %.4f -> новый порог %s", floor, cfg.threshold)

        if is_candidate:
            noisy_recent = sum(1 for p in recent_peaks if p > threshold * 0.35)
            if noisy_recent >= 3:
                logger.info(
                    "Пик %.3f похож на щелчок, но перед ним был продолжительный шум — "
                    "срабатывание пропущено (защита от ложных повторов).", shaped_peak,
                )
            elif trigger_state.try_consume(cooldown):
                logger.info("Пик: %.3f (порог %s) -> распознан как щелчок", shaped_peak, threshold)
                on_snap_detected()
        elif peak_now > threshold * 0.4 and (now - last_heard_flash) > 0.3:
            last_heard_flash = now
            on_heard()

        recent_peaks.append(peak_now)

    try:
        logger.info("Открываю аудиопоток микрофона...")
        with sd.InputStream(
            device=cfg.input_device_index, channels=1, samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE, callback=callback,
        ):
            logger.info("Микрофон слушается. Щёлкните пальцами рядом с ним.")
            while not stop_event.is_set():
                time.sleep(0.1)
    except Exception as e:
        logger.error("ОШИБКА аудиопотока: %s", e)
        logger.error(traceback.format_exc())
