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
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

FROZEN: bool = getattr(sys, "frozen", False)  # True, если это собранный PyInstaller .exe

APP_VERSION = "3.1"
APP_NAME = "SnapToGMod"

# Репозиторий используется для проверки GitHub Releases. Теги релизов должны
# иметь вид "v3.1", "v3.2" и т. п.
GITHUB_REPO = "SevereClaw/SnapToGMod"

# Секреты нельзя хранить в публичном репозитории. Для Discord-уведомлений
# задайте переменную окружения SNAPTOGMOD_DISCORD_WEBHOOK_URL перед запуском.
DISCORD_WEBHOOK_URL = os.environ.get("SNAPTOGMOD_DISCORD_WEBHOOK_URL", "").strip()

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

# Голосовой выбор персонажа (voice_select.py): модель Vosk и скриншоты-шаблоны
# кладутся в папку настроек, а не рядом с .exe — так путь не зависит от того,
# куда пользователь положил сам исполняемый файл, и не теряется при
# автообновлении (updates.py подменяет только сам .exe).
VOICE_DIR: Path = SETTINGS_DIR / "voice"
VOICE_MODEL_DIR: Path = VOICE_DIR / "model"
VOICE_TEMPLATES_DIR: Path = VOICE_DIR / "templates"
# Маленькая русская модель Vosk для автозагрузки из меню трея (пункт "Скачать
# модель распознавания автоматически"). Публичный адрес проекта Vosk — если
# когда-нибудь переедет, автозагрузка перестанет работать, но ручной способ
# (скачать самому и распаковать в VOICE_MODEL_DIR) всегда остаётся рабочим.
VOICE_MODEL_URL = "https://alphacephei.com/vosk/models/vosk-model-small-ru-0.22.zip"
for _voice_path in (VOICE_MODEL_DIR, VOICE_TEMPLATES_DIR):
    try:
        _voice_path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass

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


def slugify_character_name(name: str) -> str:
    """Грубое превращение имени персонажа в безопасное имя файла (для
    иконки-шаблона): оставляет только буквы/цифры, остальное — в "_".
    Не обязано быть красивым — пользователь имя файла не видит."""
    slug = re.sub(r"[^0-9a-zA-Zа-яёА-ЯЁ]+", "_", name.strip().lower()).strip("_")
    return slug or "character"


_CYRILLIC_TO_LATIN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def ascii_safe_filename_slug(name: str) -> str:
    """Как slugify_character_name(), но результат ВСЕГДА чистый ASCII —
    для использования в именах файлов, которые читает cv2/pyautogui
    (pyautogui.locateCenterOnScreen -> pyscreeze -> cv2.imread). cv2.imread
    на Windows не умеет открывать файлы с не-ASCII (например, кириллица) в
    пути — молча возвращает None, и иконка персонажа с русским именем
    никогда не находится на экране, хотя файл на диске реально есть
    ("Failed to read ... file is missing" в логе при живом файле — именно
    этот случай). slugify_character_name() при этом остаётся как есть
    (используется как ключ/лог, не как путь к файлу)."""
    transliterated = "".join(_CYRILLIC_TO_LATIN.get(ch, ch) for ch in name.strip().lower())
    slug = re.sub(r"[^0-9a-zA-Z]+", "_", transliterated).strip("_")
    return slug or "character"


@dataclass
class VoiceCharacter:
    """Один персонаж для голосового выбора: своё слово-триггер и своя
    иконка-шаблон (скриншот из раздела "избранное" на экране выбора
    персонажа). Несколько персонажей могут делить одно и то же
    слово-триггер (например, у двух персонажей один и тот же талант) —
    тогда сработает тот, чьё слово-триггер по итогу окажется ближе к
    услышанному тексту.

    notify_message — свой текст уведомления Windows при успешном выборе
    именно этого персонажа (плейсхолдер {name} подставляется как имя
    персонажа). Пусто — используется текст по умолчанию.
    sound_file — свой .wav, который проигрывается при успешном выборе
    именно этого персонажа. Пусто — играет стандартный системный сигнал
    (как и для остальных звуковых событий программы)."""
    slug: str
    name: str
    trigger_word: str
    icon_file: str
    notify_message: str = ""
    sound_file: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "VoiceCharacter":
        return VoiceCharacter(
            slug=str(data.get("slug", "")) or slugify_character_name(str(data.get("name", "character"))),
            name=str(data.get("name", "?")),
            trigger_word=str(data.get("trigger_word", "")),
            icon_file=str(data.get("icon_file", "")),
            notify_message=str(data.get("notify_message", "") or ""),
            sound_file=str(data.get("sound_file", "") or ""),
        )

    def notify_text(self) -> str:
        """Готовый текст уведомления: свой шаблон (с {name}), а если он не
        задан — текст по умолчанию."""
        template = self.notify_message.strip() or "Персонаж выбран: {name}!"
        return template.replace("{name}", self.name)


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

    # Голосовой выбор персонажа (voice_select.py). Модель Vosk и шаблоны
    # скриншотов не хранятся в конфиге — только пороги и список персонажей.
    voice_select_enabled: bool = False
    voice_characters: list[VoiceCharacter] = field(default_factory=list)
    # voice_trigger_word оставлено только для миграции старых config.json с
    # версии, где был ровно один персонаж (Нагито) — новый код им не
    # пользуется напрямую, слово-триггер теперь хранится в VoiceCharacter.
    voice_trigger_word: str = "удача"
    voice_similarity_threshold: float = 0.85
    voice_match_confidence: float = 0.7
    voice_action_cooldown: float = 3.0
    voice_game_window_keywords: list[str] = field(default_factory=lambda: ["garry's mod", "gmod"])

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
        d["voice_characters"] = [
            c.to_dict() if isinstance(c, VoiceCharacter) else c for c in self.voice_characters
        ]
        return d

    @staticmethod
    def from_dict(data: dict) -> "AppConfig":
        base = AppConfig()
        for key, value in data.items():
            if key in ("servers", "sounds", "voice_characters"):
                continue
            if hasattr(base, key):
                setattr(base, key, value)
        base.servers = [Server.from_dict(s) for s in data.get("servers", [])] or base.servers
        base.sounds = SoundEvents.from_dict(data.get("sounds"))
        base.voice_characters = [VoiceCharacter.from_dict(c) for c in data.get("voice_characters", [])]
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


