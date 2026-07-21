"""
voice_select.py
----------------
Голосовой выбор персонажа по слову-триггеру, распознанному офлайн-моделью
Vosk (изначально — отдельный скрипт select_nagito.py для одного персонажа,
Нагито Комаэды).

Персонажей может быть несколько (см. config.VoiceCharacter,
cfg.voice_characters) — у каждого своё слово-триггер и своя иконка-шаблон
(скриншот из раздела "избранное" на экране выбора персонажа). Меню паузы и
кнопка "Выбрать персонажа" на экране одни и те же для всех персонажей, эти
два шаблона (TEMPLATE_MENU_ITEM, TEMPLATE_SELECT_BUTTON) общие. Персонажи с
одинаковым словом-триггером (например, общий талант "Удача" у двух разных
персонажей в разных играх серии) не конфликтуют: при распознавании
побеждает тот, чьё слово-триггер оказалось ближе всего к услышанному
тексту (см. match_character) — а не первый по порядку.

Полностью опционально:
  - если библиотеки vosk / pyautogui / pydirectinput / pygetwindow не
    установлены — DEPENDENCIES_AVAILABLE=False, функция остаётся выключенной
    и не мешает работе остальной программы (по аналогии с hotkeys.py, где
    отсутствие библиотеки 'keyboard' не роняет программу);
  - если модель Vosk не распакована в config.VOICE_MODEL_DIR — load()
    возвращает False и пишет причину в logger, работа продолжается без
    голосового выбора.

Микрофон здесь НЕ открывается отдельным потоком. audio.py уже держит один
sd.InputStream для детектора щелчка — открывать второй независимый поток
к тому же устройству на Windows нередко приводит к конфликту (особенно в
эксклюзивном режиме WASAPI). Поэтому process_block() вызывается прямо из
колбэка audio.py на каждом блоке, а сам блок (обычно 44100 Гц) внутри
пересэмплируется до 16000 Гц, которые ожидает модель Vosk.
"""
from __future__ import annotations

import difflib
import json
import shutil
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional

import numpy as np

import sound
from config import (
    AppConfig, VOICE_DIR, VOICE_MODEL_DIR, VOICE_MODEL_URL, VOICE_TEMPLATES_DIR,
    VoiceCharacter, slugify_character_name, ascii_safe_filename_slug,
)

try:
    import vosk
except ImportError:
    vosk = None

try:
    import pyautogui
    from pyautogui import ImageNotFoundException
except ImportError:
    pyautogui = None
    ImageNotFoundException = Exception  # заглушка, чтобы except ниже не падал

try:
    import pydirectinput
except ImportError:
    pydirectinput = None

try:
    import pygetwindow as gw
except ImportError:
    gw = None

# cv2 (пакет opencv-python) нигде в этом файле напрямую не импортируется, но
# pyautogui.locateCenterOnScreen(..., confidence=...) молча делает `import cv2`
# ВНУТРИ себя в момент вызова, а не при импорте pyautogui. Если cv2 нет (не
# установлен pip-пакет opencv-python ИЛИ .exe собран PyInstaller-ом из venv,
# где его не было — см. комментарий про collect_all() в SnapToGMod.spec), то
# раньше это нигде не было видно, кроме одной строки в логе: программа
# нажимала ESC, но find() дальше всегда "не находил" первый же шаблон — со
# стороны выглядело как "просто открылась пауза и ничего не происходит".
# Поэтому cv2 теперь проверяется здесь явно, как отдельная зависимость.
try:
    import cv2
except ImportError:
    cv2 = None

_REQUIRED_MODULES = {
    "vosk": vosk, "pyautogui": pyautogui, "pydirectinput": pydirectinput,
    "pygetwindow": gw, "cv2 (opencv-python)": cv2,
}
MISSING_DEPENDENCIES: list[str] = [name for name, mod in _REQUIRED_MODULES.items() if mod is None]
DEPENDENCIES_AVAILABLE: bool = not MISSING_DEPENDENCIES

