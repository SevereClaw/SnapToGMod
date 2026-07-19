"""
Snap-to-GMod
------------
Слушает микрофон, распознаёт щелчок пальцами / хлопок и запускает Garry's
Mod через Steam с подключением к выбранному серверу. Есть резервная
горячая клавиша (по умолчанию Ctrl+Alt+G) на случай, если микрофон подведёт.

НАСТРОЙКИ ЧЕРЕЗ ТРЕЙ (меню сгруппировано в несколько подменю, чтобы не быть
длинным списком — «Сервер», «Настройки», «Данные и журнал»):
Щёлкните правой кнопкой по иконке в трее. Там можно:
- менять чувствительность распознавания, откалибровать её автоматически
  или проверить микрофон вживую (тест НЕ запускает игру)
- ставить прослушивание на паузу (без закрытия программы)
- включить и настроить обратный отсчёт перед запуском (с кнопкой «Отменить»)
  или полностью выключить его — по умолчанию выключен, запуск мгновенный
- менять горячую клавишу
- выбирать сервер для подключения / добавлять новые
- включать/выключать звук и уведомления Windows при срабатывании
- смотреть статистику запусков
- экспортировать/импортировать настройки (перенос на другой ПК)
- включить автозапуск с Windows
- проверить обновления вручную (см. ниже про GITHUB_REPO)
Все настройки сохраняются в config.json, статистика — в stats.json.

ОБНОВЛЕНИЯ (если раздаёте программу друзьям):
Укажите свой репозиторий в переменной GITHUB_REPO в начале файла и публикуйте
релизы на GitHub с тегами вида "v1.0.1". Программа тихо проверяет версию при
каждом запуске и молчит, если версия последняя; пункт меню «Проверить
обновления...» делает то же самое, но всегда показывает результат. Не забудьте
поднимать APP_VERSION при каждом релизе.

ЗАЩИТА ОТ ЛОЖНЫХ ПОВТОРНЫХ СРАБАТЫВАНИЙ:
Если перед похожим на щелчок звуком несколько блоков подряд уже было громко
(музыка, разговор), срабатывание пропускается — это, скорее всего, часть
того же фонового шума, а не отдельный щелчок.

РОТАЦИЯ ЖУРНАЛА:
Лог-файл не растёт бесконечно: при превышении ~2 МБ старое содержимое
переносится в snap_to_gmod.log.old, а запись продолжается с чистого файла.

ЗАПУСК ИЗ ИСХОДНИКА:
    pip install sounddevice numpy pystray pillow keyboard
    python snap_to_gmod.py
    (keyboard нужен только для горячей клавиши; без него всё остальное
    работает как обычно, просто хоткей будет недоступен)

СБОРКА В .EXE (чтобы не требовался Python на компьютере):
    pip install pyinstaller
    pyinstaller --onefile --noconsole --name SnapToGMod snap_to_gmod.py
    Готовый файл появится в папке dist\\SnapToGMod.exe.

СМЕНА ЗНАЧКА .EXE (внешний вид файла — НЕ цветной кружок-индикатор в трее,
тот рисуется программой и всегда остаётся как есть):
    Вариант 1 (без пересборки): создайте ярлык на SnapToGMod.exe правой
    кнопкой мыши -> «Создать ярлык», затем правой кнопкой по ярлыку ->
    «Свойства» -> «Сменить значок» -> укажите свой .ico файл.
    Вариант 2 (при сборке): добавьте флаг --icon свой_значок.ico к команде
    pyinstaller выше — тогда сам .exe будет со своей иконкой.

ГДЕ ХРАНЯТСЯ НАСТРОЙКИ, СТАТИСТИКА И ЖУРНАЛ ОШИБОК:
    %APPDATA%\\SnapToGMod\\config.json
    %APPDATA%\\SnapToGMod\\stats.json
    %APPDATA%\\SnapToGMod\\snap_to_gmod.log
    (папку и журнал можно открыть прямо из меню иконки в трее)
"""

import sys
import os
import time
import json
import platform
import threading
import traceback
import subprocess
import collections

# ==================== БАЗОВАЯ ИНФРАСТРУКТУРА (без сторонних библиотек) ====

FROZEN = getattr(sys, "frozen", False)  # True, если это собранный PyInstaller .exe

APP_VERSION = "1.0.0"
# Укажите свой GitHub-репозиторий вида "имя-пользователя/название-репозитория",
# чтобы заработала проверка обновлений (пункт меню + тихая проверка при запуске).
# Требуется, чтобы в репозитории были GitHub Releases с тегами вида "v1.0.1".
# Если оставить как есть, проверка обновлений просто вежливо скажет, что не настроена.
GITHUB_REPO = "yourusername/snap-to-gmod"


def get_settings_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "SnapToGMod")
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass
    return path


SETTINGS_DIR = get_settings_dir()
CONFIG_FILE = os.path.join(SETTINGS_DIR, "config.json")
STATS_FILE = os.path.join(SETTINGS_DIR, "stats.json")
LOG_FILE = os.path.join(SETTINGS_DIR, "snap_to_gmod.log")


MAX_LOG_BYTES = 2 * 1024 * 1024  # 2 МБ — при превышении старый файл переносится в .log.old
_log_call_counter = 0


def rotate_log_if_needed():
    """Если журнал стал слишком большим, переименовывает его в .log.old
    (затирая предыдущий .old) и начинает новый файл с чистого листа."""
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > MAX_LOG_BYTES:
            old_log = LOG_FILE + ".old"
            try:
                if os.path.exists(old_log):
                    os.remove(old_log)
                os.rename(LOG_FILE, old_log)
            except Exception:
                pass
    except Exception:
        pass


def log(msg=""):
    """Печатает в консоль (если она есть) и всегда дописывает в лог-файл —
    это нужно, потому что собранный .exe с --noconsole консоли не имеет."""
    global _log_call_counter
    msg = str(msg)
    try:
        if sys.stdout is not None:
            print(msg)
    except Exception:
        pass
    # Проверяем размер не на каждой записи (лишний stat() на диске), а раз
    # в 200 вызовов — этого достаточно, чтобы файл не разросся бесконтрольно.
    _log_call_counter += 1
    if _log_call_counter % 200 == 0:
        rotate_log_if_needed()
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


def show_error_box(title: str, message: str):
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)  # MB_ICONERROR
    except Exception:
        pass


def show_info_box(title: str, message: str):
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x40)  # MB_ICONINFORMATION
    except Exception:
        pass


# Держим ссылку на хэндл мьютекса живой на весь срок работы процесса —
# иначе Windows может посчитать, что мьютекс свободен, и второй экземпляр
# всё же запустится.
_single_instance_mutex_handle = None


