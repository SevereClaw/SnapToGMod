"""
system.py
---------
Всё, что завязано на операционную систему напрямую: права администратора,
единственность запущенного экземпляра, автозапуск через реестр Windows,
системные диалоговые окна сообщений.
"""
from __future__ import annotations

import os
import platform
import sys

from config import APP_NAME, FROZEN, RUN_KEY_PATH

try:
    import winreg
except ImportError:
    winreg = None

_single_instance_mutex_handle = None  # держим хэндл живым весь срок работы процесса


def show_error_box(title: str, message: str) -> None:
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)  # MB_ICONERROR
    except OSError:
        pass


def show_info_box(title: str, message: str) -> None:
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x40)  # MB_ICONINFORMATION
    except OSError:
        pass


def acquire_single_instance_lock() -> bool:
    global _single_instance_mutex_handle
    if platform.system() != "Windows":
        return True
    try:
        import ctypes
        ERROR_ALREADY_EXISTS = 183
        mutex_name = "Global\\SnapToGMod_SingleInstance_Mutex"
        handle = ctypes.windll.kernel32.CreateMutexW(None, False, mutex_name)
        if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return False
        _single_instance_mutex_handle = handle
        return True
    except OSError:
        return True


def is_admin() -> bool:
    if platform.system() != "Windows":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def relaunch_as_admin(logger) -> None:
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        exe = sys.executable
        if FROZEN:
            args = " ".join(f'"{a}"' for a in sys.argv[1:])
        else:
            args = f'"{os.path.abspath(sys.argv[0])}" ' + " ".join(f'"{a}"' for a in sys.argv[1:])
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, None, 1)
        if int(result) <= 32:
            logger.warning("Пользователь отменил запрос на повышение прав или он не удался (код %s).", result)
            return
        logger.info("Запущен новый экземпляр с правами администратора, закрываю текущий.")
        os._exit(0)
    except OSError as e:
        logger.error("Не удалось перезапуститься с правами администратора: %s", e)
        show_error_box("Snap-to-GMod", f"Не удалось перезапустить с правами администратора:\n{e}")


def get_launch_command() -> str:
    if FROZEN:
        return f'"{sys.executable}"'
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    launcher = pythonw if os.path.exists(pythonw) else exe
    script_path = os.path.abspath(sys.argv[0])
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
    except OSError:
        return False


def set_autostart(enabled: bool, logger) -> None:
    if winreg is None:
        logger.warning("Автозапуск доступен только на Windows.")
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
        logger.info("Автозапуск %s.", "включён" if enabled else "выключен")
    except OSError as e:
        logger.error("Не удалось изменить автозапуск: %s", e)
