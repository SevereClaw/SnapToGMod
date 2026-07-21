"""
Голосовой выбор персонажа "Нагито Комаэда" в Garry's Mod (сервер Shinri Trial).

УСТАРЕЛО: это самый первый, отдельный вариант скрипта под ровно одного
персонажа. Основное приложение (main.py + voice_select.py) с версии со
списком персонажей (config.VoiceCharacter) поддерживает произвольное
число персонажей со своими словами-триггерами и иконками через трей ->
"Голосовой выбор персонажа" -> "Персонажи..." — используйте его, если
нужно выбирать между несколькими персонажами (например, Нагито и Макото
на общий талант "Удача"). Этот файл оставлен только для справки/истории.

Как это работает:
  1. Скрипт постоянно слушает микрофон через офлайн-модель Vosk.
  2. Как только в распознанном тексте встречается слово "нагито",
     скрипт открывает меню (ESC), находит на экране иконку персонажа
     по картинке-образцу, кликает по ней, затем кликает
     "ВЫБРАТЬ ПЕРСОНАЖА".

ПЕРЕД ЗАПУСКОМ (см. подробную инструкцию в чате):
  1) pip install vosk sounddevice pyautogui pydirectinput pillow opencv-python pygetwindow
  2) Скачать маленькую русскую модель Vosk и распаковать её,
     указать путь в MODEL_PATH.
  3) Сделать 2 скриншота-обрезка (crop) с игры и положить рядом со скриптом:
       nagito_icon.png     - иконка Нагито в разделе "ИЗБРАННОЕ"
       select_button.png   - кнопка "ВЫБРАТЬ ПЕРСОНАЖА"
  4) Игру лучше запускать в оконном безрамочном режиме (Borderless),
     иначе pyautogui может не видеть содержимое экрана.
"""

import sys
import queue
import json
import time
import difflib
from pathlib import Path

import sounddevice as sd
import vosk
import pyautogui
import pydirectinput
import pygetwindow as gw
from pyautogui import ImageNotFoundException

# ---------- НАСТРОЙКИ ----------
MODEL_PATH = "vosk-model-small-ru-0.22"   # папка с распакованной моделью Vosk
TRIGGER_WORD = "удача"

CHAR_ICON_TEMPLATE = "nagito_icon.png"
SELECT_BUTTON_TEMPLATE = "select_button.png"
PAUSE_MENU_ITEM_TEMPLATE = "menu_choose_character.png"  # пункт "Выбрать персонажа" в меню ESC

MATCH_CONFIDENCE = 0.7    # если иконка не находится - понизить до 0.5-0.6
ACTION_COOLDOWN = 3.0     # сек. защита от повторного срабатывания подряд
SIMILARITY_THRESHOLD = 0.85   # порог "похожести" на слово-триггер (0..1)

# Проверка, что активно именно окно игры, а не браузер/дискорд/чат.
# Впиши сюда часть заголовка окна GMod (регистр не важен).
GAME_WINDOW_KEYWORDS = ["garry's mod", "gmod"]
# --------------------------------

q = queue.Queue()
_last_trigger_time = 0.0


def contains_trigger(text: str) -> bool:
    """
    "Нагито" отсутствует в словаре модели, поэтому она слышит его как
    два обычных слова, например "на гетто"/"на гита"/"на гидов".
    Склеиваем весь распознанный текст без пробелов и сравниваем со
    словом-триггером - это ловит такие случаи надёжнее точного совпадения.
    """
    compact = text.replace(" ", "")
    if not compact:
        return False
    ratio = difflib.SequenceMatcher(None, compact, TRIGGER_WORD).ratio()
    if ratio >= 0.4:  # печатаем только когда есть хоть какое-то сходство
        print(f"    (похожесть на '{TRIGGER_WORD}': {ratio:.2f})")
    return ratio >= SIMILARITY_THRESHOLD


def audio_callback(indata, frames, time_info, status):
    if status:
        print(status, file=sys.stderr)
    q.put(bytes(indata))