def acquire_single_instance_lock() -> bool:
    """Гарантирует, что одновременно работает только один экземпляр
    программы. Возвращает False, если экземпляр уже запущен."""
    global _single_instance_mutex_handle
    if platform.system() != "Windows":
        return True
    try:
        import ctypes
        ERROR_ALREADY_EXISTS = 183
        mutex_name = "Global\\SnapToGMod_SingleInstance_Mutex"
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
        last_error = ctypes.windll.kernel32.GetLastError()
        if last_error == ERROR_ALREADY_EXISTS:
            return False
        _single_instance_mutex_handle = handle
        return True
    except Exception as e:
        log(f"[!] Не удалось проверить единственность экземпляра: {e}")
        return True


def is_admin() -> bool:
    """Проверяет, запущена ли программа с правами администратора. Это важно
    для глобальной горячей клавиши: если игра (или Steam) запущена от имени
    администратора, Windows не даёт обычному процессу перехватывать нажатия
    клавиш поверх неё (защита UIPI) — хоткей будет молча не срабатывать."""
    if platform.system() != "Windows":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin():
    """Перезапускает текущий процесс с запросом прав администратора через UAC
    и завершает текущий (не повышенный) экземпляр."""
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        exe = sys.executable
        if FROZEN:
            args = " ".join(f'"{a}"' for a in sys.argv[1:])
        else:
            args = f'"{os.path.abspath(__file__)}" ' + " ".join(f'"{a}"' for a in sys.argv[1:])
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, None, 1)
        if int(result) <= 32:
            log(f"[!] Пользователь отменил запрос на повышение прав или он не удался (код {result}).")
            return
        log("[+] Запущен новый экземпляр с правами администратора, закрываю текущий.")
        os._exit(0)
    except Exception as e:
        log(f"[!] Не удалось перезапуститься с правами администратора: {e}")
        show_error_box("Snap-to-GMod", f"Не удалось перезапустить с правами администратора:\n{e}")


def _fail_with_pause(message: str):
    log(message)
    if FROZEN:
        show_error_box("Snap-to-GMod — ошибка запуска", message + f"\n\nПодробности: {LOG_FILE}")
    elif platform.system() == "Windows":
        try:
            input("\nНажмите Enter, чтобы закрыть окно...")
        except EOFError:
            pass
    sys.exit(1)


try:
    import numpy as np
    import sounddevice as sd
    from PIL import Image, ImageDraw
    import pystray
except ImportError as e:
    _fail_with_pause(
        f"[!] Не установлена одна из библиотек: {e}\n"
        "    Выполните эту команду и запустите скрипт снова:\n"
        "    pip install sounddevice numpy pystray pillow"
    )

try:
    import winreg  # автозапуск через реестр — только Windows
except ImportError:
    winreg = None

try:
    import winsound  # проигрывание звука — только Windows
except ImportError:
    winsound = None

try:
    import keyboard  # глобальная горячая клавиша — резервный способ на случай проблем с микрофоном
except ImportError:
    keyboard = None


# ==================== НАСТРОЙКИ ПО УМОЛЧАНИЮ ====================

SAMPLE_RATE = 44100
BLOCK_SIZE = 1024
INPUT_DEVICE = None  # None = микрофон по умолчанию

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_NAME = "SnapToGMod"

DEFAULT_CONFIG = {
    "threshold": 0.18,        # порог громкости срабатывания
    "cooldown": 15.0,         # пауза между срабатываниями, сек
    "sound_enabled": True,    # проигрывать звук при срабатывании
    "sound_path": None,       # свой .wav; None = стандартный системный сигнал
    "notify_enabled": True,   # показывать уведомление Windows при срабатывании
    "autostart": False,       # запускать вместе с Windows
    "skip_if_running": True,  # не подключаться повторно, если GMod уже открыт
    "hotkey": "ctrl+alt+g",   # резервная горячая клавиша (на случай проблем с микрофоном)
    "countdown_seconds": 0,   # обратный отсчёт перед запуском, сек; 0 = выключен (мгновенный запуск)
    "servers": [
        {"name": "Shinri Trial", "ip": "80.66.82.229", "port": "27103"},
    ],
    "active_server_name": "Shinri Trial",
}

SENSITIVITY_PRESETS = [
    ("Высокая (ловит тихие щелчки)", 0.10),
    ("Средняя (по умолчанию)", 0.18),
    ("Низкая (меньше случайных срабатываний)", 0.30),
]

COUNTDOWN_PRESETS = [
    ("Выключен (мгновенный запуск)", 0),
    ("2 секунды", 2),
    ("3 секунды", 3),
    ("5 секунд", 5),
    ("10 секунд", 10),
]


# ==================== КОНФИГ ====================

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            for key in DEFAULT_CONFIG:
                if key in data:
                    cfg[key] = data[key]
    except Exception as e:
        log(f"[!] Не удалось прочитать config.json, использую значения по умолчанию: {e}")
    if not cfg.get("servers"):
        cfg["servers"] = list(DEFAULT_CONFIG["servers"])
    return cfg


def save_config(cfg: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"[!] Не удалось сохранить config.json: {e}")


def get_active_server(cfg: dict) -> dict:
    servers = cfg.get("servers") or []
    active_name = cfg.get("active_server_name")
    for server in servers:
        if server.get("name") == active_name:
            return server
    if servers:
        return servers[0]
    return dict(DEFAULT_CONFIG["servers"][0])


# ==================== СТАТИСТИКА ЗАПУСКОВ ====================

def load_stats() -> dict:
    try:
        if os.path.exists(STATS_FILE):
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log(f"[!] Не удалось прочитать stats.json: {e}")
    return {"total_launches": 0, "history": []}


def record_launch(server_name: str):
    """Сохраняет факт запуска: общий счётчик + последние 20 записей с временем."""
    stats = load_stats()
    stats["total_launches"] = stats.get("total_launches", 0) + 1
    history = stats.get("history", [])
    history.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "server": server_name})
    stats["history"] = history[-20:]
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"[!] Не удалось сохранить stats.json: {e}")


def format_stats_message() -> str:
    stats = load_stats()
    total = stats.get("total_launches", 0)
    history = stats.get("history", [])
    if total == 0:
        return "Пока не было ни одного срабатывания."
    lines = [f"Всего срабатываний: {total}", "", "Последние запуски:"]
    for entry in reversed(history[-10:]):
        lines.append(f"  {entry['time']} — {entry['server']}")
    return "\n".join(lines)


# ==================== ПРОВЕРКА ОБНОВЛЕНИЙ ====================

def _parse_version(v: str):
    """'v1.2.3' -> (1, 2, 3), чтобы можно было сравнивать версии как числа,
    а не строки (иначе '1.10.0' окажется 'меньше' '1.9.0')."""
    v = (v or "").strip().lstrip("vV")
    parts = []
    for piece in v.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts) or (0,)


