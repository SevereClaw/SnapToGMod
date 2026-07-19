# SnapToGMod

**Инструмент для Garry's Mod** — переносит/конвертирует снапшоты, модели или текстуры.

---

## Скачать готовую версию (Windows)

**[[⬇️ Скачать SnapToGMod.py]](https://github.com/SevereClaw/SnapToGMod/releases/download/Release/snap_to_gmod.py)**

---

## Установка из исходников

### Требования
- Python 3.9+
- Windows (рекомендуется)

### Шаги

```bash
git clone https://github.com/SevereClaw/SnapToGMod.git
cd SnapToGMod

python -m venv venv
venv\Scripts\activate

pip install -r requirements.txt
python SnapSt.py

Сборка .exe
Bashpip install pyinstaller
pyinstaller --onefile --windowed --name SnapToGMod SnapSt.py

Автор: SevereClaw

