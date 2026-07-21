"""
tray.py
-------
Иконка в системном трее, все вспомогательные окна tkinter (ввод текста,
обратный отсчёт, калибровка, тест микрофона, просмотр журнала, окно
обновления) и сборка меню. Это единственный модуль, которому разрешено
знать про pystray и tkinter.
"""
from __future__ import annotations

import copy
import platform
import threading
import time
from typing import Callable, Optional

import pystray
from PIL import Image, ImageDraw

import audio
import config as cfg_mod
import discord_notify
import launcher
import servers as servers_mod
import sound
import stats as stats_mod
import system
import updates
import voice_select
from config import AppConfig, SENSITIVITY_PRESETS, SERVER_SEARCH_THRESHOLD, Server
from logutil import tail_log_lines

# ==================== ИКОНКА ====================

TrayState = {"IDLE": "idle", "HEARD": "heard", "TRIGGER": "trigger", "PAUSED": "paused"}
COLORS = {
    TrayState["IDLE"]: (120, 120, 120),
    TrayState["HEARD"]: (230, 190, 40),
    TrayState["TRIGGER"]: (40, 200, 90),
    TrayState["PAUSED"]: (130, 90, 200),
}


def make_icon_image(color):
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    margin = 6
    draw.ellipse((margin, margin, size - margin, size - margin), fill=color, outline=(30, 30, 30, 255), width=3)
    return img


class IconStateManager:
    """Централизованное, потокобезопасное управление цветом/подсказкой
    иконки (pystray на Windows падает, если icon.icon меняют из двух
    потоков одновременно)."""

    def __init__(self, icon: "pystray.Icon"):
        self.icon = icon
        self._lock = threading.Lock()
        self._version = 0

    def set_state(self, state: str, tooltip: str):
        with self._lock:
            try:
                self.icon.icon = make_icon_image(COLORS[state])
                self.icon.title = tooltip
            except OSError:
                pass

    def flash(self, state: str, tooltip: str, duration: float, stop_event: threading.Event,
              idle_state: str = TrayState["IDLE"], idle_tooltip: str = "Snap-to-GMod: жду щелчка"):
        self._version += 1
        my_version = self._version
        self.set_state(state, tooltip)

        def revert():
            time.sleep(duration)
            if not stop_event.is_set() and self._version == my_version:
                self.set_state(idle_state, idle_tooltip)

        threading.Thread(target=revert, daemon=True).start()

    def bump_version(self):
        self._version += 1


def toggle_pause_gesture(pause_event: threading.Event, icon_state: IconStateManager, logger, via: str = "меню"):
    if pause_event.is_set():
        pause_event.clear()
        icon_state.bump_version()
        icon_state.set_state(TrayState["IDLE"], "Snap-to-GMod: жду щелчка")
        logger.info("Прослушивание возобновлено (%s).", via)
    else:
        pause_event.set()
        icon_state.bump_version()
        icon_state.set_state(TrayState["PAUSED"], "Snap-to-GMod: на паузе")
        logger.info("Прослушивание приостановлено (%s).", via)


# ==================== ОБЩИЕ ДИАЛОГИ TKINTER ====================

def ask_text(title: str, prompt: str) -> Optional[str]:
    """БЛОКИРУЕТ вызывающий поток до ответа — вызывать только из фонового потока."""
    result = {"value": None}
    done = threading.Event()

    def _ask():
        try:
            import tkinter as tk
            from tkinter import simpledialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            result["value"] = simpledialog.askstring(title, prompt, parent=root)
            root.destroy()
        except Exception:
            pass
        finally:
            done.set()

    threading.Thread(target=_ask, daemon=True).start()
    done.wait(timeout=120)
    return result["value"]


def ask_yes_no(title: str, prompt: str) -> bool:
    """БЛОКИРУЕТ вызывающий поток до ответа — вызывать только из фонового потока."""
    result = {"value": False}
    done = threading.Event()

    def _ask():
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            result["value"] = messagebox.askyesno(title, prompt, parent=root)
            root.destroy()
        except Exception:
            pass
        finally:
            done.set()

    threading.Thread(target=_ask, daemon=True).start()
    done.wait(timeout=120)
    return result["value"]


def ask_open_file(title: str, filetypes) -> Optional[str]:
    result = {"path": None}
    done = threading.Event()

    def _pick():
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            result["path"] = filedialog.askopenfilename(title=title, filetypes=filetypes)
            root.destroy()
        except Exception:
            pass
        finally:
            done.set()

    threading.Thread(target=_pick, daemon=True).start()
    done.wait(timeout=120)
    return result["path"] or None


def ask_save_file(title: str, default_ext: str, initial_file: str, filetypes) -> Optional[str]:
    result = {"path": None}
    done = threading.Event()

    def _pick():
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            result["path"] = filedialog.asksaveasfilename(
                title=title, defaultextension=default_ext, initialfile=initial_file, filetypes=filetypes,
            )
            root.destroy()
        except Exception:
            pass
        finally:
            done.set()

    threading.Thread(target=_pick, daemon=True).start()
    done.wait(timeout=120)
    return result["path"] or None


def show_countdown_dialog(seconds: int, server_name: str, icon_state: IconStateManager,
                           stop_event: threading.Event, logger) -> bool:
    """БЛОКИРУЕТ вызывающий поток. Возвращает True, если можно запускать игру."""
    result = {"proceed": True}
    done = threading.Event()

    def _flow():
        try:
            import tkinter as tk
        except ImportError:
            done.set()
            return
        try:
            root = tk.Tk()
            root.title("Snap-to-GMod")
            root.attributes("-topmost", True)
            root.resizable(False, False)
            root.protocol("WM_DELETE_WINDOW", lambda: None)

            remaining = {"value": seconds}
            label_var = tk.StringVar(value=f"Подключаюсь к «{server_name}» через {remaining['value']} сек...")
            tk.Label(root, textvariable=label_var, padx=26, pady=16, justify="center", font=("Segoe UI", 11)).pack()

            def do_cancel():
                result["proceed"] = False
                done.set()
                try:
                    root.destroy()
                except Exception:
                    pass

            tk.Button(root, text="Отменить", command=do_cancel, padx=14, pady=6).pack(pady=(0, 16))

            def tick():
                if done.is_set():
                    return
                remaining["value"] -= 1
                if remaining["value"] <= 0:
                    done.set()
                    try:
                        root.destroy()
                    except Exception:
                        pass
                    return
                label_var.set(f"Подключаюсь к «{server_name}» через {remaining['value']} сек...")
                root.after(1000, tick)

            root.after(1000, tick)
            root.mainloop()
        except Exception as e:
            logger.warning("Ошибка окна обратного отсчёта: %s", e)
            done.set()

    threading.Thread(target=_flow, daemon=True).start()
    icon_state.flash(TrayState["HEARD"], f"Snap-to-GMod: запуск через {seconds} сек (отменить в окне)...",
                      min(seconds, 2.0), stop_event)
    done.wait(timeout=seconds + 3)
    if not result["proceed"]:
        logger.info("Запуск отменён пользователем в окне обратного отсчёта.")
    return result["proceed"]


def open_log_viewer(icon, state: dict, logger):
    def _window():
        try:
            import tkinter as tk
        except ImportError:
            state["visible"] = False
            return
        try:
            root = tk.Tk()
            root.title("Snap-to-GMod — журнал")
            root.geometry("560x360")

            def on_close():
                state["visible"] = False
                root.destroy()
                # Галочка "Просмотр журнала" в трее иначе не сбрасывается
                # сама — pystray перерисовывает пункты меню при клике по
                # трею, а не при закрытии этого отдельного окна
                # крестиком/кнопкой "Закрыть".
                try:
                    icon.update_menu()
                except Exception:
                    pass

            root.protocol("WM_DELETE_WINDOW", on_close)
            top = tk.Frame(root)
            top.pack(fill="x", padx=8, pady=6)
            tk.Label(top, text="Журнал программы (обновляется автоматически)", font=("Segoe UI", 10, "bold")).pack(side="left")

            def copy_all():
                root.clipboard_clear()
                root.clipboard_append(text.get("1.0", "end-1c"))

            tk.Button(top, text="Закрыть", command=on_close).pack(side="right")
            tk.Button(top, text="Скопировать всё", command=copy_all).pack(side="right", padx=(0, 6))

            text = tk.Text(root, font=("Consolas", 9), bg="#111111", fg="#d4d4d4", wrap="none")
            text.pack(fill="both", expand=True, padx=8, pady=(0, 8))

            menu = tk.Menu(text, tearoff=0)
            menu.add_command(label="Копировать", command=lambda: text.event_generate("<<Copy>>"))
            menu.add_command(label="Выделить всё", command=lambda: text.tag_add("sel", "1.0", "end"))
            text.bind("<Button-3>", lambda e: menu.tk_popup(e.x_root, e.y_root))
            text.bind("<Control-a>", lambda e: (text.tag_add("sel", "1.0", "end"), "break")[-1])

            last_n = {"n": -1}

            def refresh():
                if not state.get("visible"):
                    root.destroy()
                    return
                # Пока у пользователя есть активное выделение в тексте — не
                # перерисовываем содержимое: раньше автообновление раз в
                # секунду делало text.delete()+insert() и сбрасывало
                # выделение прямо во время копирования, из-за чего Ctrl+C
                # ничего не копировал.
                if not text.tag_ranges("sel"):
                    lines = tail_log_lines(300)
                    if len(lines) != last_n["n"]:
                        last_n["n"] = len(lines)
                        at_bottom = text.yview()[1] >= 0.999
                        text.delete("1.0", tk.END)
                        text.insert(tk.END, "\n".join(lines))
                        if at_bottom:
                            text.see(tk.END)
                root.after(1000, refresh)

            refresh()
            root.mainloop()
        except Exception as e:
            logger.warning("Ошибка окна журнала: %s", e)
            state["visible"] = False

    threading.Thread(target=_window, daemon=True).start()