def check_for_updates(silent: bool = True):
    """Сверяет текущую версию с последним релизом на GitHub.
    silent=True (тихая проверка при запуске) — молчит, если версия
    актуальна, или если проверка не настроена/не удалась.
    silent=False (ручной вызов из меню) — сообщает результат в любом случае."""
    if not GITHUB_REPO or "yourusername" in GITHUB_REPO:
        log("[i] Проверка обновлений не настроена (GITHUB_REPO не указан).")
        if not silent:
            show_info_box(
                "Snap-to-GMod",
                "Проверка обновлений не настроена.\n"
                "Укажите свой GitHub-репозиторий в переменной GITHUB_REPO в коде программы.",
            )
        return
    try:
        import urllib.request
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": "SnapToGMod"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latest_tag = data.get("tag_name", "")
        latest_url = data.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases"
        if not latest_tag:
            raise ValueError("пустой tag_name в ответе GitHub")
        if _parse_version(latest_tag) > _parse_version(APP_VERSION):
            log(f"[i] Доступна новая версия: {latest_tag} (у вас {APP_VERSION})")
            show_info_box(
                "Snap-to-GMod — доступно обновление",
                f"Вышла новая версия: {latest_tag}\n"
                f"У вас установлена: {APP_VERSION}\n\n"
                f"Скачать: {latest_url}",
            )
        else:
            log(f"[i] Установлена последняя версия ({APP_VERSION}).")
            if not silent:
                show_info_box("Snap-to-GMod", f"У вас последняя версия: {APP_VERSION}")
    except Exception as e:
        log(f"[!] Не удалось проверить обновления: {e}")
        if not silent:
            show_error_box("Snap-to-GMod", f"Не удалось проверить обновления:\n{e}")


def check_for_updates_async(silent: bool = True):
    threading.Thread(target=check_for_updates, args=(silent,), daemon=True).start()


# ==================== АВТОЗАПУСК (реестр Windows) ====================

def get_launch_command() -> str:
    if FROZEN:
        return f'"{sys.executable}"'
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    launcher = pythonw if os.path.exists(pythonw) else exe
    script_path = os.path.abspath(__file__)
    return f'"{launcher}" "{script_path}"'


def is_autostart_enabled() -> bool:
    if winreg is None:
        return False
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ)
        try:
            winreg.QueryValueEx(key, APP_NAME)
            return True
        except FileNotFoundError:
            return False
        finally:
            winreg.CloseKey(key)
    except Exception:
        return False


def set_autostart(enabled: bool):
    if winreg is None:
        log("[!] Автозапуск доступен только на Windows.")
        return
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE)
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, get_launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        log(f"[+] Автозапуск {'включён' if enabled else 'выключен'}.")
    except Exception as e:
        log(f"[!] Не удалось изменить автозапуск: {e}")


# ==================== ЗВУК ПРИ СРАБАТЫВАНИИ ====================

def play_trigger_sound(cfg: dict):
    if not cfg.get("sound_enabled", True) or winsound is None:
        return
    try:
        sound_path = cfg.get("sound_path")
        if sound_path and os.path.exists(sound_path):
            winsound.PlaySound(sound_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        else:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception as e:
        log(f"[!] Не удалось воспроизвести звук: {e}")


def choose_sound_file(cfg: dict):
    """Открывает системный диалог выбора .wav файла в отдельном потоке."""
    def _pick():
        try:
            import tkinter as tk
            from tkinter import filedialog
        except ImportError:
            log("[!] tkinter недоступен — не могу открыть диалог выбора файла.")
            return
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askopenfilename(
                title="Выберите звук для срабатывания",
                filetypes=[("WAV файлы", "*.wav"), ("Все файлы", "*.*")],
            )
            root.destroy()
        except Exception as e:
            log(f"[!] Ошибка диалога выбора файла: {e}")
            return
        if path:
            cfg["sound_path"] = path
            save_config(cfg)
            log(f"[+] Выбран звук: {path}")

    threading.Thread(target=_pick, daemon=True).start()


def ask_text(title: str, prompt: str):
    """Открывает диалог ввода текста и БЛОКИРУЕТ вызывающий поток до ответа.
    Вызывать только из фонового потока, не из потока иконки."""
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
        except Exception as e:
            log(f"[!] Ошибка диалога ввода: {e}")
        finally:
            done.set()

    threading.Thread(target=_ask, daemon=True).start()
    done.wait(timeout=120)
    return result["value"]


def add_server_dialog(cfg: dict):
    """Спрашивает название/IP/порт нового сервера и добавляет его в конфиг."""
    def _flow():
        name = ask_text("Новый сервер", "Название сервера:")
        if not name:
            return
        ip = ask_text("Новый сервер", "IP-адрес сервера:")
        if not ip:
            return
        port = ask_text("Новый сервер", "Порт сервера:")
        if not port:
            return
        servers = cfg.get("servers", [])
        servers = [s for s in servers if s.get("name") != name]
        servers.append({"name": name, "ip": ip.strip(), "port": port.strip()})
        cfg["servers"] = servers
        cfg["active_server_name"] = name
        save_config(cfg)
        log(f"[+] Добавлен сервер: {name} ({ip}:{port})")

    threading.Thread(target=_flow, daemon=True).start()


def remove_active_server(cfg: dict):
    servers = cfg.get("servers", [])
    if len(servers) <= 1:
        log("[!] Нельзя удалить последний оставшийся сервер.")
        return
    active_name = cfg.get("active_server_name")
    servers = [s for s in servers if s.get("name") != active_name]
    cfg["servers"] = servers
    cfg["active_server_name"] = servers[0]["name"]
    save_config(cfg)
    log(f"[+] Сервер «{active_name}» удалён.")


# ==================== ЗАПУСК GMOD ====================

# Разные версии GMod/движка Source в разное время назывались в диспетчере
# задач по-разному. Проверяем все известные варианты сразу.
GMOD_PROCESS_NAMES = {"gmod.exe", "hl2.exe", "garrysmod.exe"}


def is_gmod_running() -> bool:
    """
    Проверяет, запущен ли Garry's Mod, через список процессов Windows.
    ВАЖНО: сравниваем ТОЧНОЕ имя процесса, а не ищем подстроку в тексте —
    иначе, например, имя нашей же трей-программы "SnapToGMod.exe" (в нижнем
    регистре "snaptogmod.exe") само содержит "gmod.exe" на конце и ложно
    определялось бы как запущенная игра.
    """
    if platform.system() != "Windows":
        return False
    try:
        import csv
        import io

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        result = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=creationflags,
        )
        found = set()
        for row in csv.reader(io.StringIO(result.stdout)):
            if not row:
                continue
            image_name = row[0].strip().lower()
            if image_name in GMOD_PROCESS_NAMES:
                found.add(image_name)
        if found:
            log(f"[i] Обнаружен запущенный процесс GMod: {', '.join(found)}")
            return True
        return False
    except Exception as e:
        log(f"[!] Не удалось проверить, запущен ли GMod: {e}")
        return False


