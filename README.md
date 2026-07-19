# Snap-to-GMod v3.0.0

Windows-приложение, которое слушает микрофон, распознаёт щелчок пальцами или хлопок и подключает Garry's Mod к выбранному серверу через Steam. Резервный запуск выполняется глобальной горячей клавишей `Ctrl+Alt+G`.

## Возможности

- распознавание короткого широкополосного щелчка или хлопка;
- выбор микрофона, тест уровня и калибровка чувствительности;
- адаптивная чувствительность;
- несколько серверов, избранное, поиск и недавние серверы;
- проверка доступности Source-сервера перед запуском;
- защита от повторного запуска GMod и запусков во время обновления Steam;
- обратный отсчёт с возможностью отмены;
- глобальные горячие клавиши запуска и паузы;
- уведомления Windows и Discord;
- статистика запусков и экспорт CSV;
- автозапуск Windows;
- проверка GitHub Releases и автообновление собранного `.exe`.

## Требования

- Windows 10 или Windows 11;
- Python 3.10+ для запуска из исходников;
- установленный Steam с зарегистрированным протоколом `steam://`;
- микрофон.

## Запуск из исходников

```powershell
git clone https://github.com/SevereClaw/SnapToGMod.git
cd SnapToGMod

py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install --upgrade pip
pip install -r requirements.txt
python main.py
```

После запуска приложение появляется в системном трее. Настройки открываются правой кнопкой по значку.

## Сборка `.exe`

```powershell
.\build.ps1
```

Готовый файл: `dist\SnapToGMod.exe`.

Ручная команда:

```powershell
pip install -r requirements-dev.txt
pyinstaller --clean --noconfirm SnapToGMod.spec
```

## Выпуск v3.0.0

Автопроверка обновлений настроена на репозиторий `SevereClaw/SnapToGMod`. Для корректной работы обновлений релиз должен иметь тег формата `v3.0.0` и содержать файл `SnapToGMod.exe`.

```powershell
git add .
git commit -m "Release v3.0.0"
git push origin main
git tag v3.0.0
git push origin v3.0.0
```

Workflow `release.yml` соберёт Windows `.exe` и прикрепит его к GitHub Release.

## Структура

| Файл | Назначение |
|---|---|
| `main.py` | точка входа и связывание модулей |
| `config.py` | версия, репозиторий, пути и конфигурация |
| `audio.py` | распознавание звука и поток микрофона |
| `launcher.py` | запуск GMod и логика срабатывания |
| `servers.py` | серверы и A2S_INFO-проверка |
| `tray.py` | меню трея и окна tkinter |
| `hotkeys.py` | глобальные горячие клавиши |
| `updates.py` | GitHub Releases и автообновление |
| `discord_notify.py` | Discord-уведомления |
| `stats.py` | статистика и CSV |
| `system.py` | Windows-интеграция |
| `sound.py` | звуки событий |
| `logutil.py` | журналирование и ротация лога |

## Данные приложения

Настройки, журнал и статистика хранятся в:

```text
%APPDATA%\SnapToGMod\
```

Основные файлы:

- `config.json` — пользовательские настройки;
- `stats.json` — статистика запусков;
- `snap_to_gmod.log` — журнал.

## Ограничение проверки Steam

Проверка обновления Steam является эвристикой по `bootstrap_log.txt`. Она может давать ложные результаты и отключается в меню настроек.