TARGET_SAMPLE_RATE = 16000

# Если загрузка модели Vosk (синхронный вызов vosk.Model()/KaldiRecognizer()
# внутри VoiceSelectEngine.load()) идёт дольше этого — почти наверняка модель
# повреждена/распакована неправильно, а не "просто медленный диск". Значение
# влияет только на текст в трее (Python не может безопасно прервать чужой
# нативный вызов на середине) — но хотя бы перестаёт молча висеть с
# "модель загружается..." до бесконечности без единой подсказки.
LOADING_STUCK_WARNING_SECONDS = 20.0

# Подпапки, которые vosk.Model() ожидает найти ПРЯМО внутри VOICE_MODEL_DIR.
# Если модель распакована на уровень глубже (частая ошибка при ручной
# установке — папку-архив "vosk-model-small-ru-0.22" просто скопировали
# ВНУТРЬ voice/model/, вместо того чтобы перенести её содержимое), этих
# подпапок не будет видно на верхнем уровне — тогда сообщаем об этом сразу и
# не вызываем vosk.Model() вовсе.
_EXPECTED_MODEL_SUBDIRS = ("am", "conf")

# Имена файлов-шаблонов ОБЩИХ для всех персонажей элементов интерфейса
# (пункт меню паузы и кнопка подтверждения выбора) — пользователь кладёт их
# в VOICE_TEMPLATES_DIR. Иконка самого персонажа — своя у каждого
# VoiceCharacter (character.icon_file), см. add_character() ниже.
TEMPLATE_MENU_ITEM = "menu_choose_character.png"
TEMPLATE_SELECT_BUTTON = "select_button.png"


def model_is_present() -> bool:
    return VOICE_MODEL_DIR.exists() and any(VOICE_MODEL_DIR.iterdir())


def validate_model_dir() -> Optional[str]:
    """Проверяет, что VOICE_MODEL_DIR похожа на распакованную модель Vosk,
    ДО вызова vosk.Model() (который на "почти правильной, но не совсем"
    папке иногда не бросает питоновское исключение с понятным текстом, а
    либо падает совсем непонятно, либо подвисает надолго). Возвращает None,
    если всё похоже на правду, иначе — готовый для показа пользователю текст
    причины (что именно не так и как это поправить)."""
    if not model_is_present():
        return f"модель не найдена в {VOICE_MODEL_DIR}"
    missing = [d for d in _EXPECTED_MODEL_SUBDIRS if not (VOICE_MODEL_DIR / d).exists()]
    if not missing:
        return None
    # Частый случай: пользователь скопировал папку-архив ЦЕЛИКОМ внутрь
    # VOICE_MODEL_DIR, вместо того чтобы перенести её содержимое. Пробуем
    # найти такую вложенную папку и подсказать точное имя.
    subdirs = [p for p in VOICE_MODEL_DIR.iterdir() if p.is_dir()]
    for sub in subdirs:
        if all((sub / d).exists() for d in _EXPECTED_MODEL_SUBDIRS):
            return (
                f"модель распакована на один уровень глубже, чем нужно: найдена папка "
                f"«{sub.name}» внутри {VOICE_MODEL_DIR}. Перенесите ВСЁ СОДЕРЖИМОЕ папки "
                f"«{sub.name}» прямо в {VOICE_MODEL_DIR} (а саму папку «{sub.name}» после "
                f"этого можно удалить)."
            )
    return (
        f"в {VOICE_MODEL_DIR} нет ожидаемых папок модели ({', '.join(_EXPECTED_MODEL_SUBDIRS)}) — "
        "похоже, это не распакованная модель Vosk целиком, а что-то другое (например, сам .zip-файл "
        "или пустая/битая распаковка). Скачайте модель заново."
    )


