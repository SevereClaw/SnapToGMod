"""
config.py
---------
Пути, константы и типизированная конфигурация приложения.

Профили настроек убраны: раньше все настройки хранились в profiles.json
(несколько профилей + активный), теперь — один-единственный config.json.
Это не только проще для большинства пользователей (одно рабочее место —
один набор настроек), но и убирает целый пласт кода на переключение/
дублирование/удаление профилей.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

FROZEN: bool = getattr(sys, "frozen", False)  # True, если это собранный PyInstaller .exe

APP_VERSION = "3.0.0"
APP_NAME = "SnapToGMod"

# Укажите свой GitHub-репозиторий "имя-пользователя/название-репозитория",
# чтобы заработала проверка обновлений. Требуются GitHub Releases с тегами
# вида "v3.0.0".
GITHUB_REPO = "SevereClaw/SnapToGMod"

# Вебхук Discord-канала для уведомлений о срабатывании. Меняется только тут,
# в коде — пользователь редактирует лишь своё отображаемое имя.
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1528392905507475588/J65N_zXPNRfF1RxxHSn0cdJ2E-VMmlDXyNgw8BtnQ4euozaAYECjZ1swURbyiYsJZUyC"

RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"

SAMPLE_RATE = 44100
BLOCK_SIZE = 1024

MAX_RECENT_SERVERS = 30
SERVER_SEARCH_THRESHOLD = 8  # если серверов больше — в меню появляется «Найти...»

SENSITIVITY_PRESETS = [
    ("Высокая (ловит тихие щелчки)", 0.10),
    ("Средняя (по умолчанию)", 0.18),
    ("Низкая (меньше случайных срабатываний)", 0.30),
]


def _settings_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    path = Path(base) / APP_NAME
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return path


SETTINGS_DIR: Path = _settings_dir()
CONFIG_FILE: Path = SETTINGS_DIR / "config.json"
STATS_FILE: Path = SETTINGS_DIR / "stats.json"
LOG_FILE: Path = SETTINGS_DIR / "snap_to_gmod.log"

# Старые файлы предыдущих версий — используются только при однократной миграции.
LEGACY_PROFILES_FILE: Path = SETTINGS_DIR / "profiles.json"
LEGACY_CONFIG_FILE: Path = SETTINGS_DIR / "config.json"  # старый формат совпадает по имени


@dataclass
class Server:
    """Один игровой сервер Source-движка."""
    name: str
    ip: str
    port: str
    favorite: bool = False

    @property
    def address(self) -> str:
        return f"{self.ip}:{self.port}"

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "Server":
        return Server(
            name=str(data.get("name", "?")),
            ip=str(data.get("ip", "")),
            port=str(data.get("port", "")),
            favorite=bool(data.get("favorite", False)),
        )


@dataclass
class SoundEvents:
    """Пути к .wav для разных событий. None = стандартный системный сигнал."""
    detected: Optional[str] = None   # похоже на щелчок, ещё не подтверждено
    launch: Optional[str] = None     # успешный запуск игры
    error: Optional[str] = None      # ошибка запуска

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: Optional[dict]) -> "SoundEvents":
        data = data or {}
        return SoundEvents(
            detected=data.get("detected"),
            launch=data.get("launch"),
            error=data.get("error"),
        )


@dataclass
class AppConfig:
    threshold: float = 0.18
    cooldown: float = 15.0
    sound_enabled: bool = True
    sounds: SoundEvents = field(default_factory=SoundEvents)
    notify_enabled: bool = True
    autostart: bool = False
    skip_if_running: bool = True
    skip_if_steam_updating: bool = True
    detection_only: bool = False  # только уведомление, игра не запускается
    hotkey: str = "ctrl+alt+g"
    pause_hotkey: str = "ctrl+alt+p"
    countdown_seconds: int = 0
    discord_notify_enabled: bool = False
    discord_display_name: Optional[str] = None
    input_device_index: Optional[int] = None
    adaptive_sensitivity: bool = False
    check_availability_before_launch: bool = False
    servers: list[Server] = field(default_factory=lambda: [Server("Shinri Trial", "80.66.82.229", "27103")])
    active_server_name: str = "Shinri Trial"
    recent_servers: list[str] = field(default_factory=list)  # последние 20-30 использованных имён

    def active_server(self) -> Server:
        for server in self.servers:
            if server.name == self.active_server_name:
                return server
        if self.servers:
            return self.servers[0]
        return Server("Shinri Trial", "80.66.82.229", "27103")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["servers"] = [s.to_dict() if isinstance(s, Server) else s for s in self.servers]
        d["sounds"] = self.sounds.to_dict() if isinstance(self.sounds, SoundEvents) else self.sounds
        return d

    @staticmethod
    def from_dict(data: dict) -> "AppConfig":
        base = AppConfig()
        for key, value in data.items():
            if key == "servers":
                continue
            if key == "sounds":
                continue
            if hasattr(base, key):
                setattr(base, key, value)
        base.servers = [Server.from_dict(s) for s in data.get("servers", [])] or base.servers
        base.sounds = SoundEvents.from_dict(data.get("sounds"))
        return base


def _migrate_legacy(logger) -> Optional[dict]:
    """Если найден старый profiles.json (несколько профилей), берёт из него
    только активный профиль — остальные профили безвозвратно не переносятся,
    т.к. многопрофильность убрана.

    ВАЖНО: после успешной миграции файл переименовывается в
    profiles.json.migrated. Раньше он не трогался вообще, из-за чего
    _migrate_legacy() находил его на КАЖДОМ следующем запуске программы и
    заново перетирал текущий config.json старыми данными профиля — любые
    изменения, сделанные между запусками (никнейм в Discord, сервера,
    хоткеи и т.д.), откатывались назад при каждом перезапуске."""
    if LEGACY_PROFILES_FILE.exists():
        try:
            data = json.loads(LEGACY_PROFILES_FILE.read_text(encoding="utf-8"))
            profiles = data.get("profiles", {})
            active = data.get("active")
            result = None
            if active in profiles:
                logger.info("Миграция: беру активный профиль «%s» из старого profiles.json.", active)
                result = profiles[active]
            elif profiles:
                first = next(iter(profiles))
                logger.info("Миграция: активный профиль не найден, беру первый: «%s».", first)
                result = profiles[first]
            if result is not None:
                try:
                    migrated_marker = LEGACY_PROFILES_FILE.with_suffix(".json.migrated")
                    LEGACY_PROFILES_FILE.replace(migrated_marker)
                    logger.info("Старый profiles.json переименован в %s, чтобы миграция не повторялась.", migrated_marker.name)
                except OSError as e:
                    logger.warning("Не удалось переименовать старый profiles.json: %s", e)
                return result
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Не удалось прочитать старый profiles.json: %s", e)
    return None


def load_config(logger) -> AppConfig:
    migrated = _migrate_legacy(logger)
    if migrated is not None:
        cfg = AppConfig.from_dict(migrated)
        save_config(cfg, logger)
        return cfg
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            return AppConfig.from_dict(data)
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Не удалось прочитать config.json, использую значения по умолчанию: %s", e)
    return AppConfig()


def save_config(cfg: AppConfig, logger) -> None:
    try:
        CONFIG_FILE.write_text(
            json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        logger.error("Не удалось сохранить config.json: %s", e)