def launch_gmod_and_connect(server: dict):
    ip, port = server["ip"], server["port"]
    connect_uri = f"steam://connect/{ip}:{port}"
    log(f"[+] Щелчок распознан! Открываю {connect_uri} ({server.get('name', '')}) ...")

    system = platform.system()
    try:
        if system == "Windows":
            os.startfile(connect_uri)
        elif system == "Darwin":
            subprocess.run(["open", connect_uri], check=True)
        else:
            subprocess.run(["xdg-open", connect_uri], check=True)
        log("[+] Команда отправлена в Steam.")
        record_launch(server.get("name", f"{ip}:{port}"))
    except Exception as e:
        log(f"[!] Не удалось открыть steam:// ссылку: {e}")
        log("    Убедитесь, что Steam установлен и является обработчиком steam:// по умолчанию.")


# ==================== РАСПОЗНАВАНИЕ ЩЕЛЧКА ====================

def snap_peak_if_shaped(audio_block: np.ndarray, min_peak: float = 0.03):
    """
    Проверяет "форму" звука (резкость + широкополосность), БЕЗ проверки
    порога чувствительности. Возвращает пиковую громкость, если звук похож
    на щелчок/хлопок, иначе None. Используется и обычным распознаванием,
    и режимом калибровки (там свой порог ещё не известен).
    """
    peak = float(np.max(np.abs(audio_block)))
    if peak < min_peak:
        return None

    loud_ratio = np.mean(np.abs(audio_block) > (peak * 0.5))
    if loud_ratio >= 0.25:
        return None

    spectrum = np.abs(np.fft.rfft(audio_block))
    freqs = np.fft.rfftfreq(len(audio_block), d=1.0 / SAMPLE_RATE)
    total_energy = float(np.sum(spectrum)) + 1e-9
    high_energy = float(np.sum(spectrum[freqs > 2000]))
    high_ratio = high_energy / total_energy
    if high_ratio <= 0.12:
        return None

    return peak


def is_snap(audio_block: np.ndarray, threshold: float) -> bool:
    """
    Щелчок = звук подходящей "формы" (см. snap_peak_if_shaped) с пиком
    громче заданного порога чувствительности.
    """
    peak = snap_peak_if_shaped(audio_block)
    return peak is not None and peak >= threshold


# ==================== ТРЕЙ-ИКОНКА ====================

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
    draw.ellipse((margin, margin, size - margin, size - margin),
                 fill=color, outline=(30, 30, 30, 255), width=3)
    return img


class IconStateManager:
    """
    Централизованное, потокобезопасное управление цветом/подсказкой иконки.
    pystray на Windows падает (OSError WinError 1402), если icon.icon меняют
    одновременно из двух потоков — поэтому все изменения идут через один
    Lock, а версия состояния гарантирует, что отложенный возврат к серому
    цвету не затрёт более свежее состояние (например, паузу, включённую
    прямо во время жёлтой вспышки).
    """

    def __init__(self, icon: "pystray.Icon"):
        self.icon = icon
        self._lock = threading.Lock()
        self._version = 0

    def set_state(self, state: str, tooltip: str):
        with self._lock:
            try:
                self.icon.icon = make_icon_image(COLORS[state])
                self.icon.title = tooltip
            except OSError as e:
                log(f"[!] Пропущено обновление иконки трея: {e}")

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
        """Отменяет любой отложенный revert() из старой вспышки — вызывать
        при смене состояния напрямую (например, включение/выключение паузы)."""
        self._version += 1


class TriggerState:
    """Общий кулдаун между щелчком-триггером и горячей клавишей — чтобы
    не запускать игру дважды, если сработали оба способа почти одновременно."""

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


def show_countdown_dialog(seconds: int, server_name: str, icon_state: "IconStateManager",
                           stop_event: threading.Event) -> bool:
    """Показывает окно обратного отсчёта перед запуском с кнопкой «Отменить».
    БЛОКИРУЕТ вызывающий поток до истечения времени или отмены — поэтому
    вызывать только из фонового потока (не из потока иконки/аудио-стрима
    напрямую), см. perform_trigger, который запускается отдельным потоком.
    Возвращает True, если можно запускать игру, False — если отменено."""
    result = {"proceed": True}
    done = threading.Event()

    def _flow():
        try:
            import tkinter as tk
        except ImportError:
            log("[!] tkinter недоступен — обратный отсчёт пропущен, запускаю сразу.")
            done.set()
            return
        try:
            root = tk.Tk()
            root.title("Snap-to-GMod")
            root.attributes("-topmost", True)
            root.resizable(False, False)
            root.protocol("WM_DELETE_WINDOW", lambda: None)  # закрыть можно только «Отменить»

            remaining = {"value": seconds}
            label_var = tk.StringVar(
                value=f"Подключаюсь к «{server_name}» через {remaining['value']} сек...")
            tk.Label(root, textvariable=label_var, padx=26, pady=16,
                     justify="center", font=("Segoe UI", 11)).pack()

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
            log(f"[!] Ошибка окна обратного отсчёта: {e}")
            done.set()

    threading.Thread(target=_flow, daemon=True).start()
    icon_state.flash(TrayState["HEARD"], f"Snap-to-GMod: запуск через {seconds} сек (отменить в окне)...",
                      min(seconds, 2.0), stop_event)
    done.wait(timeout=seconds + 3)
    if not result["proceed"]:
        log("[i] Запуск отменён пользователем в окне обратного отсчёта.")
    return result["proceed"]


def perform_trigger(cfg: dict, icon: "pystray.Icon", icon_state: "IconStateManager",
                     stop_event: threading.Event):
    """Общая логика запуска — вызывается и по щелчку, и по горячей клавише.
    ВАЖНО: должна запускаться в отдельном потоке (не из колбэка аудио-стрима
    и не из потока библиотеки keyboard напрямую), т.к. может блокироваться
    на несколько секунд, если включён обратный отсчёт."""
    if cfg.get("skip_if_running", True) and is_gmod_running():
        log("[i] GMod уже запущен — срабатывание проигнорировано, чтобы не спамить переподключением.")
        icon_state.flash(TrayState["HEARD"], "Snap-to-GMod: вы уже в игре, срабатывание проигнорировано",
                          0.6, stop_event)
        return

    server = get_active_server(cfg)
    countdown = int(cfg.get("countdown_seconds", 0) or 0)
    if countdown > 0:
        if not show_countdown_dialog(countdown, server.get("name", ""), icon_state, stop_event):
            return

    log(f"[+] Запускаю GMod -> {server.get('name', '')}")
    icon_state.flash(TrayState["TRIGGER"], f"Snap-to-GMod: запускаю {server.get('name', '')}!",
                      1.5, stop_event)
    play_trigger_sound(cfg)
    launch_gmod_and_connect(server)
    if cfg.get("notify_enabled", True):
        try:
            icon.notify("Удачной игры! \U0001F3AE", f"Snap-to-GMod: {server.get('name', '')}")
        except Exception as e:
            log(f"[!] Не удалось показать уведомление Windows: {e}")