def start_mic_test(mic_test_state: dict, cfg: AppConfig, logger):
    if mic_test_state.get("active"):
        return
    mic_test_state["active"] = True
    mic_test_state["peak"] = 0.0
    logger.info("Запущен тест микрофона (игра не запускается).")

    def _window():
        try:
            import tkinter as tk
        except ImportError:
            mic_test_state["active"] = False
            return
        try:
            root = tk.Tk()
            root.title("Тест микрофона — Snap-to-GMod")
            root.attributes("-topmost", True)
            root.resizable(False, False)

            def on_close():
                mic_test_state["active"] = False
                root.destroy()

            root.protocol("WM_DELETE_WINDOW", on_close)
            tk.Label(root, text="Щёлкайте пальцами или хлопайте рядом с микрофоном.\n"
                                 "Это НЕ запускает игру — только показывает уровень громкости.",
                     padx=22, pady=12, justify="center", font=("Segoe UI", 10)).pack()
            canvas = tk.Canvas(root, width=300, height=22, bg="#222222", highlightthickness=0)
            canvas.pack(padx=22, pady=(0, 6))
            bar = canvas.create_rectangle(0, 0, 0, 22, fill="#4caf50", width=0)
            threshold_line = canvas.create_line(0, 0, 0, 22, fill="#ff4444", width=2)
            value_var = tk.StringVar(value="Пик: 0.000  /  порог 0.000")
            tk.Label(root, textvariable=value_var, font=("Segoe UI", 10)).pack(pady=(0, 4))
            tk.Button(root, text="Закрыть тест", command=on_close, padx=12, pady=5).pack(pady=(0, 14))

            def poll():
                if not mic_test_state.get("active"):
                    root.destroy()
                    return
                peak = mic_test_state.get("peak", 0.0)
                threshold = cfg.threshold
                scale = max(threshold * 1.6, 0.02)
                width = max(0, min(300, int(peak / scale * 300)))
                color = "#4caf50" if peak >= threshold else "#e6be28" if peak > threshold * 0.4 else "#555555"
                canvas.coords(bar, 0, 0, width, 22)
                canvas.itemconfig(bar, fill=color)
                x = min(300, int(threshold / scale * 300))
                canvas.coords(threshold_line, x, 0, x, 22)
                value_var.set(f"Пик: {peak:.3f}  /  порог {threshold:.3f}")
                root.after(100, poll)

            root.after(100, poll)
            root.mainloop()
        except Exception as e:
            logger.warning("Ошибка окна теста микрофона: %s", e)
            mic_test_state["active"] = False

    threading.Thread(target=_window, daemon=True).start()


def start_calibration(calibration_state: dict, logger):
    if calibration_state.get("active"):
        return
    calibration_state.update(active=True, peaks=[], last_capture=0.0, target=5)
    logger.info("Начата калибровка чувствительности.")

    def _window():
        try:
            import tkinter as tk
        except ImportError:
            return
        try:
            root = tk.Tk()
            root.title("Калибровка чувствительности — Snap-to-GMod")
            root.attributes("-topmost", True)
            root.resizable(False, False)
            root.protocol("WM_DELETE_WINDOW", lambda: None)
            tk.Label(root, text="Щёлкните пальцами 5 раз рядом с микрофоном,\nс паузой около секунды между щелчками.",
                     padx=24, pady=14, justify="center", font=("Segoe UI", 10)).pack()
            progress_var = tk.StringVar(value="Поймано щелчков: 0 / 5")
            tk.Label(root, textvariable=progress_var, font=("Segoe UI", 13, "bold"), pady=4).pack()

            def do_cancel():
                calibration_state["active"] = False
                logger.info("Калибровка отменена пользователем.")
                root.destroy()

            tk.Button(root, text="Отменить калибровку", command=do_cancel, padx=12, pady=5).pack(pady=(4, 14))

            def poll():
                if not calibration_state.get("active"):
                    root.destroy()
                    return
                progress_var.set(f"Поймано щелчков: {len(calibration_state['peaks'])} / {calibration_state['target']}")
                root.after(150, poll)

            root.after(150, poll)
            root.mainloop()
        except Exception as e:
            logger.warning("Ошибка окна калибровки: %s", e)

    threading.Thread(target=_window, daemon=True).start()


_VOICE_GUIDE_TEXT = """САМЫЙ ПРОСТОЙ СПОСОБ

Нажми кнопку «Запустить мастер настройки» ниже — программа сама скачает
модель распознавания речи и сама сохранит все 3 картинки-шаблона: просто
следуй подсказкам на экране и обводи мышью рамку вокруг того, что попросят.
Ничего скачивать/переименовывать/копировать вручную не нужно.

Слово-триггер по умолчанию — «удача». Поменять его можно в этом же
подменю трея (пункт «Слово-триггер»).

──────────────────────────────────────────────

ЕСЛИ ЧТО-ТО НЕ ПОЛУЧАЕТСЯ — подробности для ручной настройки:

1) Зависимости (в ТОМ ЖЕ venv/python, из которого потом собирается .exe!):
       pip install -r requirements-voice.txt
   Если программа запущена как .exe, а не как python main.py — этого
   недостаточно, .exe нужно ПЕРЕСОБРАТЬ после установки зависимостей:
       pyinstaller SnapToGMod.spec
   Частая причина того, что при слове-триггере просто открывается пауза
   и больше ничего не происходит: не установлен или не попал в сборку
   .exe пакет opencv-python (модуль cv2). Кнопка «Запустить диагностику»
   ниже проверяет это отдельно.

2) Модель и шаблоны можно положить и вручную — путь открывает кнопка
   «Открыть папку модели/шаблонов» ниже. Про требуемую структуру папок
   написано в README.md рядом с программой.

3) Порог совпадения (confidence, по умолчанию 0.7) — если шаблон почти
   совпадает, но не находится, попробуйте понизить до 0.5-0.6 в
   config.json (voice_match_confidence).
"""


