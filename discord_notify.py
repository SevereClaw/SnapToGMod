"""
discord_notify.py
------------------
Уведомление в Discord через вебхук при срабатывании. Адрес вебхука читается
из переменной окружения SNAPTOGMOD_DISCORD_WEBHOOK_URL и не хранится в коде.
Пользователь меняет только отображаемое имя и включает/выключает уведомления.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

from config import AppConfig, DISCORD_WEBHOOK_URL, Server


DEFAULT_DISPLAY_NAME = "Аноним"


def get_default_display_name() -> str:
    """Имя по умолчанию для сообщений в Discord, пока пользователь не задал
    своё через меню. Раньше здесь подставлялся логин Windows-пользователя
    (getpass.getuser()) — это утечка реального имени пользователя в канал
    Discord без его ведома, и вдобавок значение могло отличаться от
    ожидаемого в зависимости от того, из-под какой учётной записи запущена
    программа (например, после перезапуска с правами администратора).
    Теперь по умолчанию у всех одинаково: «Аноним»."""
    return DEFAULT_DISPLAY_NAME


def send_discord_notification(cfg: AppConfig, server: Server, logger) -> None:
    if not cfg.discord_notify_enabled:
        return
    if not DISCORD_WEBHOOK_URL:
        logger.warning(
            "Discord-уведомления включены, но переменная "
            "SNAPTOGMOD_DISCORD_WEBHOOK_URL не задана."
        )
        return

    def _send():
        try:
            name = cfg.discord_display_name or get_default_display_name()
            embed = {
                "description": f"**{name}** запустил(а) GMod",
                "color": 0x5865F2,
                "fields": [
                    {"name": "Сервер", "value": server.name, "inline": True},
                    {"name": "IP", "value": server.ip, "inline": True},
                ],
                "footer": {"text": "Snap-to-GMod"},
            }
            payload = json.dumps({"embeds": [embed]}).encode("utf-8")
            req = urllib.request.Request(
                DISCORD_WEBHOOK_URL, data=payload,
                headers={
                    "Content-Type": "application/json",
                    # Без User-Agent Discord часто отдаёт 403 через Cloudflare.
                    "User-Agent": "SnapToGMod-DiscordWebhook/1.0",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=6):
                pass
            logger.info("Уведомление отправлено в Discord.")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            logger.warning("Не удалось отправить уведомление в Discord: HTTP %s %s — %s", e.code, e.reason, body)
        except Exception as e:
            logger.warning("Не удалось отправить уведомление в Discord: %s", e)

    threading.Thread(target=_send, daemon=True).start()
