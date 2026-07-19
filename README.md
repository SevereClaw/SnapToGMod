# SnapToGMod

**Инструмент для Garry's Mod** — запускает игру по щелчку пальцами / хлопку через микрофон + резервная горячая клавиша (Ctrl+Alt+G).

---

## Скачать готовую версию (Windows)

**[⬇️ Скачать snap_to_gmod.py](https://github.com/SevereClaw/SnapToGMod/releases/download/Release/snap_to_gmod.py)**

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

```

### Сборка в .exe

```
pip install pyinstaller
pyinstaller --onefile --noconsole --name SnapToGMod SnapSt.py
```
Готовый файл будет в папке dist

### Автор: SevereClaw