def open_voice_guide(cfg: AppConfig, voice_engine: Optional["voice_select.VoiceSelectEngine"],
                      guide_state: dict, save_config: Callable[[], None], char_manager_state: dict, logger):
    if guide_state.get("visible"):
        return
    guide_state["visible"] = True

    def _window():
        try:
            import tkinter as tk
        except ImportError:
            guide_state["visible"] = False
            return
        try:
            root = tk.Tk()
            root.title("Голосовой выбор персонажа — диагностика и гайд")
            root.geometry("640x640")

            def on_close():
                guide_state["visible"] = False
                root.destroy()

            root.protocol("WM_DELETE_WINDOW", on_close)

            nb_frame = tk.Frame(root)
            nb_frame.pack(fill="both", expand=True, padx=8, pady=8)

            wizard_row = tk.Frame(nb_frame)
            wizard_row.pack(fill="x", pady=(0, 6))

            def _open_manager():
                open_character_manager(cfg, voice_engine, save_config, logger, char_manager_state, guide_state)

            tk.Button(wizard_row, text="▶ Открыть менеджер персонажей", command=_open_manager,
                      padx=10, pady=6, bg="#4caf50", fg="white", font=("Segoe UI", 9, "bold")).pack(fill="x")

            guide_text = tk.Text(nb_frame, font=("Segoe UI", 9), wrap="word", height=12)
            guide_text.insert("1.0", _VOICE_GUIDE_TEXT)
            guide_text.configure(state="disabled")
            guide_text.pack(fill="both", expand=True)

            tk.Label(nb_frame, text="Статус: обновляется автоматически", font=("Segoe UI", 8), fg="#666").pack(
                anchor="w", pady=(4, 0))
            status_var = tk.StringVar(value="—")
            tk.Label(nb_frame, textvariable=status_var, font=("Segoe UI", 9, "bold")).pack(anchor="w")

            diag_frame = tk.LabelFrame(nb_frame, text="Диагностика", padx=8, pady=6)
            diag_frame.pack(fill="both", expand=True, pady=(8, 0))

            diag_text = tk.Text(diag_frame, font=("Consolas", 9), height=12, wrap="word",
                                 bg="#111111", fg="#d4d4d4")
            diag_text.pack(fill="both", expand=True)

            def line(msg, ok=None):
                mark = "  " if ok is None else ("[OK] " if ok else "[!!] ")
                diag_text.insert(tk.END, mark + msg + "\n")

            def run_diagnostics():
                diag_text.delete("1.0", tk.END)
                try:
                    report = voice_select.run_diagnostics(cfg)
                except Exception as e:
                    line(f"Ошибка диагностики: {e}", ok=False)
                    return
                line(f"Библиотеки: {'все на месте' if report['dependencies_available'] else 'не хватает: ' + ', '.join(report['missing_dependencies'])}",
                     ok=report["dependencies_available"])
                line(f"OpenCV (cv2): {report['cv2_msg']}", ok=report["cv2_ok"])
                if report["model_problem"]:
                    line(f"Модель: {report['model_problem']}", ok=False)
                else:
                    line(f"Модель: найдена в {report['model_dir']}", ok=True)
                res = report["screen_resolution"]
                line(f"Разрешение экрана сейчас: {res[0]}x{res[1]}" if res else "Разрешение экрана: не удалось определить")
                for t in report["templates"]:
                    if not t["exists"]:
                        line(f"Общий шаблон {t['name']}: отсутствует ({t.get('error', 'нет файла')})", ok=False)
                    else:
                        size_txt = f", {t['size'][0]}x{t['size'][1]}px" if t.get("size") else ""
                        line(f"Общий шаблон {t['name']}: есть{size_txt}", ok=True)
                if not report["characters"]:
                    line("Персонажей не заведено — откройте менеджер персонажей выше.", ok=False)
                for c in report["characters"]:
                    if not c["exists"]:
                        line(f"Персонаж «{c['character_name']}» (триггер «{c['trigger_word']}»): нет иконки ({c.get('error', 'нет файла')})", ok=False)
                    else:
                        size_txt = f", {c['size'][0]}x{c['size'][1]}px" if c.get("size") else ""
                        line(f"Персонаж «{c['character_name']}» (триггер «{c['trigger_word']}»): иконка есть{size_txt}", ok=True)
                line(f"Порог похожести {report['similarity_threshold']}, confidence {report['match_confidence']}")
                line("Готово. Если игра сейчас открыта на экране выбора персонажа/в меню — "
                     "используйте кнопки «Проверить на экране» ниже для каждого шаблона.")

            btn_row = tk.Frame(diag_frame)
            btn_row.pack(fill="x", pady=(6, 0))
            tk.Button(btn_row, text="Запустить диагностику", command=run_diagnostics, padx=10, pady=4).pack(side="left")
            def _open_voice_dir():
                try:
                    import os
                    os.startfile(cfg_mod.VOICE_DIR)
                except OSError as e:
                    logger.warning("Не удалось открыть папку голосового модуля: %s", e)

            tk.Button(btn_row, text="Открыть папку модели/шаблонов", padx=10, pady=4,
                      command=_open_voice_dir).pack(side="left", padx=(6, 0))

            live_row = tk.Frame(diag_frame)
            live_row.pack(fill="x", pady=(6, 0))
            tk.Label(live_row, text="Проверить шаблон на экране прямо сейчас:", font=("Segoe UI", 8)).pack(anchor="w")
            live_btns = tk.Frame(live_row)
            live_btns.pack(fill="x")

            def check_template(name):
                def _do():
                    found, msg = voice_select.try_locate_template(name, cfg.voice_match_confidence)
                    line(f"{name}: {msg}", ok=found)
                    diag_text.see(tk.END)
                return _do

            live_names = list(voice_select.TEMPLATE_FILES) + [c.icon_file for c in cfg.voice_characters if c.icon_file]
            for tpl_name in live_names:
                tk.Button(live_btns, text=tpl_name, command=check_template(tpl_name), padx=6, pady=3).pack(
                    side="left", padx=(0, 4), pady=(2, 0))

            trig_frame = tk.LabelFrame(nb_frame, text="Проверка слова-триггера (без микрофона)", padx=8, pady=6)
            trig_frame.pack(fill="x", pady=(8, 0))
            trig_row = tk.Frame(trig_frame)
            trig_row.pack(fill="x")
            tk.Label(trig_row, text="Текст:").pack(side="left")
            trig_entry = tk.Entry(trig_row, width=30)
            trig_entry.pack(side="left", padx=(4, 8))
            trig_result = tk.StringVar(value="")
            tk.Label(trig_row, textvariable=trig_result, font=("Segoe UI", 9, "bold")).pack(side="left")

            def on_trig_change(*_):
                text = trig_entry.get().strip().lower()
                if not text:
                    trig_result.set("")
                    return
                if not cfg.voice_characters:
                    trig_result.set("персонажей не заведено")
                    return
                match = voice_select.match_character(text, cfg.voice_characters, cfg.voice_similarity_threshold)
                if match is not None:
                    trig_result.set(f"сработает: «{match.name}»")
                    return
                best_ratio = max(
                    (voice_select.trigger_similarity(text, c.trigger_word) for c in cfg.voice_characters),
                    default=0.0,
                )
                trig_result.set(f"НЕ сработает (лучшая похожесть {best_ratio:.2f})")

            trig_entry.bind("<KeyRelease>", on_trig_change)

            def refresh_status():
                if not guide_state.get("visible"):
                    root.destroy()
                    return
                if voice_engine is not None:
                    status_var.set(voice_engine.status_text)
                else:
                    status_var.set("модуль недоступен")
                root.after(1000, refresh_status)

            refresh_status()
            run_diagnostics()
            root.mainloop()
        except Exception as e:
            logger.warning("Ошибка окна диагностики голосового выбора: %s", e)
            guide_state["visible"] = False

    threading.Thread(target=_window, daemon=True).start()


def capture_screen_region(instruction: str) -> Optional[tuple[int, int, int, int]]:
    """БЛОКИРУЕТ вызывающий поток. Показывает полупрозрачное окно на весь
    экран (сквозь него видно, что происходит в игре) — пользователь тянет
    мышью рамку вокруг нужного элемента и отпускает кнопку. Возвращает
    (left, top, width, height) в пикселях экрана, либо None, если отменено
    (Esc) или рамка не была реально протянута."""
    result = {"rect": None}
    done = threading.Event()

    def _run():
        try:
            import tkinter as tk
            root = tk.Tk()
            root.attributes("-fullscreen", True)
            root.attributes("-alpha", 0.35)
            root.attributes("-topmost", True)
            root.configure(bg="black")
            root.config(cursor="cross")

            canvas = tk.Canvas(root, bg="black", highlightthickness=0)
            canvas.pack(fill="both", expand=True)
            canvas.create_text(
                root.winfo_screenwidth() // 2, 40,
                text=instruction + "\n(потяните мышью рамку вокруг нужного места; Esc — отмена)",
                fill="white", font=("Segoe UI", 14, "bold"), justify="center",
            )

            start: dict = {}
            rect_id = {"id": None}

            def on_press(event):
                start["x"], start["y"] = event.x_root, event.y_root
                rect_id["id"] = canvas.create_rectangle(event.x, event.y, event.x, event.y,
                                                          outline="#4caf50", width=3)

            def on_drag(event):
                if rect_id["id"] is not None and "x" in start:
                    x0 = start["x"] - root.winfo_rootx()
                    y0 = start["y"] - root.winfo_rooty()
                    canvas.coords(rect_id["id"], x0, y0, event.x, event.y)

            def on_release(event):
                if "x" not in start:
                    return
                x1, y1 = start["x"], start["y"]
                x2, y2 = event.x_root, event.y_root
                left, right = sorted((x1, x2))
                top, bottom = sorted((y1, y2))
                w, h = right - left, bottom - top
                root.destroy()
                if w >= 8 and h >= 8:
                    result["rect"] = (left, top, w, h)

            def on_escape(_event):
                root.destroy()

            canvas.bind("<ButtonPress-1>", on_press)
            canvas.bind("<B1-Motion>", on_drag)
            canvas.bind("<ButtonRelease-1>", on_release)
            root.bind("<Escape>", on_escape)
            root.mainloop()
            time.sleep(0.2)  # дать окну реально исчезнуть с экрана перед скриншотом
        except Exception:
            pass
        finally:
            done.set()

    threading.Thread(target=_run, daemon=True).start()
    done.wait(timeout=180)
    return result["rect"]