def download_model(logger, progress_cb: Optional[Callable[[int, int], None]] = None) -> tuple[bool, str]:
    """Скачивает маленькую русскую модель Vosk (см. config.VOICE_MODEL_URL) и
    распаковывает её прямо в VOICE_MODEL_DIR. Нужен интернет; вызывать из
    фонового потока — занимает от нескольких секунд до пары минут в
    зависимости от скорости соединения. progress_cb(скачано_байт, всего_байт)
    вызывается по ходу скачивания — total может быть 0, если сервер не прислал
    Content-Length. Возвращает (успех, текст_ошибки)."""
    VOICE_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = VOICE_DIR / "_model_download.zip"
    extract_dir = VOICE_DIR / "_model_extract_tmp"
    try:
        req = urllib.request.Request(VOICE_MODEL_URL, headers={"User-Agent": "SnapToGMod"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            total = int(resp.headers.get("Content-Length", 0) or 0)
            downloaded = 0
            with open(zip_path, "wb") as f:
                while True:
                    chunk = resp.read(256 * 1024)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        try:
                            progress_cb(downloaded, total)
                        except Exception:
                            pass
        logger.info("Голосовой выбор персонажа: модель скачана (%d байт), распаковываю...", downloaded)

        if extract_dir.exists():
            shutil.rmtree(extract_dir, ignore_errors=True)
        extract_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(extract_dir)

        # Архив модели обычно содержит один вложенный каталог вида
        # "vosk-model-small-ru-0.22/" — переносим его СОДЕРЖИМОЕ прямо в
        # VOICE_MODEL_DIR (а не саму папку-обёртку), т.к. vosk.Model() ждёт
        # путь, где сразу лежат "am", "conf" и т.д.
        entries = list(extract_dir.iterdir())
        source_dir = entries[0] if len(entries) == 1 and entries[0].is_dir() else extract_dir

        if VOICE_MODEL_DIR.exists():
            shutil.rmtree(VOICE_MODEL_DIR, ignore_errors=True)
        shutil.move(str(source_dir), str(VOICE_MODEL_DIR))

        logger.info("Голосовой выбор персонажа: модель распакована в %s.", VOICE_MODEL_DIR)
        return True, ""
    except urllib.error.URLError as e:
        return False, f"нет соединения или сервер недоступен: {e}"
    except Exception as e:
        return False, str(e)
    finally:
        try:
            if zip_path.exists():
                zip_path.unlink()
        except OSError:
            pass
        try:
            if extract_dir.exists():
                shutil.rmtree(extract_dir, ignore_errors=True)
        except OSError:
            pass


def trigger_similarity(text: str, trigger_word: str) -> float:
    """Та же логика сравнения, что и внутри VoiceSelectEngine, но без
    зависимости от живого экземпляра — используется и распознавателем, и
    окном диагностики (проверка слова-триггера без микрофона)."""
    compact = text.replace(" ", "")
    if not compact or not trigger_word:
        return 0.0
    return difflib.SequenceMatcher(None, compact, trigger_word).ratio()


def match_character(
    text: str, characters: list[VoiceCharacter], threshold: float,
) -> Optional[VoiceCharacter]:
    """Ищет персонажа, чьё слово-триггер лучше всего совпало с услышанным
    текстом. Раньше был ровно один персонаж и одно слово-триггер — теперь их
    может быть несколько, в том числе с ОДИНАКОВЫМ словом-триггером
    (например, общий талант "Удача" у разных персонажей). В этом случае
    побеждает не первый по списку, а тот, у кого похожесть выше — иначе при
    двух персонажах на одно слово всегда выбирался бы только первый
    добавленный, а остальные были бы недостижимы."""
    best: Optional[VoiceCharacter] = None
    best_ratio = 0.0
    for character in characters:
        ratio = trigger_similarity(text, character.trigger_word)
        if ratio >= threshold and ratio > best_ratio:
            best = character
            best_ratio = ratio
    return best


def character_icon_path(character: VoiceCharacter) -> Path:
    return VOICE_TEMPLATES_DIR / character.icon_file


def unique_character_icon_filename(name: str, existing: list[VoiceCharacter]) -> str:
    """Генерирует имя файла иконки вида char_<slug>.png, не занятое другим
    персонажем в списке — на случай двух персонажей с похожим/одинаковым
    именем (например, "Макото" и "Макото (Danganronpa 3)").

    Слаг здесь всегда транслитерируется в ASCII (см. ascii_safe_filename_slug):
    cv2.imread (через pyautogui.locateCenterOnScreen) на Windows не читает
    файлы с кириллицей в пути — при русском имени персонажа файл создавался
    бы, но потом никогда не находился бы на экране."""
    base_slug = ascii_safe_filename_slug(name)
    taken = {c.icon_file for c in existing}
    candidate = f"char_{base_slug}.png"
    n = 2
    while candidate in taken:
        candidate = f"char_{base_slug}_{n}.png"
        n += 1
    return candidate


def add_character(cfg: AppConfig, name: str, trigger_word: str) -> VoiceCharacter:
    """Создаёт нового персонажа (без иконки — её сохраняют отдельно через
    save_template_from_region(character.icon_file, rect)) и добавляет его в
    cfg.voice_characters. Не сохраняет конфиг на диск — это делает вызывающий
    код (обычно сразу после сохранения иконки, одним save_config())."""
    slug = slugify_character_name(name)
    taken_slugs = {c.slug for c in cfg.voice_characters}
    unique_slug = slug
    n = 2
    while unique_slug in taken_slugs:
        unique_slug = f"{slug}_{n}"
        n += 1
    character = VoiceCharacter(
        slug=unique_slug,
        name=name.strip(),
        trigger_word=trigger_word.strip().lower(),
        icon_file=unique_character_icon_filename(name, cfg.voice_characters),
    )
    cfg.voice_characters.append(character)
    return character


def remove_character(cfg: AppConfig, character: VoiceCharacter, delete_icon_file: bool = True) -> None:
    """Убирает персонажа из cfg.voice_characters (по slug) и, если
    delete_icon_file=True и его иконку не переиспользует никто другой,
    удаляет файл иконки с диска — чтобы папка шаблонов не копила мусор от
    удалённых персонажей."""
    cfg.voice_characters = [c for c in cfg.voice_characters if c.slug != character.slug]
    if not delete_icon_file:
        return
    if any(c.icon_file == character.icon_file for c in cfg.voice_characters):
        return  # тот же файл иконки всё ещё используется другим персонажем
    try:
        path = character_icon_path(character)
        if path.exists():
            path.unlink()
    except OSError:
        pass


class VoiceHooks:
    """Колбэки в UI-слой (tray.py), по аналогии с launcher.TriggerHooks —
    этот модуль ничего не знает про pystray/tkinter."""

    def __init__(
        self,
        on_heard: Optional[Callable[[], None]] = None,
        on_result: Optional[Callable[[bool, str], None]] = None,
    ):
        self.on_heard = on_heard
        self.on_result = on_result


class VoiceSelectEngine:
    """Состояние голосового выбора: модель Vosk, распознаватель, защита от
    повторных срабатываний. Создаётся один раз в main.py и живёт всё время
    работы программы; модель Vosk подгружается лениво — только когда
    функция включена в настройках (тумблер в трее)."""

    def __init__(self, cfg: AppConfig, hooks: VoiceHooks):
        self.cfg = cfg
        self.hooks = hooks
        self._model = None
        self._recognizer = None
        self._lock = threading.Lock()
        self.ready = False
        self.loading = False
        self.loading_started_at: Optional[float] = None
        self.last_error: Optional[str] = None
        self._last_action_time = 0.0

    @property
    def status_text(self) -> str:
        """Короткое описание состояния для пункта меню трея."""
        if not DEPENDENCIES_AVAILABLE:
            return f"не установлены библиотеки: {', '.join(MISSING_DEPENDENCIES)}"
        if self.loading:
            elapsed = time.time() - self.loading_started_at if self.loading_started_at else 0.0
            if elapsed > LOADING_STUCK_WARNING_SECONDS:
                return (
                    f"модель загружается уже {elapsed:.0f} сек — похоже, зависла. "
                    "Проверьте папку модели через «Диагностика и гайд»."
                )
            return "модель загружается..."
        if self.ready:
            if not self.cfg.voice_characters:
                return "готово, но персонажей не заведено — добавьте хотя бы одного"
            return f"готово ({len(self.cfg.voice_characters)} перс.)"
        if self.last_error:
            return f"ошибка: {self.last_error}"
        return "выключено"

    # ---------- загрузка/выгрузка модели ----------

    def load(self, logger) -> bool:
        """Загружает модель Vosk с диска. Вызывать из фонового потока —
        занимает от долей секунды до нескольких секунд в зависимости от
        размера модели. Повторный вызов, пока уже готово, ничего не делает."""
        if not DEPENDENCIES_AVAILABLE:
            self.last_error = "не установлены нужные библиотеки"
            logger.warning(
                "Голосовой выбор персонажа: %s (%s). Выполните: pip install -r requirements-voice.txt",
                self.last_error, ", ".join(MISSING_DEPENDENCIES),
            )
            return False
        with self._lock:
            if self.ready:
                return True
            problem = validate_model_dir()
            if problem:
                self.last_error = problem
                logger.warning("Голосовой выбор персонажа: %s", problem)
                return False
            self.loading = True
            self.loading_started_at = time.time()
            try:
                vosk.SetLogLevel(-1)  # не засорять наш журнал внутренними логами Vosk/Kaldi
                model = vosk.Model(str(VOICE_MODEL_DIR))
                recognizer = vosk.KaldiRecognizer(model, TARGET_SAMPLE_RATE)
                recognizer.SetWords(False)
                self._model = model
                self._recognizer = recognizer
                self.ready = True
                self.last_error = None
                logger.info("Голосовой выбор персонажа: модель Vosk загружена (%s).", VOICE_MODEL_DIR)
                return True
            except Exception as e:
                self.last_error = str(e)
                logger.error("Голосовой выбор персонажа: не удалось загрузить модель Vosk: %s", e)
                return False
            finally:
                self.loading = False
                self.loading_started_at = None

    def load_async(self, logger) -> None:
        threading.Thread(target=self.load, args=(logger,), daemon=True).start()

    def unload(self, logger) -> None:
        with self._lock:
            self._model = None
            self._recognizer = None
            self.ready = False
        logger.info("Голосовой выбор персонажа: выключен, модель выгружена из памяти.")

    # ---------- ресемплинг ----------

    @staticmethod
    def _resample(block: np.ndarray, orig_sr: int) -> np.ndarray:
        """Линейный ресемплинг одного блока в TARGET_SAMPLE_RATE. Точная фаза
        между соседними блоками не сохраняется — только длина и частота
        каждого блока по отдельности. Для распознавания одного короткого
        слова-триггера с нечётким сравнением (см. _contains_trigger) лишние
        щелчки на границах блоков не критичны, а полноценный полифазный
        ресемплер с состоянием — не оправданная здесь сложность."""
        if orig_sr == TARGET_SAMPLE_RATE:
            return block.astype(np.float32)
        n_out = max(1, int(round(len(block) * TARGET_SAMPLE_RATE / orig_sr)))
        x_old = np.linspace(0.0, 1.0, num=len(block), endpoint=False)
        x_new = np.linspace(0.0, 1.0, num=n_out, endpoint=False)
        return np.interp(x_new, x_old, block).astype(np.float32)

    # ---------- обработка аудио (вызывается из колбэка audio.py) ----------

    def process_block(self, audio_block: np.ndarray, orig_sr: int, logger) -> None:
        """Распознавание Vosk по одному блоку — быстро (единицы мс на блок
        ~1024 сэмпла), поэтому делается синхронно в колбэке, как и FFT-анализ
        формы щелчка рядом в audio.py. А вот поиск шаблонов на экране и клики
        (_run_action) — не быстрые и поэтому уходят в отдельный поток."""
        if not self.ready or self._recognizer is None:
            return
        resampled = self._resample(audio_block, orig_sr)
        pcm16 = (np.clip(resampled, -1.0, 1.0) * 32767.0).astype(np.int16).tobytes()

        with self._lock:
            if self._recognizer is None:
                return
            try:
                if self._recognizer.AcceptWaveform(pcm16):
                    text = json.loads(self._recognizer.Result()).get("text", "")
                else:
                    text = json.loads(self._recognizer.PartialResult()).get("partial", "")
            except Exception as e:
                logger.warning("Голосовой выбор персонажа: ошибка распознавания: %s", e)
                return

        if not text:
            return
        character = self._match_character(text)
        if character is not None:
            with self._lock:
                if self._recognizer is not None:
                    try:
                        self._recognizer.Reset()  # чтобы то же слово не сработало повторно на финальном результате
                    except Exception:
                        pass
            self._maybe_trigger(logger, character)

    def _match_character(self, text: str) -> Optional[VoiceCharacter]:
        """Слова-триггеры персонажей часто отсутствуют в словаре маленькой
        модели ("Нагито" она слышит как несколько обычных слов) — поэтому
        сравнение нечёткое, а не точное совпадение. Несколько персонажей
        могут делить одно слово-триггер (общий талант) — см. match_character."""
        return match_character(text, self.cfg.voice_characters, self.cfg.voice_similarity_threshold)

    def _maybe_trigger(self, logger, character: VoiceCharacter) -> None:
        now = time.time()
        if now - self._last_action_time < self.cfg.voice_action_cooldown:
            return
        self._last_action_time = now
        logger.info("Голосовой выбор персонажа: слово-триггер услышано, выбран персонаж «%s».", character.name)
        if self.hooks.on_heard:
            self.hooks.on_heard()
        threading.Thread(target=self._run_action, args=(logger, character), daemon=True).start()

    # ---------- действие на экране ----------

    def _is_game_focused(self) -> bool:
        """Проверка, что сейчас в фокусе именно окно игры, а не браузер/чат —
        иначе ESC и клики уйдут не туда."""
        try:
            active = gw.getActiveWindow()
        except Exception:
            return True  # не удалось определить — не блокируем работу
        if active is None or not active.title:
            return False
        title = active.title.lower()
        return any(keyword.lower() in title for keyword in self.cfg.voice_game_window_keywords)

    def _find(self, filename: str):
        path = VOICE_TEMPLATES_DIR / filename
        if not path.exists():
            return None, f"нет файла-шаблона «{filename}» в {VOICE_TEMPLATES_DIR}"
        if cv2 is None:
            # pyautogui.locateCenterOnScreen(..., confidence=...) молча делает
            # `import cv2` внутри себя и без него результат ВСЕГДА "не найдено",
            # даже если шаблон идеально совпадает с экраном. Раньше это тонуло
            # в общем except ниже и выглядело как "шаблон не найден", хотя
            # реальная причина — не установлен/не собран в .exe opencv-python.
            return None, "не установлен opencv-python (cv2) — сравнение по confidence не работает совсем"
        try:
            pos = pyautogui.locateCenterOnScreen(str(path), confidence=self.cfg.voice_match_confidence)
            return pos, None
        except ImageNotFoundException:
            return None, None
        except Exception as e:
            return None, str(e)

    @staticmethod
    def _click(pos) -> None:
        """Клик через pydirectinput — обычный клик pyautogui Source-движок
        (GMod) часто игнорирует: курсор двигается, а сам клик не засчитывается."""
        x, y = int(pos.x), int(pos.y)
        pydirectinput.moveTo(x, y)
        time.sleep(0.1)
        pydirectinput.click(x, y)

    def _fail(self, logger, message: str) -> None:
        logger.warning("Голосовой выбор персонажа: %s", message)
        if self.hooks.on_result:
            self.hooks.on_result(False, message)

    def _run_action(self, logger, character: VoiceCharacter) -> None:
        if not self._is_game_focused():
            logger.info("Голосовой выбор персонажа: слово услышано, но игра сейчас не в фокусе — пропускаю.")
            return
        if not character.icon_file:
            self._fail(logger, f"у персонажа «{character.name}» не задана иконка — переснимите её в менеджере персонажей.")
            return
        try:
            pydirectinput.press("esc")
            time.sleep(0.6)

            pos, err = self._find(TEMPLATE_MENU_ITEM)
            if not pos:
                suffix = f" ({err})" if err else ""
                self._fail(logger, f"Пункт меню «Выбрать персонажа» не найден на экране.{suffix}")
                return
            self._click(pos)
            time.sleep(0.8)  # даём экрану выбора персонажа полностью открыться

            # Иконка ищется именно та, что привязана к распознанному
            # персонажу (character.icon_file) — раньше здесь был один
            # захардкоженный файл nagito_icon.png, поэтому распознанное
            # слово-триггер вообще не влияло на то, ЧЬЮ иконку кликнет
            # скрипт: всегда кликался один и тот же нагито, независимо от
            # того, кого на самом деле назвал пользователь.
            pos, err = self._find(character.icon_file)
            if not pos:
                suffix = f" ({err})" if err else ""
                self._fail(logger, f"Иконка персонажа «{character.name}» не найдена на экране выбора.{suffix}")
                return
            self._click(pos)
            time.sleep(0.4)

            pos, err = self._find(TEMPLATE_SELECT_BUTTON)
            if not pos:
                suffix = f" ({err})" if err else ""
                self._fail(logger, f"Кнопка «Выбрать персонажа» не найдена.{suffix}")
                return
            self._click(pos)

            logger.info("Голосовой выбор персонажа: «%s» выбран(а).", character.name)
            sound.play_character_sound(self.cfg, character, logger)
            if self.hooks.on_result:
                self.hooks.on_result(True, character.notify_text())
        except Exception as e:
            self._fail(logger, f"ошибка во время действия: {e}")


# ==================== ДИАГНОСТИКА (для окна "Диагностика и гайд" в трее) ====================
# Набор независимых, безопасных для вызова из UI-потока проверок. Ничего не
# запускает в фоне и не трогает состояние живого VoiceSelectEngine — только
# читает диск/пробует cv2/pyautogui. Возвращает данные, окно в tray.py само
# решает, как их показать.

# Общие для всех персонажей файлы — иконки персонажей проверяются отдельно
# (template_status(cfg) ниже принимает и их, по одному на персонажа).
TEMPLATE_FILES = (TEMPLATE_MENU_ITEM, TEMPLATE_SELECT_BUTTON)


def save_template_from_region(filename: str, region: tuple[int, int, int, int]) -> tuple[bool, str]:
    """Делает скриншот прямоугольной области экрана (left, top, width, height)
    и сохраняет как PNG-шаблон с нужным именем прямо в VOICE_TEMPLATES_DIR.
    Используется мастером настройки в трее — чтобы не заставлять человека
    вручную делать скриншот, обрезать его в редакторе и класть файл с
    правильным именем в правильную папку самостоятельно."""
    if pyautogui is None:
        return False, "pyautogui не установлен"
    left, top, w, h = region
    if w < 8 or h < 8:
        return False, "выделенная область слишком маленькая"
    try:
        VOICE_TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
        img = pyautogui.screenshot(region=(left, top, w, h))
        img.save(str(VOICE_TEMPLATES_DIR / filename))
        return True, f"сохранено ({w}x{h}px)"
    except Exception as e:
        return False, str(e)


def cv2_self_test() -> tuple[bool, str]:
    """Проверяет не просто "cv2 импортируется", а что связка pyautogui+cv2
    реально может выполнить сравнение по confidence — именно это молча падает
    при недостающем/несобранном в .exe opencv-python и выглядит как "шаблон
    никогда не находится"."""
    if cv2 is None:
        return False, "opencv-python (cv2) не установлен или не попал в сборку .exe"
    if pyautogui is None:
        return False, "pyautogui не установлен"
    try:
        import numpy as _np
        from PIL import Image as _Image
        blank = _Image.fromarray(_np.zeros((20, 20, 3), dtype=_np.uint8))
        # Ищем заведомо отсутствующий узор в пустом изображении — здесь важен
        # не результат (его не будет), а то, что confidence-путь вообще
        # отрабатывает без исключения "OpenCV must be installed...".
        pyautogui.locate(blank, blank, confidence=0.9)
        return True, "opencv-python работает"
    except ImageNotFoundException:
        return True, "opencv-python работает"
    except Exception as e:
        return False, f"сравнение по confidence не работает: {e}"


def template_status(filenames: Optional[tuple] = None) -> list[dict]:
    """Для каждого файла-шаблона: есть ли файл и его размер в пикселях
    (сильно отличающийся от текущего разрешения экрана размер — частая
    причина, почему шаблон "не находится", даже если он на экране точно
    есть — см. screen_resolution()). По умолчанию проверяет только два
    общих шаблона (меню/кнопка) — иконки персонажей передаются отдельно."""
    results = []
    for name in (filenames if filenames is not None else TEMPLATE_FILES):
        path = VOICE_TEMPLATES_DIR / name
        entry = {"name": name, "path": str(path), "exists": path.exists(), "size": None}
        if entry["exists"]:
            try:
                from PIL import Image as _Image
                with _Image.open(path) as img:
                    entry["size"] = img.size
            except Exception as e:
                entry["exists"] = False
                entry["error"] = f"файл повреждён или не читается: {e}"
        results.append(entry)
    return results


def screen_resolution() -> Optional[tuple[int, int]]:
    if pyautogui is None:
        return None
    try:
        size = pyautogui.size()
        return int(size.width), int(size.height)
    except Exception:
        return None


def try_locate_template(name: str, confidence: float) -> tuple[bool, str]:
    """Живая попытка найти один шаблон на экране прямо сейчас — для кнопки
    «Проверить на экране» в окне диагностики. Не кликает, не жмёт ESC —
    только ищет."""
    if cv2 is None or pyautogui is None:
        return False, "opencv-python (cv2) или pyautogui недоступны"
    path = VOICE_TEMPLATES_DIR / name
    if not path.exists():
        return False, f"нет файла «{name}»"
    try:
        pos = pyautogui.locateCenterOnScreen(str(path), confidence=confidence)
        if pos:
            return True, f"найдено на экране в точке ({int(pos.x)}, {int(pos.y)})"
        return False, "не найдено на экране"
    except ImageNotFoundException:
        return False, "не найдено на экране"
    except Exception as e:
        return False, str(e)


def run_diagnostics(cfg: AppConfig) -> dict:
    """Собирает полный отчёт для окна «Диагностика и гайд». Ничего не
    ломает и не запускает долгих операций (кроме cv2_self_test, который
    работает на пустой картинке и занимает миллисекунды)."""
    cv2_ok, cv2_msg = cv2_self_test()
    return {
        "dependencies_available": DEPENDENCIES_AVAILABLE,
        "missing_dependencies": list(MISSING_DEPENDENCIES),
        "cv2_ok": cv2_ok,
        "cv2_msg": cv2_msg,
        "model_present": model_is_present(),
        "model_dir": str(VOICE_MODEL_DIR),
        "model_problem": validate_model_dir(),
        "templates_dir": str(VOICE_TEMPLATES_DIR),
        "templates": template_status(),  # общие: пункт меню + кнопка выбора
        "characters": [
            {
                "character_name": c.name,
                "trigger_word": c.trigger_word,
                "icon_file": c.icon_file,
                **{k: v for k, v in template_status((c.icon_file,))[0].items() if k != "name"},
            }
            for c in cfg.voice_characters
        ],
        "screen_resolution": screen_resolution(),
        "similarity_threshold": cfg.voice_similarity_threshold,
        "match_confidence": cfg.voice_match_confidence,
    }