def _migrate_single_character(cfg: AppConfig, logger) -> None:
    """До появления списка персонажей был ровно один персонаж (Нагито
    Комаэда) с одним словом-триггером в voice_trigger_word и одним файлом
    иконки nagito_icon.png прямо в VOICE_TEMPLATES_DIR. Если персонажи ещё
    не заведены, а след старой настройки виден на диске — переносим его как
    первого персонажа, чтобы уже настроенные слово-триггер и иконка не
    потерялись молча."""
    if cfg.voice_characters:
        return
    legacy_icon = VOICE_TEMPLATES_DIR / "nagito_icon.png"
    if not legacy_icon.exists():
        return
    cfg.voice_characters = [VoiceCharacter(
        slug="nagito",
        name="Нагито Комаэда",
        trigger_word=cfg.voice_trigger_word or "удача",
        icon_file="nagito_icon.png",
    )]
    logger.info(
        "Миграция: старая однoперсонажная настройка голосового выбора (%s) перенесена как первый "
        "персонаж «Нагито Комаэда» в списке персонажей.", legacy_icon.name,
    )


def _migrate_nonascii_icon_filenames(cfg: AppConfig, logger) -> bool:
    """Персонажи, добавленные ДО исправления (icon_file строился из
    кириллического имени), могли получить файл иконки вида
    "char_ибуки.png". Такой файл реально лежит на диске, но
    pyautogui.locateCenterOnScreen (через cv2.imread) на Windows не может
    его прочитать — в логе это выглядит как "Иконка ... не найдена на
    экране" / "Failed to read ... file is missing" при живом файле.
    Здесь такие файлы переименовываются в ASCII-безопасное имя, а
    icon_file персонажа обновляется — без этого пользователю пришлось бы
    вручную переснимать иконку каждого такого персонажа."""
    changed = False
    for character in cfg.voice_characters:
        if not character.icon_file or character.icon_file.isascii():
            continue
        old_path = VOICE_TEMPLATES_DIR / character.icon_file
        if not old_path.exists():
            continue
        new_slug = ascii_safe_filename_slug(character.name)
        taken = {c.icon_file for c in cfg.voice_characters if c is not character}
        new_name = f"char_{new_slug}.png"
        n = 2
        while new_name in taken:
            new_name = f"char_{new_slug}_{n}.png"
            n += 1
        try:
            old_path.replace(VOICE_TEMPLATES_DIR / new_name)
        except OSError as e:
            logger.warning("Не удалось переименовать иконку персонажа «%s» (%s -> %s): %s",
                            character.name, character.icon_file, new_name, e)
            continue
        logger.info(
            "Миграция: иконка персонажа «%s» переименована из %s в %s (кириллица в имени файла "
            "не читалась через cv2/pyautogui на Windows — иконка \"не находилась\" на экране, "
            "хотя файл был на месте).", character.name, character.icon_file, new_name,
        )
        character.icon_file = new_name
        changed = True
    return changed


def load_config(logger) -> AppConfig:
    migrated = _migrate_legacy(logger)
    if migrated is not None:
        cfg = AppConfig.from_dict(migrated)
        _migrate_single_character(cfg, logger)
        _migrate_nonascii_icon_filenames(cfg, logger)
        save_config(cfg, logger)
        return cfg
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            cfg = AppConfig.from_dict(data)
            _migrate_single_character(cfg, logger)
            if _migrate_nonascii_icon_filenames(cfg, logger):
                save_config(cfg, logger)
            return cfg
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Не удалось прочитать config.json, использую значения по умолчанию: %s", e)
    cfg = AppConfig()
    _migrate_single_character(cfg, logger)
    return cfg


def save_config(cfg: AppConfig, logger) -> None:
    try:
        CONFIG_FILE.write_text(
            json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as e:
        logger.error("Не удалось сохранить config.json: %s", e)