def start_mic_test(mic_test_state: dict, cfg: dict):
    """Открывает окно с живым уровнем микрофона — НЕ запускает игру, только
    для проверки, что порог чувствительности подобран разумно."""
    if mic_test_state.get("active"):
        return
    mic_test_state["active"] = True
    mic_test_state["peak"] = 0.0
    log("[i] Запущен тест микрофона (игра не запускается).")

    def stop_test():
        mic_test_state["active"] = False
        log("[i] Тест микрофона остановлен.")

    def _window():
        try:
            import tkinter as tk
        except ImportError:
            log("[!] tkinter недоступен — тест микрофона без окна показать нельзя.")
            mic_test_state["active"] = False
            return
        try:
            root = tk.Tk()
            root.title("Тест микрофона — Snap-to-GMod")
            root.attributes("-topmost", True)
            root.resizable(False, False)

            def on_close():
                stop_test()
                try:
                    root.destroy()
                except Exception:
                    pass

            root.protocol("WM_DELETE_WINDOW", on_close)

            tk.Label(
                root,
                text="Щёлкайте пальцами или хлопайте рядом с микрофоном.\n"
                     "Это НЕ запускает игру — только показывает уровень громкости.",
                padx=22, pady=12, justify="center", font=("Segoe UI", 10),
            ).pack()

            canvas = tk.Canvas(root, width=300, height=22, bg="#222222", highlightthickness=0)
            canvas.pack(padx=22, pady=(0, 6))
            bar = canvas.create_rectangle(0, 0, 0, 22, fill="#4caf50", width=0)
            threshold_line = canvas.create_line(0, 0, 0, 22, fill="#ff4444", width=2)

            value_var = tk.StringVar(value="Пик: 0.000  /  порог 0.000")
            tk.Label(root, textvariable=value_var, font=("Segoe UI", 10)).pack(pady=(0, 4))
            hint_var = tk.StringVar(value="")
            tk.Label(root, textvariable=hint_var, font=("Segoe UI", 9), fg="#666666").pack(pady=(0, 8))

            tk.Button(root, text="Закрыть тест", command=on_close, padx=12, pady=5).pack(pady=(0, 14))

            def poll():
                if not mic_test_state.get("active"):
                    try:
                        root.destroy()
                    except Exception:
                        pass
                    return
                peak = mic_test_state.get("peak", 0.0)
                threshold = cfg.get("threshold", 0.18)
                scale = max(threshold * 1.6, 0.02)
                width = max(0, min(300, int(peak / scale * 300)))
                color = "#4caf50" if peak >= threshold else "#e6be28" if peak > threshold * 0.4 else "#555555"
                canvas.coords(bar, 0, 0, width, 22)
                canvas.itemconfig(bar, fill=color)
                x = min(300, int(threshold / scale * 300))
                canvas.coords(threshold_line, x, 0, x, 22)
                value_var.set(f"Пик: {peak:.3f}  /  порог {threshold:.3f}")
                hint_var.set("Зелёный столбец дошёл до красной черты -> сработал бы щелчок."
                              if peak >= threshold else "")
                root.after(100, poll)

            root.after(100, poll)
            root.mainloop()
        except Exception as e:
            log(f"[!] Ошибка окна теста микрофона: {e}")
            mic_test_state["active"] = False

    threading.Thread(target=_window, daemon=True).start()


def register_hotkey(cfg: dict, on_hotkey):
    """Регистрирует глобальную горячую клавишу (резервный способ запуска,
    если микрофон не подхватил щелчок)."""
    if keyboard is None:
        log("[i] Библиотека 'keyboard' не установлена — горячая клавиша недоступна "
            "(pip install keyboard, если нужна).")
        return False
    try:
        keyboard.unhook_all_hotkeys()
    except Exception:
        pass
    hotkey = cfg.get("hotkey") or "ctrl+alt+g"
    try:
        keyboard.parse_hotkey(hotkey)  # проверяем формат ДО регистрации
    except Exception as e:
        log(f"[!] Некорректный формат горячей клавиши '{hotkey}': {e}")
        return False
    try:
        keyboard.add_hotkey(hotkey, on_hotkey)
        log(f"[+] Горячая клавиша зарегистрирована: {hotkey}")
        if platform.system() == "Windows" and not is_admin():
            log("[i] Программа запущена БЕЗ прав администратора. Если Garry's Mod или "
                "Steam запущены от имени администратора, горячая клавиша НЕ будет "
                "срабатывать поверх игры (Windows блокирует ввод между процессами с "
                "разным уровнем прав). Решение: пункт меню трея "
                "«Перезапустить от имени администратора».")
        return True
    except Exception as e:
        log(f"[!] Не удалось зарегистрировать горячую клавишу '{hotkey}': {e}")
        return False


def audio_loop(icon: "pystray.Icon", stop_event: threading.Event, cfg: dict,
                pause_event: threading.Event, icon_state: "IconStateManager",
                trigger_state: "TriggerState", calibration_state: dict,
                mic_test_state: dict):
    """Работает в фоновом потоке: слушает микрофон и обновляет иконку."""
    last_heard_flash = 0.0
    # Короткая история пиков последних блоков — нужна для защиты от
    # «случайного второго срабатывания»: если непосредственно перед похожим
    # на щелчок пиком уже был продолжительный громкий шум (музыка, разговор
    # на фоне), это скорее всего не отдельный щелчок, а часть того же шума.
    recent_peaks = collections.deque(maxlen=12)  # ~250-300 мс истории

    def callback(indata, frames, time_info, status):
        nonlocal last_heard_flash
        if status:
            log(str(status))

        if pause_event.is_set():
            return  # прослушивание на паузе — ничего не анализируем

        audio_block = indata[:, 0]
        now = time.time()
        peak_now = float(np.max(np.abs(audio_block)))

        # ---- Режим теста микрофона: только показываем уровень, ничего не триггерим ----
        if mic_test_state.get("active"):
            mic_test_state["peak"] = peak_now
            return

        # ---- Режим калибровки: ловим N щелчков и подбираем порог сами ----
        if calibration_state.get("active"):
            peak = snap_peak_if_shaped(audio_block)
            if peak is not None and (now - calibration_state.get("last_capture", 0)) > 1.0:
                calibration_state["last_capture"] = now
                calibration_state["peaks"].append(peak)
                count = len(calibration_state["peaks"])
                target = calibration_state["target"]
                icon_state.flash(TrayState["TRIGGER"], f"Snap-to-GMod: калибровка {count}/{target}",
                                  0.4, stop_event, idle_state=TrayState["PAUSED"],
                                  idle_tooltip="Snap-to-GMod: калибровка...")
                if count >= target:
                    new_threshold = max(0.05, min(0.5, min(calibration_state["peaks"]) * 0.7))
                    cfg["threshold"] = round(new_threshold, 3)
                    save_config(cfg)
                    calibration_state["active"] = False
                    log(f"[+] Калибровка завершена, новый порог: {cfg['threshold']}")
                    icon_state.bump_version()
                    icon_state.set_state(TrayState["IDLE"], "Snap-to-GMod: жду щелчка")
                    show_info_box("Snap-to-GMod — калибровка",
                                   f"Готово! Новый порог чувствительности: {cfg['threshold']:.3f}")
            return  # пока калибруемся, обычную логику триггера не выполняем

        threshold = cfg["threshold"]
        cooldown = cfg.get("cooldown", 15.0)

        shaped_peak = snap_peak_if_shaped(audio_block)
        is_candidate = shaped_peak is not None and shaped_peak >= threshold

        if is_candidate:
            # Сколько из недавних блоков были заметно громкими -> признак
            # продолжительного шума, а не одиночного изолированного щелчка.
            noisy_recent = sum(1 for p in recent_peaks if p > threshold * 0.35)
            if noisy_recent >= 3:
                log(f"[i] Пик {shaped_peak:.3f} похож на щелчок, но перед ним был продолжительный "
                    "шум — срабатывание пропущено (защита от ложных повторов).")
            elif trigger_state.try_consume(cooldown):
                log(f"[+] Пик: {shaped_peak:.3f} (порог {threshold}) -> распознан как щелчок")
                # Запускаем в отдельном потоке: perform_trigger может ждать
                # обратный отсчёт несколько секунд, а колбэк аудио-потока
                # обязан возвращаться быстро, иначе будут потери сэмплов.
                threading.Thread(target=perform_trigger, args=(cfg, icon, icon_state, stop_event),
                                  daemon=True).start()
        elif peak_now > threshold * 0.4 and (now - last_heard_flash) > 0.3:
            last_heard_flash = now
            icon_state.flash(TrayState["HEARD"], "Snap-to-GMod: услышал звук...", 0.3, stop_event)

        recent_peaks.append(peak_now)

    try:
        log("[+] Открываю аудиопоток микрофона...")
        with sd.InputStream(
            device=INPUT_DEVICE, channels=1, samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE, callback=callback,
        ):
            log("[+] Микрофон слушается. Щёлкните пальцами рядом с ним.")
            while not stop_event.is_set():
                time.sleep(0.1)
    except Exception as e:
        log("[!] ОШИБКА аудиопотока — индикатор поэтому не реагирует:")
        log(traceback.format_exc())
        icon_state.set_state(TrayState["HEARD"], f"Ошибка микрофона: {e}")