def _ensure_shared_voice_templates(logger) -> bool:
    """Пункт меню паузы «Выбрать персонажа» и кнопка подтверждения выбора
    одинаковые для всех персонажей — их достаточно снять один раз. Если они
    уже есть на диске, ничего не спрашивает и сразу возвращает True."""
    menu_path = cfg_mod.VOICE_TEMPLATES_DIR / voice_select.TEMPLATE_MENU_ITEM
    button_path = cfg_mod.VOICE_TEMPLATES_DIR / voice_select.TEMPLATE_SELECT_BUTTON
    if menu_path.exists() and button_path.exists():
        return True

    if not voice_select.model_is_present():
        if not ask_yes_no(
            "Настройка голосового выбора",
            "Сейчас скачаю модель распознавания речи (~45 МБ, нужен интернет).\n\nНачать?",
        ):
            return False
        ok, err = voice_select.download_model(logger)
        if not ok:
            system.show_error_box("Настройка голосового выбора", f"Не удалось скачать модель:\n{err}")
            return False

    steps = [
        (
            "Общие элементы — шаг 1 из 2",
            "Открой Garry's Mod, зайди на сервер и нажми ESC — должно открыться меню паузы.\n\n"
            "Когда меню паузы видно на экране, нажми «ОК» — после этого обведи мышью рамку "
            "вокруг пункта «Выбрать персонажа».",
            voice_select.TEMPLATE_MENU_ITEM,
            "Обведи рамкой пункт «Выбрать персонажа»",
        ),
        (
            "Общие элементы — шаг 2 из 2",
            "Теперь по-настоящему кликни мышью на «Выбрать персонажа» в меню — должен открыться "
            "экран выбора персонажа. Наведи курсор на любую иконку так, чтобы появилась кнопка "
            "«ВЫБРАТЬ ПЕРСОНАЖА» (пока НЕ нажимай её).\n\nКогда кнопка видна на экране, нажми "
            "«ОК» — после этого обведи рамкой саму кнопку.",
            voice_select.TEMPLATE_SELECT_BUTTON,
            "Обведи рамкой кнопку «ВЫБРАТЬ ПЕРСОНАЖА»",
        ),
    ]
    for title, message, filename, drag_instruction in steps:
        system.show_info_box(title, message)
        rect = capture_screen_region(drag_instruction)
        if rect is None:
            system.show_error_box("Настройка голосового выбора", "Отменено — рамка не была обведена.")
            return False
        ok, note = voice_select.save_template_from_region(filename, rect)
        if not ok:
            system.show_error_box("Настройка голосового выбора", f"Не удалось сохранить «{filename}»:\n{note}")
            return False
        logger.info("Общий шаблон %s сохранён (%s).", filename, note)
    return True