def find_on_screen(template_path, confidence):
    """Ищет картинку на экране. Возвращает координаты центра или None,
    не роняя программу, если ничего не найдено."""
    try:
        return pyautogui.locateCenterOnScreen(template_path, confidence=confidence)
    except ImageNotFoundException:
        return None


def is_game_focused() -> bool:
    """Проверяет, что сейчас в фокусе именно окно игры, а не браузер/чат."""
    try:
        active = gw.getActiveWindow()
    except Exception:
        return True  # если не удалось определить - не блокируем работу скрипта
    if active is None or not active.title:
        return False
    title = active.title.lower()
    return any(keyword in title for keyword in GAME_WINDOW_KEYWORDS)


def game_click(pos):
    """Клик через pydirectinput - обычный pyautogui.click() Source-движок
    (GMod) часто игнорирует, курсор двигается, а сам клик не засчитывается."""
    x, y = int(pos.x), int(pos.y)
    pydirectinput.moveTo(x, y)
    time.sleep(0.1)
    pydirectinput.click(x, y)


def select_nagito():
    global _last_trigger_time
    now = time.time()
    if now - _last_trigger_time < ACTION_COOLDOWN:
        return
    _last_trigger_time = now

    if not is_game_focused():
        print("[.] Слово услышано, но игра сейчас не в фокусе — пропускаю")
        return

    print("[*] Триггер услышан — выбираю Нагито Комаэда")

    pydirectinput.press('esc')
    time.sleep(0.6)

    # Шаг 1: в меню паузы кликаем пункт "Выбрать персонажа",
    # чтобы открылся экран выбора персонажа.
    menu_item_pos = find_on_screen(PAUSE_MENU_ITEM_TEMPLATE, MATCH_CONFIDENCE)
    if not menu_item_pos:
        print("[!] Пункт меню 'Выбрать персонажа' не найден — проверь menu_choose_character.png")
        return
    game_click(menu_item_pos)
    time.sleep(0.8)  # даём экрану выбора персонажа полностью открыться

    # Шаг 2: на экране выбора персонажа ищем иконку Нагито
    icon_pos = find_on_screen(CHAR_ICON_TEMPLATE, MATCH_CONFIDENCE)
    if not icon_pos:
        print("[!] Иконка персонажа не найдена на экране — проверь nagito_icon.png и то, что меню открыто")
        return
    game_click(icon_pos)
    time.sleep(0.4)

    btn_pos = find_on_screen(SELECT_BUTTON_TEMPLATE, MATCH_CONFIDENCE)
    if not btn_pos:
        print("[!] Кнопка 'ВЫБРАТЬ ПЕРСОНАЖА' не найдена")
        return
    game_click(btn_pos)
    time.sleep(0.3)

    print("[*] Персонаж выбран!")


def main():
    if not Path(MODEL_PATH).exists():
        print(f"[!] Не найдена модель Vosk по пути '{MODEL_PATH}'. Скачай и распакуй её.")
        sys.exit(1)

    model = vosk.Model(MODEL_PATH)
    samplerate = 16000
    rec = vosk.KaldiRecognizer(model, samplerate)

    with sd.RawInputStream(samplerate=samplerate, blocksize=4000, dtype='int16',
                            channels=1, callback=audio_callback):
        print("[*] Слушаю микрофон... скажи 'Удача', чтобы выбрать персонажа. Ctrl+C для выхода.")
        while True:
            data = q.get()
            if rec.AcceptWaveform(data):
                # Финальный результат (после паузы в речи)
                result = json.loads(rec.Result())
                text = result.get("text", "").lower()
                if text:
                    print("[распознано]", text)
                    if contains_trigger(text):
                        select_nagito()
            else:
                # Промежуточный результат - приходит НЕ дожидаясь паузы,
                # это и даёт быструю реакцию на слово прямо во время речи.
                partial = json.loads(rec.PartialResult())
                partial_text = partial.get("partial", "").lower()
                if partial_text and contains_trigger(partial_text):
                    select_nagito()
                    rec.Reset()  # сброс, чтобы то же слово не сработало повторно на финальном результате


if __name__ == "__main__":
    main()
