# Публикация v3.0.0 в существующий репозиторий

Текущий `SnapSt.py` относится к старой однофайловой версии и после переноса v3 больше не нужен.

Из корня локального клона:

```powershell
git checkout main
git pull

git rm SnapSt.py
# Скопируйте содержимое архива v3 в корень репозитория с заменой файлов.

git add -A
git commit -m "Release v3.0.0"
git push origin main

git tag v3.0.0
git push origin v3.0.0
```

После отправки тега workflow `.github/workflows/release.yml` создаст или обновит GitHub Release и загрузит `SnapToGMod.exe`.