def open_character_manager(cfg: AppConfig, voice_engine: Optional["voice_select.VoiceSelectEngine"],
                            save_config: Callable[[], None], logger, state: dict, guide_state: dict):
    """Система создания и управления персонажами голосового выбора — одно
    окно вместо нескольких разрозненных пунктов меню трея (было: отдельный
    мастер настройки только под одного захардкоженного персонажа + отдельный
    пункт «слово-триггер», который тоже относился только к нему одному).
    Здесь можно добавить сколько угодно персонажей, у каждого — своё слово-
    триггер и своя иконка, переснять иконку заново или удалить персонажа."""
    if state.get("visible"):
        return
    state["visible"] = True

    def _window():
        try:
            import tkinter as tk
        except ImportError:
            state["visible"] = False
            return
        try:
            root = tk.Tk()
            root.title("Персонажи — голосовой выбор")
            root.geometry("560x520")

            def on_close():
                state["visible"] = False
                root.destroy()
            root.protocol("WM_DELETE_WINDOW", on_close)

            tk.Label(
                root, justify="left", font=("Segoe UI", 8), fg="#555",
                text=(
                    "Каждый персонаж — своё слово-триггер и своя иконка с экрана выбора "
                    "персонажа.\nСлово-триггер должно быть уникальным: если у двух персонажей "
                    "общий талант (например,\n«Удача»), добавьте к фразе имя — «удача нагито», "
                    "«удача макото» — иначе второй\nперсонаж с тем же словом никогда не будет "
                    "выбран голосом.\nПодробный пошаговый гайд — кнопка «Гайд по настройке» "
                    "ниже. Уведомление Windows и звук при выборе\nперсонажа можно настроить "
                    "отдельно для каждого — кнопки внизу."
                ),
            ).pack(fill="x", padx=8, pady=(8, 4), anchor="w")

            list_frame = tk.Frame(root)
            list_frame.pack(fill="both", expand=True, padx=8)
            listbox = tk.Listbox(list_frame, font=("Segoe UI", 10), activestyle="dotbox")
            listbox.pack(side="left", fill="both", expand=True)
            scrollbar = tk.Scrollbar(list_frame, command=listbox.yview)
            scrollbar.pack(side="right", fill="y")
            listbox.config(yscrollcommand=scrollbar.set)

            def refresh_list():
                listbox.delete(0, tk.END)
                if not cfg.voice_characters:
                    listbox.insert(tk.END, "(персонажей пока нет — нажми «Добавить персонажа»)")
                    return
                for c in cfg.voice_characters:
                    has_icon = bool(c.icon_file) and (cfg_mod.VOICE_TEMPLATES_DIR / c.icon_file).exists()
                    mark = "иконка есть" if has_icon else "НЕТ ИКОНКИ"
                    sound_mark = " 🔊свой звук" if c.sound_file else ""
                    listbox.insert(tk.END, f"{c.name}  —  триггер «{c.trigger_word}»  [{mark}]{sound_mark}")

            def selected_character():
                if not cfg.voice_characters:
                    return None
                sel = listbox.curselection()
                if not sel or sel[0] >= len(cfg.voice_characters):
                    return None
                return cfg.voice_characters[sel[0]]

            def reload_engine():
                cfg.voice_select_enabled = True
                save_config()
                if voice_engine is not None:
                    voice_engine.load_async(logger)

            def add_character():
                if not voice_select.DEPENDENCIES_AVAILABLE:
                    system.show_error_box(
                        "Добавление персонажа",
                        "Не установлены нужные библиотеки: " + ", ".join(voice_select.MISSING_DEPENDENCIES) +
                        ".\nВыполните: pip install -r requirements-voice.txt",
                    )
                    return
                name = ask_text("Новый персонаж", "Имя персонажа (только для себя, в игре нигде не показывается):")
                if not name or not name.strip():
                    return
                name = name.strip()
                prompt = (
                    f"Слово-триггер для «{name}» — то, что нужно сказать вслух, чтобы выбрать его "
                    "(например, талант персонажа):"
                )
                trigger = None
                while True:
                    trigger = ask_text("Новый персонаж", prompt)
                    if not trigger or not trigger.strip():
                        return
                    conflict = voice_select.trigger_word_conflict(cfg, trigger)
                    if conflict is None:
                        break
                    # Слова-триггеры должны быть уникальными — иначе при
                    # одинаковой похожести всегда выигрывает первый по
                    # порядку персонаж, а второй с тем же словом становится
                    # недостижим голосом (см. docstring match_character в
                    # voice_select.py). Просим другую фразу, например с
                    # добавлением имени персонажа.
                    prompt = (
                        f"Слово-триггер «{trigger.strip()}» уже используется персонажем "
                        f"«{conflict.name}». Слова-триггеры должны быть уникальными — "
                        f"придумайте отдельную фразу, например «{trigger.strip()} {name}»:"
                    )
                if not _ensure_shared_voice_templates(logger):
                    return
                system.show_info_box(
                    "Добавление персонажа",
                    f"Открой экран выбора персонажа и найди иконку «{name}» в избранном.\n\n"
                    "Когда она видна на экране, нажми «ОК» — после этого обведи её рамкой.",
                )
                rect = capture_screen_region(f"Обведи рамкой иконку «{name}»")
                if rect is None:
                    system.show_error_box("Добавление персонажа", "Отменено — рамка не была обведена.")
                    return
                try:
                    character = voice_select.add_character(cfg, name, trigger.strip())
                except ValueError as e:
                    system.show_error_box("Добавление персонажа", str(e))
                    return
                ok, note = voice_select.save_template_from_region(character.icon_file, rect)
                if not ok:
                    voice_select.remove_character(cfg, character, delete_icon_file=False)
                    system.show_error_box("Добавление персонажа", f"Не удалось сохранить иконку:\n{note}")
                    return
                reload_engine()
                logger.info("Персонаж «%s» добавлен (слово-триггер «%s»).", character.name, character.trigger_word)
                refresh_list()

            def rename_trigger():
                character = selected_character()
                if character is None:
                    return
                prompt = f"Слово-триггер для «{character.name}».\nСейчас: {character.trigger_word}"
                while True:
                    text = ask_text("Слово-триггер", prompt)
                    if text is None or not text.strip():
                        return
                    conflict = voice_select.trigger_word_conflict(cfg, text, exclude_slug=character.slug)
                    if conflict is None:
                        break
                    # Как и при добавлении персонажа — слова-триггеры обязаны
                    # быть уникальными, иначе один из двух персонажей с
                    # одинаковым словом станет недостижим голосом.
                    prompt = (
                        f"Слово-триггер «{text.strip()}» уже используется персонажем "
                        f"«{conflict.name}». Слова-триггеры должны быть уникальными — "
                        f"придумайте отдельную фразу, например «{text.strip()} {character.name}»:"
                    )
                character.trigger_word = text.strip().lower()
                save_config()
                logger.info("Слово-триггер персонажа «%s» изменено на «%s».", character.name, character.trigger_word)
                refresh_list()

            def recapture_icon():
                character = selected_character()
                if character is None:
                    return
                system.show_info_box(
                    "Переснять иконку",
                    f"Открой экран выбора персонажа так, чтобы была видна иконка «{character.name}».\n\n"
                    "Когда видна — нажми «ОК» и обведи её рамкой.",
                )
                rect = capture_screen_region(f"Обведи рамкой иконку «{character.name}»")
                if rect is None:
                    return
                ok, note = voice_select.save_template_from_region(character.icon_file, rect)
                if not ok:
                    system.show_error_box("Переснять иконку", f"Не удалось сохранить:\n{note}")
                    return
                refresh_list()

            def set_notify_message():
                character = selected_character()
                if character is None:
                    return
                text = ask_text(
                    "Уведомление Windows",
                    f"Текст уведомления Windows при выборе «{character.name}» голосом.\n"
                    "Плейсхолдер {name} подставит имя персонажа.\n"
                    "Оставь поле пустым и нажми «ОК», чтобы вернуть текст по умолчанию.\n\n"
                    f"Сейчас: {character.notify_text()}",
                )
                if text is None:
                    return
                character.notify_message = text.strip()
                save_config()
                logger.info("Текст уведомления персонажа «%s» изменён на «%s».",
                            character.name, character.notify_text())
                refresh_list()

            def set_character_sound():
                character = selected_character()
                if character is None:
                    return
                from tkinter import filedialog
                # ВАЖНО: не использовать ask_open_file() здесь — оно создаёт
                # СВОЙ отдельный tk.Tk() в отдельном потоке, а это окно
                # менеджера персонажей уже само крутит tk.mainloop() в своём
                # потоке. Два живых Tk-корня в разных потоках одновременно —
                # именно то, из-за чего программа падала при нажатии «Свой
                # звук...». Правильно — открыть диалог через УЖЕ существующий
                # root этого окна, в том же потоке, без нового Tk()/потока.
                path = filedialog.askopenfilename(
                    parent=root,
                    title=f"Звук при выборе «{character.name}» голосом",
                    filetypes=[("WAV файлы", "*.wav"), ("Все файлы", "*.*")],
                )
                if not path:
                    return
                character.sound_file = path
                save_config()
                logger.info("Звук персонажа «%s» изменён на: %s", character.name, path)
                refresh_list()

            def clear_character_sound():
                character = selected_character()
                if character is None:
                    return
                character.sound_file = ""
                save_config()
                logger.info("Звук персонажа «%s» сброшен на стандартный.", character.name)
                refresh_list()

            def test_character_sound():
                character = selected_character()
                if character is None:
                    return
                sound.play_character_sound(cfg, character, logger)

            def delete_character():
                character = selected_character()
                if character is None:
                    return
                if not ask_yes_no("Удалить персонажа", f"Удалить «{character.name}» из списка?"):
                    return
                voice_select.remove_character(cfg, character)
                save_config()
                logger.info("Персонаж «%s» удалён.", character.name)
                refresh_list()

            def open_guide():
                open_voice_guide(cfg, voice_engine, guide_state, save_config, state, logger)

            btn_row0 = tk.Frame(root)
            btn_row0.pack(fill="x", padx=8, pady=(8, 0))
            tk.Button(btn_row0, text="📖 Гайд по настройке...", command=open_guide, padx=6, pady=4).pack(fill="x")

            btn_row1 = tk.Frame(root)
            btn_row1.pack(fill="x", padx=8, pady=(6, 2))
            tk.Button(btn_row1, text="➕ Добавить персонажа...", command=add_character, padx=10, pady=6,
                      bg="#4caf50", fg="white", font=("Segoe UI", 9, "bold")).pack(fill="x")

            btn_row2 = tk.Frame(root)
            btn_row2.pack(fill="x", padx=8, pady=(2, 2))
            tk.Button(btn_row2, text="Изменить слово-триггер", command=rename_trigger, padx=6, pady=4).pack(side="left")
            tk.Button(btn_row2, text="Переснять иконку", command=recapture_icon, padx=6, pady=4).pack(
                side="left", padx=(6, 0))
            tk.Button(btn_row2, text="Удалить", command=delete_character, padx=6, pady=4).pack(
                side="left", padx=(6, 0))

            btn_row3 = tk.Frame(root)
            btn_row3.pack(fill="x", padx=8, pady=(2, 8))
            tk.Button(btn_row3, text="Текст уведомления...", command=set_notify_message, padx=6, pady=4).pack(side="left")
            tk.Button(btn_row3, text="Свой звук...", command=set_character_sound, padx=6, pady=4).pack(
                side="left", padx=(6, 0))
            tk.Button(btn_row3, text="Сбросить звук", command=clear_character_sound, padx=6, pady=4).pack(
                side="left", padx=(6, 0))
            tk.Button(btn_row3, text="🔊 Проверить звук", command=test_character_sound, padx=6, pady=4).pack(
                side="left", padx=(6, 0))

            refresh_list()
            root.mainloop()
        except Exception as e:
            logger.warning("Ошибка окна менеджера персонажей: %s", e)
        finally:
            state["visible"] = False

    threading.Thread(target=_window, daemon=True).start()


def show_update_window(latest_tag: str, latest_url: str, asset_url: Optional[str], logger):
    def _window():
        try:
            import tkinter as tk
            import webbrowser
        except ImportError:
            system.show_info_box("Snap-to-GMod — доступно обновление",
                                  f"Вышла новая версия: {latest_tag}\nСкачать: {latest_url}")
            return
        try:
            root = tk.Tk()
            root.title("Snap-to-GMod — обновление")
            root.attributes("-topmost", True)
            root.resizable(False, False)
            tk.Label(root, text=f"Доступна версия {latest_tag}", font=("Segoe UI", 12, "bold"), padx=24, pady=(16, 2)).pack()
            tk.Label(root, text=f"У вас установлена: {cfg_mod.APP_VERSION}", font=("Segoe UI", 9), fg="#666").pack(pady=(0, 12))
            status_var = tk.StringVar(value="")
            tk.Label(root, textvariable=status_var, font=("Segoe UI", 9), fg="#0a7d2c").pack()
            btns = tk.Frame(root, padx=16, pady=14)
            btns.pack()
            tk.Button(btns, text="Скачать страницу релиза", command=lambda: webbrowser.open(latest_url), padx=10, pady=5).pack(side="left", padx=4)

            can_auto = bool(asset_url) and cfg_mod.FROZEN and platform.system() == "Windows"
            if can_auto:
                def do_auto_update():
                    import os
                    status_var.set("Скачивание обновления...")
                    root.update_idletasks()
                    ok, msg = updates.apply_auto_update(asset_url, logger)
                    if ok:
                        status_var.set("Готово — программа перезапускается...")
                        root.update_idletasks()
                        root.after(800, lambda: os._exit(0))
                    else:
                        status_var.set(f"Ошибка: {msg}")

                tk.Button(btns, text="Обновить и перезапустить", command=do_auto_update, padx=10, pady=5, bg="#5865F2", fg="white").pack(side="left", padx=4)

            tk.Button(root, text="Позже", command=root.destroy, padx=10, pady=3).pack(pady=(0, 12))
            root.mainloop()
        except Exception as e:
            logger.warning("Ошибка окна обновления: %s", e)

    threading.Thread(target=_window, daemon=True).start()


# ==================== ДЕЙСТВИЯ НАД СЕРВЕРАМИ (диалоги) ====================