def print_audio_devices():
    log("-" * 60)
    log("Доступные аудиоустройства:")
    try:
        log(str(sd.query_devices()))
        log(f"Устройство ввода по умолчанию: #{sd.default.device[0]}")
    except Exception as e:
        log(f"[!] Не удалось получить список устройств: {e}")
    log("-" * 60)


def build_menu(cfg: dict, on_quit, pause_event: threading.Event, icon_state_holder: list,
               calibration_state: dict, mic_test_state: dict, reregister_hotkey):
    def set_sensitivity(value):
        def _handler(icon, item):
            cfg["threshold"] = value
            save_config(cfg)
            log(f"[+] Чувствительность изменена, порог = {value}")
        return _handler

    def sensitivity_checked(value):
        return lambda item: abs(cfg["threshold"] - value) < 1e-6

    def not_calibrating(item=None):
        return not calibration_state.get("active")

    sensitivity_items = [
        pystray.MenuItem(label, set_sensitivity(value), radio=True,
                          checked=sensitivity_checked(value), enabled=not_calibrating)
        for label, value in SENSITIVITY_PRESETS
    ]

    def not_testing_mic(item=None):
        return not mic_test_state.get("active")

    def test_mic(icon, item):
        start_mic_test(mic_test_state, cfg)

    def set_countdown(value):
        def _handler(icon, item):
            cfg["countdown_seconds"] = value
            save_config(cfg)
            log(f"[+] Обратный отсчёт перед запуском: "
                f"{'выключен' if value == 0 else f'{value} сек'}")
        return _handler

    def countdown_checked(value):
        return lambda item: int(cfg.get("countdown_seconds", 0) or 0) == value

    countdown_items = [
        pystray.MenuItem(label, set_countdown(value), radio=True, checked=countdown_checked(value))
        for label, value in COUNTDOWN_PRESETS
    ]

    def check_updates_now(icon, item):
        check_for_updates_async(silent=False)

    def relaunch_admin(icon, item):
        relaunch_as_admin()

    def toggle_pause(icon, item):
        icon_state = icon_state_holder[0]
        if pause_event.is_set():
            pause_event.clear()
            icon_state.bump_version()
            icon_state.set_state(TrayState["IDLE"], "Snap-to-GMod: жду щелчка")
            log("[i] Прослушивание возобновлено.")
        else:
            pause_event.set()
            icon_state.bump_version()
            icon_state.set_state(TrayState["PAUSED"], "Snap-to-GMod: на паузе")
            log("[i] Прослушивание приостановлено.")

    def toggle_sound(icon, item):
        cfg["sound_enabled"] = not cfg.get("sound_enabled", True)
        save_config(cfg)

    def toggle_notify(icon, item):
        cfg["notify_enabled"] = not cfg.get("notify_enabled", True)
        save_config(cfg)

    def choose_sound(icon, item):
        choose_sound_file(cfg)

    def reset_sound(icon, item):
        cfg["sound_path"] = None
        save_config(cfg)
        log("[+] Возвращён стандартный системный звук.")

    def toggle_autostart(icon, item):
        new_val = not cfg.get("autostart", False)
        set_autostart(new_val)
        cfg["autostart"] = new_val
        save_config(cfg)

    def toggle_skip_if_running(icon, item):
        cfg["skip_if_running"] = not cfg.get("skip_if_running", True)
        save_config(cfg)

    def select_server(name):
        def _handler(icon, item):
            cfg["active_server_name"] = name
            save_config(cfg)
            log(f"[+] Активный сервер: {name}")
        return _handler

    def server_checked(name):
        return lambda item: cfg.get("active_server_name") == name

    def servers_menu_items():
        for server in cfg.get("servers", []):
            name = server.get("name", "?")
            label = f"{name} ({server.get('ip')}:{server.get('port')})"
            yield pystray.MenuItem(label, select_server(name), radio=True,
                                    checked=server_checked(name))
        yield pystray.Menu.SEPARATOR
        yield pystray.MenuItem("Добавить сервер...", lambda icon, item: add_server_dialog(cfg))
        yield pystray.MenuItem("Удалить текущий сервер", lambda icon, item: remove_active_server(cfg))

    def show_stats(icon, item):
        show_info_box("Snap-to-GMod — статистика", format_stats_message())

    def open_settings_folder(icon, item):
        try:
            os.startfile(SETTINGS_DIR)
        except Exception as e:
            log(f"[!] Не удалось открыть папку настроек: {e}")

    def open_log(icon, item):
        try:
            if not os.path.exists(LOG_FILE):
                open(LOG_FILE, "a", encoding="utf-8").close()
            os.startfile(LOG_FILE)
        except Exception as e:
            log(f"[!] Не удалось открыть журнал: {e}")

    def start_calibration(icon, item):
        if calibration_state.get("active"):
            return  # калибровка уже идёт — повторный запуск игнорируем
        calibration_state["active"] = True
        calibration_state["peaks"] = []
        calibration_state["last_capture"] = 0.0
        calibration_state["target"] = 5
        log("[i] Начата калибровка чувствительности.")

        def cancel_calibration():
            calibration_state["active"] = False
            log("[i] Калибровка отменена пользователем.")

        def _progress_window():
            """Окно калибровки БЕЗ кнопки «ОК»/«Готово» — защита от дураков:
            единственный способ закрыть окно вручную — это «Отменить»,
            завершить калибровку по-настоящему может только сама программа,
            когда действительно поймает 5 щелчков."""
            try:
                import tkinter as tk
            except ImportError:
                log("[!] tkinter недоступен — калибровка идёт без окна прогресса, "
                    "следите за иконкой в трее (вспышки зелёным = пойманные щелчки).")
                return
            try:
                root = tk.Tk()
                root.title("Калибровка чувствительности — Snap-to-GMod")
                root.attributes("-topmost", True)
                root.resizable(False, False)
                # Крестиком окно не закрыть — только явной кнопкой «Отменить»,
                # чтобы нельзя было случайно/машинально прервать калибровку.
                root.protocol("WM_DELETE_WINDOW", lambda: None)

                tk.Label(
                    root,
                    text="Щёлкните пальцами 5 раз рядом с микрофоном,\n"
                         "с паузой около секунды между щелчками.",
                    padx=24, pady=14, justify="center", font=("Segoe UI", 10),
                ).pack()

                progress_var = tk.StringVar(value="Поймано щелчков: 0 / 5")
                tk.Label(root, textvariable=progress_var, font=("Segoe UI", 13, "bold"),
                          pady=4).pack()

                def do_cancel():
                    cancel_calibration()
                    root.destroy()

                tk.Button(root, text="Отменить калибровку", command=do_cancel,
                           padx=12, pady=5).pack(pady=(4, 14))

                def poll():
                    if not calibration_state.get("active"):
                        try:
                            root.destroy()
                        except Exception:
                            pass
                        return
                    count = len(calibration_state["peaks"])
                    target = calibration_state["target"]
                    progress_var.set(f"Поймано щелчков: {count} / {target}")
                    root.after(150, poll)

                root.after(150, poll)
                root.mainloop()
            except Exception as e:
                log(f"[!] Ошибка окна калибровки: {e}")

        threading.Thread(target=_progress_window, daemon=True).start()

    def change_hotkey(icon, item):
        """Записывает новую горячую клавишу по реальному одновременному
        нажатию клавиш пользователем — вместо ручного ввода текста вроде
        'ctrl+alt+g', что неудобно и легко напечатать с опечаткой."""
        def _flow():
            if keyboard is None:
                show_error_box("Snap-to-GMod",
                                "Библиотека 'keyboard' не установлена — горячая клавиша недоступна.\n"
                                "Выполните: pip install keyboard")
                return

            # На время записи снимаем текущий хоткей, чтобы он не мешал записи
            # и не срабатывал во время удержания клавиш.
            try:
                keyboard.unhook_all_hotkeys()
            except Exception:
                pass

            icon.title = "Snap-to-GMod: нажмите новую комбинацию клавиш..."
            show_info_box(
                "Горячая клавиша",
                "После нажатия «ОК» зажмите нужную комбинацию клавиш ОДНОВРЕМЕННО "
                "(например Ctrl+Alt+G) и отпустите — запись сработает автоматически.",
            )
            try:
                new_hotkey = keyboard.read_hotkey(suppress=False)
            except Exception as e:
                log(f"[!] Ошибка записи горячей клавиши: {e}")
                show_error_box("Snap-to-GMod", f"Не удалось записать комбинацию:\n{e}")
                reregister_hotkey()
                return

            # Предупреждаем, если записана одна клавиша без модификатора —
            # велик риск случайных срабатываний во время игры/печати.
            if "+" not in new_hotkey and len(new_hotkey) == 1:
                show_error_box(
                    "Snap-to-GMod",
                    f"Записана одна клавиша «{new_hotkey}» без Ctrl/Alt/Shift.\n"
                    "Это может срабатывать случайно. Попробуйте ещё раз и "
                    "зажмите комбинацию с модификатором, например Ctrl+Alt+G.",
                )
                reregister_hotkey()
                return

            old_hotkey = cfg.get("hotkey")
            cfg["hotkey"] = new_hotkey.strip().lower()
            if reregister_hotkey():
                save_config(cfg)
                log(f"[+] Горячая клавиша изменена на: {cfg['hotkey']}")
                icon.title = "Snap-to-GMod: жду щелчка"
                show_info_box("Snap-to-GMod", f"Новая горячая клавиша: {cfg['hotkey']}")
            else:
                cfg["hotkey"] = old_hotkey
                reregister_hotkey()
                icon.title = "Snap-to-GMod: жду щелчка"
                show_error_box("Snap-to-GMod", f"Не удалось назначить комбинацию «{new_hotkey}».\n"
                                                "Попробуйте другую комбинацию.")
        threading.Thread(target=_flow, daemon=True).start()

    def export_settings(icon, item):
        def _flow():
            try:
                import tkinter as tk
                from tkinter import filedialog
            except ImportError:
                log("[!] tkinter недоступен — не могу открыть диалог сохранения.")
                return
            try:
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                path = filedialog.asksaveasfilename(
                    title="Сохранить настройки Snap-to-GMod",
                    defaultextension=".json",
                    initialfile="snap_to_gmod_settings.json",
                    filetypes=[("JSON файлы", "*.json")],
                )
                root.destroy()
            except Exception as e:
                log(f"[!] Ошибка диалога сохранения: {e}")
                return
            if not path:
                return
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
                log(f"[+] Настройки экспортированы: {path}")
                show_info_box("Snap-to-GMod", f"Настройки сохранены в файл:\n{path}")
            except Exception as e:
                log(f"[!] Не удалось экспортировать настройки: {e}")
                show_error_box("Snap-to-GMod", f"Не удалось сохранить файл:\n{e}")
        threading.Thread(target=_flow, daemon=True).start()

    def import_settings(icon, item):
        def _flow():
            try:
                import tkinter as tk
                from tkinter import filedialog
            except ImportError:
                log("[!] tkinter недоступен — не могу открыть диалог выбора файла.")
                return
            try:
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                path = filedialog.askopenfilename(
                    title="Загрузить настройки Snap-to-GMod",
                    filetypes=[("JSON файлы", "*.json"), ("Все файлы", "*.*")],
                )
                root.destroy()
            except Exception as e:
                log(f"[!] Ошибка диалога открытия: {e}")
                return
            if not path:
                return
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for key in DEFAULT_CONFIG:
                    if key in data:
                        cfg[key] = data[key]
                save_config(cfg)
                reregister_hotkey()
                log(f"[+] Настройки импортированы из: {path}")
                show_info_box(
                    "Snap-to-GMod",
                    "Настройки импортированы.\n"
                    "Автозапуск с Windows нужно будет включить заново отдельным пунктом меню, "
                    "если он был включён на другом ПК.",
                )
            except Exception as e:
                log(f"[!] Не удалось импортировать настройки: {e}")
                show_error_box("Snap-to-GMod", f"Не удалось прочитать файл:\n{e}")
        threading.Thread(target=_flow, daemon=True).start()

    sound_menu = pystray.Menu(
        pystray.MenuItem("Звук при срабатывании", toggle_sound,
                          checked=lambda item: cfg.get("sound_enabled", True)),
        pystray.MenuItem("Выбрать свой звук (.wav)...", choose_sound),
        pystray.MenuItem("Сбросить звук на стандартный", reset_sound),
        pystray.MenuItem("Уведомление Windows при запуске", toggle_notify,
                          checked=lambda item: cfg.get("notify_enabled", True)),
    )

    detection_menu = pystray.Menu(
        *sensitivity_items,
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Откалибровать по 5 щелчкам...", start_calibration,
                          enabled=not_calibrating),
        pystray.MenuItem("Тест микрофона (без запуска игры)...", test_mic,
                          enabled=not_testing_mic),
    )

    settings_menu = pystray.Menu(
        pystray.MenuItem("Чувствительность", detection_menu),
        pystray.MenuItem("Обратный отсчёт перед запуском", pystray.Menu(*countdown_items)),
        pystray.MenuItem("Звук и уведомления", sound_menu),
        pystray.MenuItem(lambda item: f"Горячая клавиша: {cfg.get('hotkey', 'не задана')}",
                          change_hotkey),
        pystray.MenuItem("Запускать вместе с Windows", toggle_autostart,
                          checked=lambda item: cfg.get("autostart", False)),
        pystray.MenuItem("Не подключаться повторно, если GMod уже открыт", toggle_skip_if_running,
                          checked=lambda item: cfg.get("skip_if_running", True)),
        pystray.MenuItem(
            "Перезапустить от имени администратора (нужно для хоткея поверх игры)",
            relaunch_admin,
            visible=lambda item: platform.system() == "Windows" and not is_admin(),
        ),
    )

    data_menu = pystray.Menu(
        pystray.MenuItem("Статистика", show_stats),
        pystray.MenuItem("Экспортировать настройки...", export_settings),
        pystray.MenuItem("Импортировать настройки...", import_settings),
        pystray.MenuItem("Открыть папку настроек", open_settings_folder),
        pystray.MenuItem("Открыть журнал", open_log),
    )

    return pystray.Menu(
        pystray.MenuItem(f"Snap-to-GMod v{APP_VERSION}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Приостановить прослушивание", toggle_pause,
                          checked=lambda item: pause_event.is_set()),
        pystray.MenuItem("Сервер", pystray.Menu(servers_menu_items)),
        pystray.MenuItem("Настройки", settings_menu),
        pystray.MenuItem("Данные и журнал", data_menu),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Проверить обновления...", check_updates_now),
        pystray.MenuItem("Выход", on_quit),
    )


