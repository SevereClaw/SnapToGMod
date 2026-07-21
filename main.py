"""
main.py
-------
Точка входа: связывает все модули, поднимает трей-иконку и фоновые потоки.

ЗАПУСК ИЗ ИСХОДНИКА:
    pip install sounddevice numpy pystray pillow keyboard
    python main.py

СБОРКА В .EXE:
    pip install pyinstaller
    pyinstaller --onefile --noconsole --name SnapToGMod main.py
"""
from __future__ import annotations

import platform
import sys
import threading
import traceback

import config as cfg_mod
import hotkeys
import launcher
import system
from logutil import setup_logging

logger = setup_logging()

try:
    import numpy as np  # noqa: F401
    import sounddevice as sd  # noqa: F401
    import pystray  # noqa: F401
    from PIL import Image, ImageDraw  # noqa: F401
except ImportError as e:
    msg = (
        f"Не установлена одна из библиотек: {e}\n"
        "Выполните: pip install sounddevice numpy pystray pillow"
    )
    logger.error(msg)
    if cfg_mod.FROZEN:
        system.show_error_box("Snap-to-GMod — ошибка запуска", msg)
    sys.exit(1)

import audio
import discord_notify
import sound
import tray
import updates
import voice_select


def main() -> None:
    if not system.acquire_single_instance_lock():
        logger.info("Программа уже запущена — новый экземпляр закрывается.")
        system.show_info_box("Snap-to-GMod", "Snap-to-GMod уже запущен и слушает микрофон.\nПроверьте иконку в трее.")
        sys.exit(0)

    logger.info("=" * 60)
    logger.info(" Snap-to-GMod v%s: щёлкните пальцами, чтобы запустить Garry's Mod", cfg_mod.APP_VERSION)
    logger.info(" Иконка появится в системном трее. Настройки — правой кнопкой по ней.")
    logger.info("=" * 60)

    cfg = cfg_mod.load_config(logger)
    cfg.autostart = system.is_autostart_enabled()

    def save_config():
        cfg_mod.save_config(cfg, logger)

    save_config()
    logger.info("Настройки загружены. Серверов в списке: %d, активный: %s", len(cfg.servers), cfg.active_server_name)

    audio.print_audio_devices(logger)
    updates.check_for_updates_async(
        logger, silent=True,
        on_update_available=lambda tag, url, asset: tray.show_update_window(tag, url, asset, logger),
    )

    stop_event = threading.Event()
    pause_event = threading.Event()
    trigger_state = audio.TriggerState()
    calibration_state = {"active": False, "peaks": [], "last_capture": 0.0, "target": 5}
    mic_test_state = {"active": False, "peak": 0.0}
    icon_state_holder: list = []

    def notify_windows(message: str, title: str) -> None:
        """Обёртка над icon.notify(): раньше ошибка здесь (например,
        Shell_NotifyIcon не сработал, потому что иконка ещё не готова, или
        сообщение долетело, а сама Windows молча съела баллон — так бывает
        при включённом «Фокусировке внимания»/режиме «Не беспокоить») просто
        падала в лог как необработанное исключение или вообще терялась —
        теперь причина будет видна в журнале программы."""
        try:
            icon.notify(message, title)
        except Exception as e:
            logger.warning(
                "Не удалось показать уведомление Windows («%s: %s»): %s. "
                "Проверьте, не включён ли в Windows режим «Фокусировка внимания»/«Не беспокоить» — "
                "он тоже может молча скрывать всплывающие уведомления от программ.",
                title, message, e,
            )

    voice_engine = voice_select.VoiceSelectEngine(cfg, voice_select.VoiceHooks(
        on_heard=lambda: icon_state_holder[0].flash(
            tray.TrayState["HEARD"], "Snap-to-GMod: услышал слово-триггер...", 0.6, stop_event,
        ),
        on_result=lambda success, message: notify_windows(message, "Голосовой выбор персонажа"),
    ))

    def reregister_hotkeys() -> bool:
        return hotkeys.reregister_all_hotkeys(cfg, on_hotkey_pressed, on_pause_hotkey_pressed, logger)

    hooks = launcher.TriggerHooks(
        flash_heard=lambda text: icon_state_holder[0].flash(tray.TrayState["HEARD"], text, 0.6, stop_event),
        flash_trigger=lambda text: icon_state_holder[0].flash(tray.TrayState["TRIGGER"], text, 1.5, stop_event),
        show_countdown=lambda seconds, name: tray.show_countdown_dialog(seconds, name, icon_state_holder[0], stop_event, logger),
        play_sound=lambda event: sound.play_event_sound(cfg, event, logger),
        send_discord=lambda server: discord_notify.send_discord_notification(cfg, server, logger),
        windows_notify=lambda title, msg: notify_windows(msg, title) if icon_state_holder else None,
    )

    def run_trigger():
        launcher.perform_trigger(cfg, logger, save_config, hooks)

    def on_hotkey_pressed():
        if calibration_state.get("active") or mic_test_state.get("active"):
            return
        if trigger_state.try_consume(cfg.cooldown):
            logger.info("Горячая клавиша нажата -> запускаю GMod.")
            threading.Thread(target=run_trigger, daemon=True).start()

    def on_pause_hotkey_pressed():
        if calibration_state.get("active") or mic_test_state.get("active"):
            return
        if icon_state_holder:
            tray.toggle_pause_gesture(pause_event, icon_state_holder[0], logger, via="жест")

    def on_quit(icon, item):
        if hotkeys.keyboard is not None:
            try:
                hotkeys.keyboard.unhook_all_hotkeys()
            except Exception:
                pass
        stop_event.set()
        icon.stop()

    icon = pystray.Icon(
        "snap_to_gmod",
        tray.make_icon_image(tray.COLORS[tray.TrayState["IDLE"]]),
        "Snap-to-GMod: запускается...",
        menu=tray.build_menu(cfg, logger, on_quit, pause_event, icon_state_holder,
                              calibration_state, mic_test_state, reregister_hotkeys, save_config,
                              voice_engine=voice_engine),
    )

    def setup(icon):
        icon.visible = True
        icon.title = "Snap-to-GMod: жду щелчка"
        logger.info("Иконка в трее должна быть видна.")
        icon_state = tray.IconStateManager(icon)
        icon_state_holder.append(icon_state)
        reregister_hotkeys()
        if cfg.voice_select_enabled:
            voice_engine.load_async(logger)
        threading.Thread(
            target=audio.audio_loop,
            args=(cfg, logger, stop_event, pause_event, trigger_state, calibration_state, mic_test_state),
            kwargs=dict(
                voice_engine=voice_engine,
                on_snap_detected=lambda: threading.Thread(target=run_trigger, daemon=True).start(),
                on_calibration_progress=lambda: icon_state.flash(
                    tray.TrayState["TRIGGER"],
                    f"Snap-to-GMod: калибровка {len(calibration_state['peaks'])}/{calibration_state['target']}",
                    0.4, stop_event, idle_state=tray.TrayState["PAUSED"], idle_tooltip="Snap-to-GMod: калибровка...",
                ),
                on_calibration_done=lambda: (
                    icon_state.bump_version(),
                    icon_state.set_state(tray.TrayState["IDLE"], "Snap-to-GMod: жду щелчка"),
                    system.show_info_box("Snap-to-GMod — калибровка", f"Готово! Новый порог чувствительности: {cfg.threshold:.3f}"),
                ),
                on_heard=lambda: icon_state.flash(tray.TrayState["HEARD"], "Snap-to-GMod: услышал звук...", 0.3, stop_event),
                save_config=save_config,
            ),
            daemon=True,
        ).start()

    try:
        icon.run(setup=setup)
    except KeyboardInterrupt:
        stop_event.set()
        logger.info("Остановлено пользователем.")
    except Exception:
        logger.error("ОШИБКА при запуске трей-иконки:")
        logger.error(traceback.format_exc())
        logger.error("Возможные причины на Windows: переустановите pystray и pillow — "
                      "pip install --force-reinstall pystray pillow")
        sys.exit(1)

    logger.info("Завершение работы.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        tb = traceback.format_exc()
        logger.error("НЕОБРАБОТАННАЯ ОШИБКА:\n%s", tb)
        if cfg_mod.FROZEN:
            system.show_error_box("Snap-to-GMod — ошибка",
                                   f"Программа столкнулась с ошибкой и должна закрыться.\nПодробности в файле:\n{cfg_mod.LOG_FILE}")
    finally:
        if not cfg_mod.FROZEN and platform.system() == "Windows":
            try:
                input("\nНажмите Enter, чтобы закрыть окно...")
            except EOFError:
                pass