def add_server_dialog(cfg: AppConfig, save_config: Callable[[], None], logger):
    def _flow():
        text = ask_text("Новый сервер",
                         "Введите одной строкой: Имя сервера IP:порт\nНапример: Shinri Trial 80.66.82.229:27103")
        if not text or " " not in text.strip():
            if text:
                system.show_error_box("Snap-to-GMod", "Формат: Имя сервера IP:порт")
            return
        text = text.strip()
        name, addr = text.rsplit(" ", 1)
        name = name.strip()
        if ":" not in addr:
            system.show_error_box("Snap-to-GMod", "Не найден порт после «:». Формат: Имя сервера IP:порт")
            return
        ip, port = (p.strip() for p in addr.rsplit(":", 1))
        if not name or not ip or not port.isdigit():
            system.show_error_box("Snap-to-GMod", "Некорректные имя, IP или порт сервера.")
            return
        servers_mod.add_server(cfg, name, ip, port)
        save_config()
        logger.info("Добавлен сервер: %s (%s:%s)", name, ip, port)

    threading.Thread(target=_flow, daemon=True).start()


def clear_servers_dialog(cfg: AppConfig, save_config: Callable[[], None], logger):
    """Оставляет только избранные сервера + текущий активный, остальные
    удаляет из списка — на случай, если список раздулся (например, из-за
    старого импорта Steam Favorites)."""
    removed = servers_mod.clear_non_favorite_servers(cfg)
    save_config()
    logger.info("Список серверов очищен: удалено %s.", removed)
    system.show_info_box("Snap-to-GMod",
                          f"Удалено серверов: {removed}\nОставлены только избранные и текущий активный.")


def search_server_dialog(cfg: AppConfig, select_server: Callable[[str], None]):
    def _flow():
        query = ask_text("Найти сервер", "Введите часть имени или IP сервера:")
        if query is None:
            return
        matches = servers_mod.search_servers(cfg, query)
        if not matches:
            system.show_info_box("Snap-to-GMod", "Совпадений не найдено.")
            return
        if len(matches) == 1:
            select_server(matches[0].name)
            system.show_info_box("Snap-to-GMod", f"Выбран сервер: {matches[0].name}")
            return
        listing = "\n".join(f"- {s.name} ({s.address})" for s in matches[:20])
        system.show_info_box("Snap-to-GMod — найдено несколько серверов",
                              f"{listing}\n\nУточните запрос или выберите сервер из подменю «Сервер».")

    threading.Thread(target=_flow, daemon=True).start()


def check_active_server_dialog(cfg: AppConfig, logger):
    def _flow():
        server = cfg.active_server()
        info = servers_mod.check_server_availability(server)
        if info["reachable"]:
            players = (f"{info['players']}/{info['max_players']}" if info["players"] is not None else "?")
            ping = f"{info['ping_ms']:.0f} мс" if info["ping_ms"] is not None else "?"
            msg = f"Сервер «{server.name}» доступен.\nPing: {ping}\nИгроков: {players}"
        else:
            msg = f"Сервер «{server.name}» не отвечает на ping и запрос движка."
        logger.info("Проверка доступности «%s»: %s", server.name, "доступен" if info["reachable"] else "недоступен")
        system.show_info_box("Snap-to-GMod — проверка сервера", msg)

    threading.Thread(target=_flow, daemon=True).start()


def choose_sound_file_dialog(cfg: AppConfig, event: str, save_config: Callable[[], None], logger):
    def _flow():
        path = ask_open_file(f"Выберите звук для события «{event}»", [("WAV файлы", "*.wav"), ("Все файлы", "*.*")])
        if path:
            setattr(cfg.sounds, event, path)
            save_config()
            logger.info("Выбран звук для события «%s»: %s", event, path)

    threading.Thread(target=_flow, daemon=True).start()


# ==================== СБОРКА МЕНЮ ====================

