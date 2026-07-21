# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []

# Голосовой выбор персонажа (voice_select.py) опционален. vosk и opencv-python
# — не чисто питоновские пакеты, у них внутри нативные .dll/.so библиотеки,
# которые PyInstaller при обычном анализе импортов НЕ подхватывает — из-за
# этого при запуске .exe была ошибка "cannot find ...\_MEI.....\vosk" (папка
# с DLL просто не попадала в сборку). collect_all() явно забирает все файлы
# пакета, включая бинарники.
#
# Если requirements-voice.txt в этом venv не установлен — collect_all() упадёт
# с ошибкой импорта, поэтому обёрнуто в try/except: сборка просто пройдёт без
# голосового модуля, пункт меню в трее покажет "не установлены библиотеки".
# ВАЖНО: собирать нужно тем же venv/python, куда ставился requirements-voice.txt,
# иначе даже при этом try/except модуль в сборку не попадёт.
for _pkg in ("vosk", "cv2"):
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception:
        pass

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SnapToGMod',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