def main():
    if not acquire_single_instance_lock():
        log("[i] Программа уже запущена — новый экземпляр закрывается.")
        show_info_box(
            "Snap-to-GMod",
            "Snap-to-GMod уже запущен и слушает микрофон.\nПроверьте иконку в системном трее.",
        )
        sys.exit(0)

    rotate_log_if_needed()
    log("=" * 60)
    log(f" Snap-to-GMod v{APP_VERSION}: щёлкните пальцами, чтобы запустить Garry's Mod")
    log(" Иконка появится в системном трее. Настройки — правой кнопкой по ней.")
    log("=" * 60)

    cfg = load_config()
    cfg["autostart"] = is_autostart_enabled()
    save_config(cfg)

    print_audio_devices()
    check_for_updates_async(silent=True)  # тихая проверка при старте — сообщит, только если есть обновление

    stop_event = threading.Event()
    pause_event = threading.Event()
    trigger_state = TriggerState()
    calibration_state = {"active": False, "peaks": [], "last_capture": 0.0, "target": 5}
    mic_test_state = {"active": False, "peak": 0.0}

    def on_quit(icon, item):
        if keyboard is not None:
            try:
                keyboard.unhook_all_hotkeys()
            except Exception:
                pass
        stop_event.set()
        icon.stop()

    icon_state_holder = []

    def on_hotkey_pressed():
        # Срабатывает в потоке библиотеки keyboard — используем тот же
        # общий кулдаун, что и для щелчка, чтобы не спамить.
        if calibration_state.get("active") or mic_test_state.get("active"):
            return
        if trigger_state.try_consume(cfg.get("cooldown", 15.0)):
            log("[+] Горячая клавиша нажата -> запускаю GMod.")
            threading.Thread(target=perform_trigger, args=(cfg, icon, icon_state_holder[0], stop_event),
                              daemon=True).start()

    def reregister_hotkey():
        return register_hotkey(cfg, on_hotkey_pressed)

    icon = pystray.Icon(
        "snap_to_gmod",
        make_icon_image(COLORS[TrayState["IDLE"]]),
        "Snap-to-GMod: запускается...",
        menu=build_menu(cfg, on_quit, pause_event, icon_state_holder, calibration_state,
                         mic_test_state, reregister_hotkey),
    )

    def setup(icon):
        icon.visible = True
        icon.title = "Snap-to-GMod: жду щелчка"
        log("[+] Иконка в трее должна быть видна.")
        icon_state = IconStateManager(icon)
        icon_state_holder.append(icon_state)
        reregister_hotkey()
        threading.Thread(
            target=audio_loop,
            args=(icon, stop_event, cfg, pause_event, icon_state, trigger_state, calibration_state,
                  mic_test_state),
            daemon=True,
        ).start()

    try:
        # icon.run() блокирует и ДОЛЖЕН выполняться в главном потоке —
        # иначе иконка может не появиться (особенно на macOS).
        icon.run(setup=setup)
    except KeyboardInterrupt:
        stop_event.set()
        log("\n[+] Остановлено пользователем.")
    except Exception:
        log("[!] ОШИБКА при запуске трей-иконки:")
        log(traceback.format_exc())
        log("    Возможные причины на Windows: переустановите pystray и pillow —")
        log("    pip install --force-reinstall pystray pillow")
        sys.exit(1)

    log("[+] Завершение работы.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        tb = traceback.format_exc()
        log("\n[!] НЕОБРАБОТАННАЯ ОШИБКА:\n" + tb)
        if FROZEN:
            show_error_box(
                "Snap-to-GMod — ошибка",
                "Программа столкнулась с ошибкой и должна закрыться.\n"
                f"Подробности записаны в файл:\n{LOG_FILE}",
            )
    finally:
        if not FROZEN and platform.system() == "Windows":
            try:
                input("\nНажмите Enter, чтобы закрыть окно...")
            except EOFError:
                pass