def build_menu(
    cfg: AppConfig,
    logger,
    on_quit,
    pause_event: threading.Event,
    icon_state_holder: list,
    calibration_state: dict,
    mic_test_state: dict,
    reregister_hotkeys: Callable[[], bool],
    save_config: Callable[[], None],
    voice_engine: Optional["voice_select.VoiceSelectEngine"] = None,
):
    def not_calibrating(item=None):
        return not calibration_state.get("active")

    def not_testing_mic(item=None):
        return not mic_test_state.get("active")

    def set_sensitivity(value):
        def _handler(icon, item):
            cfg.threshold = value
            save_config()
            logger.info("Чувствительность изменена, порог = %s", value)
        return _handler

    def sensitivity_checked(value):
        return lambda item: abs(cfg.threshold - value) < 1e-6

    sensitivity_items = [
        pystray.MenuItem(label, set_sensitivity(value), radio=True,
                          checked=sensitivity_checked(value), enabled=not_calibrating)
        for label, value in SENSITIVITY_PRESETS
    ]

    def toggle_adaptive(icon, item):
        cfg.adaptive_sensitivity = not cfg.adaptive_sensitivity
        save_config()
        logger.info("Адаптивная чувствительность: %s", "вкл" if cfg.adaptive_sensitivity else "выкл")

    def do_start_calibration(icon, item):
        start_calibration(calibration_state, logger)

    def do_test_mic(icon, item):
        start_mic_test(mic_test_state, cfg, logger)

    detection_menu = pystray.Menu(
        *sensitivity_items,
        pystray.MenuItem("Автоматическая чувствительность (подстраивается под фон)", toggle_adaptive,
                          checked=lambda item: cfg.adaptive_sensitivity),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Откалибровать по 5 щелчкам...", do_start_calibration, enabled=not_calibrating),
        pystray.MenuItem("Тест микрофона (без запуска игры)...", do_test_mic, enabled=not_testing_mic),
    )

    def select_device(index):
        def _handler(icon, item):
            cfg.input_device_index = index
            save_config()
            name = "системный по умолчанию" if index is None else dict(audio.list_input_devices()).get(index, index)
            logger.info("Микрофон изменён: %s", name)
            system.show_info_box("Snap-to-GMod", f"Микрофон изменён: {name}\nПерезапустите программу, чтобы применить.")
        return _handler

    def device_checked(index):
        return lambda item: cfg.input_device_index == index

    def mic_menu_items():
        yield pystray.MenuItem("Системный по умолчанию", select_device(None), radio=True, checked=device_checked(None))
        yield pystray.Menu.SEPARATOR
        for index, name in audio.list_input_devices():
            yield pystray.MenuItem(f"{index}: {name}"[:60], select_device(index), radio=True, checked=device_checked(index))

    mic_menu = pystray.Menu(mic_menu_items)

    def set_countdown_manual(icon, item):
        def _flow():
            current = int(cfg.countdown_seconds or 0)
            text = ask_text("Обратный отсчёт", f"Введите время в секундах (0 = выключен).\nСейчас: {current} сек")
            if text is None:
                return
            text = text.strip()
            if not text.isdigit():
                system.show_error_box("Snap-to-GMod", "Введите целое число секунд (0 или больше).")
                return
            cfg.countdown_seconds = max(0, min(600, int(text)))
            save_config()
            logger.info("Обратный отсчёт перед запуском: %s",
                        "выключен" if cfg.countdown_seconds == 0 else f"{cfg.countdown_seconds} сек")
        threading.Thread(target=_flow, daemon=True).start()

    def countdown_label(item=None):
        state = "выключен" if cfg.countdown_seconds == 0 else f"{cfg.countdown_seconds} сек"
        return f"Обратный отсчёт: {state} (изменить...)"

    def toggle_sound(icon, item):
        cfg.sound_enabled = not cfg.sound_enabled
        save_config()

    def reset_sound(event):
        def _handler(icon, item):
            setattr(cfg.sounds, event, None)
            save_config()
            logger.info("Звук для события «%s» сброшен на стандартный.", event)
        return _handler

    def choose_sound(event):
        return lambda icon, item: choose_sound_file_dialog(cfg, event, save_config, logger)

    def toggle_notify(icon, item):
        cfg.notify_enabled = not cfg.notify_enabled
        save_config()

    sound_menu = pystray.Menu(
        pystray.MenuItem("Звук при срабатывании (общий выключатель)", toggle_sound,
                          checked=lambda item: cfg.sound_enabled),
        pystray.MenuItem("Звук: обнаружен щелчок", pystray.Menu(
            pystray.MenuItem("Выбрать .wav...", choose_sound("detected")),
            pystray.MenuItem("Сбросить на стандартный", reset_sound("detected")),
        )),
        pystray.MenuItem("Звук: успешный запуск", pystray.Menu(
            pystray.MenuItem("Выбрать .wav...", choose_sound("launch")),
            pystray.MenuItem("Сбросить на стандартный", reset_sound("launch")),
        )),
        pystray.MenuItem("Звук: ошибка запуска", pystray.Menu(
            pystray.MenuItem("Выбрать .wav...", choose_sound("error")),
            pystray.MenuItem("Сбросить на стандартный", reset_sound("error")),
        )),
        pystray.MenuItem("Уведомление Windows при запуске", toggle_notify,
                          checked=lambda item: cfg.notify_enabled),
    )

    def toggle_discord(icon, item):
        cfg.discord_notify_enabled = not cfg.discord_notify_enabled
        save_config()

    def set_discord_display_name(icon, item):
        def _flow():
            current = cfg.discord_display_name or discord_notify.get_default_display_name()
            name = ask_text("Имя в сообщениях Discord", f"Как вас подписывать в сообщениях о запуске?\nСейчас: {current}")
            if name is None:
                return
            cfg.discord_display_name = name.strip() or None
            save_config()
            logger.info("Имя в сообщениях Discord изменено на: %s", cfg.discord_display_name or discord_notify.get_default_display_name())
        threading.Thread(target=_flow, daemon=True).start()

    def discord_display_name_label(item=None):
        return f"Моё имя в сообщениях: {cfg.discord_display_name or discord_notify.get_default_display_name()} (изменить...)"

    discord_menu = pystray.Menu(
        pystray.MenuItem("Уведомлять в Discord при срабатывании", toggle_discord,
                          checked=lambda item: cfg.discord_notify_enabled),
        pystray.MenuItem(discord_display_name_label, set_discord_display_name),
    )

    def select_server(name):
        def _handler(icon, item):
            cfg.active_server_name = name
            save_config()
            logger.info("Активный сервер: %s", name)
        return _handler

    def server_checked(name):
        return lambda item: cfg.active_server_name == name

    def do_search(icon, item):
        search_server_dialog(cfg, lambda name: (setattr(cfg, "active_server_name", name), save_config()))

    def toggle_active_favorite(icon, item):
        servers_mod.toggle_favorite(cfg, cfg.active_server_name)
        save_config()

    def active_favorite_label(item=None):
        return "Убрать текущий сервер из избранного" if cfg.active_server().favorite else "Добавить текущий сервер в избранное"

    def servers_menu_items():
        if len(cfg.servers) > SERVER_SEARCH_THRESHOLD:
            yield pystray.MenuItem("Найти сервер по имени...", do_search)
            yield pystray.Menu.SEPARATOR
        valid_names = {s.name for s in cfg.servers}
        recent = [n for n in reversed(cfg.recent_servers[-10:]) if n in valid_names]
        if recent:
            yield pystray.MenuItem("Недавние", pystray.Menu(*(
                pystray.MenuItem(name, select_server(name), radio=True, checked=server_checked(name))
                for name in recent
            )))
            yield pystray.Menu.SEPARATOR
        for server in servers_mod.sorted_servers(cfg):
            star = "\u2605 " if server.favorite else ""
            label = f"{star}{server.name} ({server.address})"
            yield pystray.MenuItem(label, select_server(server.name), radio=True, checked=server_checked(server.name))
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("Добавить сервер...", lambda icon, item: add_server_dialog(cfg, save_config, logger))
        yield pystray.MenuItem("Удалить текущий сервер", lambda icon, item: (
            servers_mod.remove_server(cfg, cfg.active_server_name) and save_config()
        ))
        yield pystray.MenuItem(active_favorite_label, toggle_active_favorite)
        if len(cfg.servers) > SERVER_SEARCH_THRESHOLD:
            yield pystray.MenuItem("Очистить список серверов (оставить избранное + текущий)...",
                                    lambda icon, item: clear_servers_dialog(cfg, save_config, logger))
        yield pystray.MenuItem("Проверить доступность текущего сервера (ping)...",
                                lambda icon, item: check_active_server_dialog(cfg, logger))

    def toggle_autostart(icon, item):
        new_val = not cfg.autostart
        system.set_autostart(new_val, logger)
        cfg.autostart = new_val
        save_config()

    def toggle_skip_if_running(icon, item):
        cfg.skip_if_running = not cfg.skip_if_running
        save_config()

    def toggle_skip_if_steam_updating(icon, item):
        cfg.skip_if_steam_updating = not cfg.skip_if_steam_updating
        save_config()

    def toggle_detection_only(icon, item):
        cfg.detection_only = not cfg.detection_only
        save_config()
        logger.info("Режим «только обнаружение»: %s", "вкл" if cfg.detection_only else "выкл")

    def toggle_check_availability(icon, item):
        cfg.check_availability_before_launch = not cfg.check_availability_before_launch
        save_config()

    def relaunch_admin(icon, item):
        system.relaunch_as_admin(logger)

    def change_hotkey_for(config_key: str, label: str):
        def _handler(icon, item):
            def _flow():
                import hotkeys
                if hotkeys.keyboard is None:
                    system.show_error_box("Snap-to-GMod", "Библиотека 'keyboard' не установлена.\nВыполните: pip install keyboard")
                    return
                try:
                    hotkeys.keyboard.unhook_all_hotkeys()
                except Exception:
                    pass
                icon.title = f"Snap-to-GMod: нажмите новую комбинацию для «{label}»..."
                system.show_info_box("Горячая клавиша",
                                      f"После нажатия «ОК» зажмите нужную комбинацию для «{label}» и отпустите.")
                try:
                    new_hotkey = hotkeys.keyboard.read_hotkey(suppress=False)
                except Exception as e:
                    logger.warning("Ошибка записи горячей клавиши: %s", e)
                    reregister_hotkeys()
                    return
                if "+" not in new_hotkey and len(new_hotkey) == 1:
                    system.show_error_box("Snap-to-GMod",
                                           f"Записана одна клавиша «{new_hotkey}» без Ctrl/Alt/Shift — рискованно.")
                    reregister_hotkeys()
                    return
                old_hotkey = getattr(cfg, config_key)
                setattr(cfg, config_key, new_hotkey.strip().lower())
                if reregister_hotkeys():
                    save_config()
                    logger.info("Горячая клавиша «%s» изменена на: %s", label, getattr(cfg, config_key))
                    icon.title = "Snap-to-GMod: жду щелчка"
                else:
                    setattr(cfg, config_key, old_hotkey)
                    reregister_hotkeys()
                    icon.title = "Snap-to-GMod: жду щелчка"
                    system.show_error_box("Snap-to-GMod", f"Не удалось назначить комбинацию «{new_hotkey}».")
            threading.Thread(target=_flow, daemon=True).start()
        return _handler

    change_hotkey = change_hotkey_for("hotkey", "запуск игры")
    change_pause_hotkey = change_hotkey_for("pause_hotkey", "быстрая пауза")

    def download_voice_model(icon, item=None):
        def _flow():
            logger.info("Голосовой выбор персонажа: начинаю скачивание модели...")
            icon.title = "Snap-to-GMod: скачиваю модель распознавания речи..."
            ok, err = voice_select.download_model(logger)
            icon.title = "Snap-to-GMod: жду щелчка"
            if ok:
                logger.info("Голосовой выбор персонажа: модель скачана.")
                icon.notify("Модель скачана, включаю распознавание.", "Голосовой выбор персонажа")
                if voice_engine is not None:
                    voice_engine.load_async(logger)
            else:
                logger.warning("Голосовой выбор персонажа: не удалось скачать модель: %s", err)
                system.show_error_box(
                    "Snap-to-GMod",
                    f"Не удалось скачать модель автоматически:\n{err}\n\n"
                    "Можно скачать вручную с https://alphacephei.com/vosk/models "
                    "и распаковать в папку модели (пункт «Открыть папку модели/шаблонов»).",
                )
        threading.Thread(target=_flow, daemon=True).start()

    def toggle_voice_select(icon, item):
        if cfg.voice_select_enabled:
            cfg.voice_select_enabled = False
            save_config()
            if voice_engine is not None:
                voice_engine.unload(logger)
            return

        if voice_engine is None or not voice_select.DEPENDENCIES_AVAILABLE:
            missing = ", ".join(voice_select.MISSING_DEPENDENCIES) if voice_engine is not None else "модуль недоступен"
            system.show_error_box(
                "Snap-to-GMod",
                f"Не установлены нужные библиотеки: {missing}.\nВыполните: pip install -r requirements-voice.txt",
            )
            return

        cfg.voice_select_enabled = True
        save_config()

        def _flow():
            if not voice_select.model_is_present() and ask_yes_no(
                "Голосовой выбор персонажа",
                "Модель распознавания речи не найдена.\n\n"
                "Скачать маленькую русскую модель автоматически (~45 МБ, vosk-model-small-ru-0.22)?\n\n"
                "«Нет» — включу без скачивания, модель можно будет положить вручную позже "
                "(пункт «Открыть папку модели/шаблонов»).",
            ):
                download_voice_model(icon)
            else:
                voice_engine.load_async(logger)

        threading.Thread(target=_flow, daemon=True).start()

    def voice_select_status_label(item=None):
        if voice_engine is None:
            return "Голосовой выбор персонажа: недоступно"
        return f"Голосовой выбор персонажа: {voice_engine.status_text}"

    def open_voice_folder(icon, item):
        try:
            import os
            os.startfile(cfg_mod.VOICE_DIR)
        except OSError as e:
            logger.warning("Не удалось открыть папку голосового модуля: %s", e)

    VOICE_GUIDE_STATE = {"visible": False}
    CHAR_MANAGER_STATE = {"visible": False}

    def open_voice_guide_window(icon, item):
        open_voice_guide(cfg, voice_engine, VOICE_GUIDE_STATE, save_config, CHAR_MANAGER_STATE, logger)

    def open_character_manager_window(icon, item):
        open_character_manager(cfg, voice_engine, save_config, logger, CHAR_MANAGER_STATE, VOICE_GUIDE_STATE)

    def characters_label(item=None):
        n = len(cfg.voice_characters)
        return f"Персонажи ({n})..." if n else "Персонажи (добавить...)"

    voice_select_menu = pystray.Menu(
        pystray.MenuItem(voice_select_status_label, None, enabled=False),
        pystray.MenuItem("Включено", toggle_voice_select, checked=lambda item: cfg.voice_select_enabled),
        pystray.MenuItem("Скачать модель распознавания автоматически (~45 МБ)...", download_voice_model,
                          enabled=lambda item: voice_select.DEPENDENCIES_AVAILABLE),
        pystray.MenuItem(characters_label, open_character_manager_window,
                          enabled=lambda item: voice_select.DEPENDENCIES_AVAILABLE and not CHAR_MANAGER_STATE.get("visible")),
        pystray.MenuItem(
            "Открыть папку модели/шаблонов (model/ и templates/)...", open_voice_folder,
        ),
    )

    settings_menu = pystray.Menu(
        pystray.MenuItem("Чувствительность", detection_menu),
        pystray.MenuItem("Микрофон", mic_menu),
        pystray.MenuItem(countdown_label, set_countdown_manual),
        pystray.MenuItem("Звук и уведомления", sound_menu),
        pystray.MenuItem("Discord", discord_menu),
        pystray.MenuItem(lambda item: f"Горячая клавиша запуска: {cfg.hotkey or 'не задана'}", change_hotkey),
        pystray.MenuItem(lambda item: f"Жест быстрой паузы: {cfg.pause_hotkey or 'не задан'}", change_pause_hotkey),
        pystray.MenuItem("Голосовой выбор персонажа", voice_select_menu),
        pystray.MenuItem("Только обнаружение (без запуска игры, только уведомление)", toggle_detection_only,
                          checked=lambda item: cfg.detection_only),
        pystray.MenuItem("Проверять доступность сервера перед запуском", toggle_check_availability,
                          checked=lambda item: cfg.check_availability_before_launch),
        pystray.MenuItem("Запускать вместе с Windows", toggle_autostart, checked=lambda item: cfg.autostart),
        pystray.MenuItem("Не подключаться повторно, если GMod уже открыт", toggle_skip_if_running,
                          checked=lambda item: cfg.skip_if_running),
        pystray.MenuItem("Не запускать, если Steam обновляется", toggle_skip_if_steam_updating,
                          checked=lambda item: cfg.skip_if_steam_updating),
        pystray.MenuItem("Перезапустить от имени администратора (нужно для хоткеев поверх игры)", relaunch_admin,
                          visible=lambda item: platform.system() == "Windows" and not system.is_admin()),
    )

    def show_stats(icon, item):
        system.show_info_box("Snap-to-GMod — статистика", stats_mod.format_stats_message(logger))

    def export_stats_as_csv(icon, item):
        def _flow():
            path = ask_save_file("Экспорт статистики", ".csv", "snap_to_gmod_stats.csv", [("CSV файлы", "*.csv")])
            if not path:
                return
            try:
                stats_mod.export_stats_csv(path, logger)
                logger.info("Статистика экспортирована (CSV): %s", path)
                system.show_info_box("Snap-to-GMod", f"Статистика сохранена в файл:\n{path}")
            except OSError as e:
                logger.warning("Не удалось экспортировать статистику: %s", e)
                system.show_error_box("Snap-to-GMod", f"Не удалось сохранить файл:\n{e}")
        threading.Thread(target=_flow, daemon=True).start()

    LOG_VIEWER_STATE = {"visible": False}

    def toggle_log_viewer(icon, item):
        if LOG_VIEWER_STATE["visible"]:
            LOG_VIEWER_STATE["visible"] = False
        else:
            LOG_VIEWER_STATE["visible"] = True
            open_log_viewer(icon, LOG_VIEWER_STATE, logger)
        try:
            icon.update_menu()
        except Exception:
            pass

    def export_settings(icon, item):
        def _flow():
            path = ask_save_file("Сохранить настройки Snap-to-GMod", ".json", "snap_to_gmod_settings.json",
                                  [("JSON файлы", "*.json")])
            if not path:
                return
            try:
                import json
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(cfg.to_dict(), f, ensure_ascii=False, indent=2)
                logger.info("Настройки экспортированы: %s", path)
                system.show_info_box("Snap-to-GMod", f"Настройки сохранены в файл:\n{path}")
            except OSError as e:
                logger.warning("Не удалось экспортировать настройки: %s", e)
                system.show_error_box("Snap-to-GMod", f"Не удалось сохранить файл:\n{e}")
        threading.Thread(target=_flow, daemon=True).start()

    def import_settings(icon, item):
        def _flow():
            path = ask_open_file("Загрузить настройки Snap-to-GMod", [("JSON файлы", "*.json"), ("Все файлы", "*.*")])
            if not path:
                return
            try:
                import json
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                imported = AppConfig.from_dict(data)
                cfg.__dict__.update(imported.__dict__)
                save_config()
                reregister_hotkeys()
                logger.info("Настройки импортированы из: %s", path)
                system.show_info_box("Snap-to-GMod", "Настройки импортированы.\n"
                                                       "Автозапуск нужно будет включить заново, если он был включён на другом ПК.")
            except (OSError, ValueError) as e:
                logger.warning("Не удалось импортировать настройки: %s", e)
                system.show_error_box("Snap-to-GMod", f"Не удалось прочитать файл:\n{e}")
        threading.Thread(target=_flow, daemon=True).start()

    def open_settings_folder(icon, item):
        try:
            import os
            os.startfile(cfg_mod.SETTINGS_DIR)
        except OSError as e:
            logger.warning("Не удалось открыть папку настроек: %s", e)

    def open_log(icon, item):
        try:
            import os
            if not cfg_mod.LOG_FILE.exists():
                cfg_mod.LOG_FILE.touch()
            os.startfile(cfg_mod.LOG_FILE)
        except OSError as e:
            logger.warning("Не удалось открыть журнал: %s", e)

    data_menu = pystray.Menu(
        pystray.MenuItem("Статистика", show_stats),
        pystray.MenuItem("Экспорт статистики как CSV...", export_stats_as_csv),
        pystray.MenuItem("Просмотр журнала (окно, без открытия файла)", toggle_log_viewer,
                          checked=lambda item: LOG_VIEWER_STATE.get("visible")),
        pystray.MenuItem("Экспортировать настройки...", export_settings),
        pystray.MenuItem("Импортировать настройки...", import_settings),
        pystray.MenuItem("Открыть папку настроек", open_settings_folder),
        pystray.MenuItem("Открыть журнал (файл)", open_log),
    )

    def toggle_pause(icon, item):
        toggle_pause_gesture(pause_event, icon_state_holder[0], logger, via="меню")

    def check_updates_now(icon, item):
        updates.check_for_updates_async(
            logger, silent=False,
            on_update_available=lambda tag, url, asset: show_update_window(tag, url, asset, logger),
        )

    return pystray.Menu(
        pystray.MenuItem(f"Snap-to-GMod v{cfg_mod.APP_VERSION}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Приостановить прослушивание", toggle_pause, checked=lambda item: pause_event.is_set()),
        pystray.MenuItem("Сервер", pystray.Menu(servers_menu_items)),
        pystray.MenuItem("Настройки", settings_menu),
        pystray.MenuItem("Данные и журнал", data_menu),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Проверить обновления...", check_updates_now),
        pystray.MenuItem("Выход", on_quit),
    )
