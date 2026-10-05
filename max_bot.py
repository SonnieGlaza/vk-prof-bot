"""MAX webhook adapter for the shared career-test bot core.

Run this as a separate Railway service (``python max_bot.py``) and point a
MAX webhook subscription at ``MAX_WEBHOOK_URL``. The existing ``bot.py`` keeps
serving VK. MAX user IDs are mapped to negative core IDs so they cannot collide
with positive VK IDs in the shared progress/results tables.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, urljoin

import requests

import bot as core

log = logging.getLogger("max_bot")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")

MAX_API_BASE = (os.getenv("MAX_API_BASE") or "https://platform-api2.max.ru").rstrip("/")
MAX_BOT_TOKEN = (os.getenv("MAX_BOT_TOKEN") or "").strip()

# MAX API отдаёт цепочку «Russian Trusted Root/Sub CA» (Минцифры), которой нет
# в стандартном бандле certifi — без неё TLS-проверка падает с
# CERTIFICATE_VERIFY_FAILED. Сертификаты встроены прямо сюда, чтобы хватало
# одного файла max_bot.py при деплое на ботхост (без внешних assets).
_RUSSIAN_CA_PEMS = """\
-----BEGIN CERTIFICATE-----
MIIFwjCCA6qgAwIBAgICEAAwDQYJKoZIhvcNAQELBQAwcDELMAkGA1UEBhMCUlUx
PzA9BgNVBAoMNlRoZSBNaW5pc3RyeSBvZiBEaWdpdGFsIERldmVsb3BtZW50IGFu
ZCBDb21tdW5pY2F0aW9uczEgMB4GA1UEAwwXUnVzc2lhbiBUcnVzdGVkIFJvb3Qg
Q0EwHhcNMjIwMzAxMjEwNDE1WhcNMzIwMjI3MjEwNDE1WjBwMQswCQYDVQQGEwJS
VTE/MD0GA1UECgw2VGhlIE1pbmlzdHJ5IG9mIERpZ2l0YWwgRGV2ZWxvcG1lbnQg
YW5kIENvbW11bmljYXRpb25zMSAwHgYDVQQDDBdSdXNzaWFuIFRydXN0ZWQgUm9v
dCBDQTCCAiIwDQYJKoZIhvcNAQEBBQADggIPADCCAgoCggIBAMfFOZ8pUAL3+r2n
qqE0Zp52selXsKGFYoG0GM5bwz1bSFtCt+AZQMhkWQheI3poZAToYJu69pHLKS6Q
XBiwBC1cvzYmUYKMYZC7jE5YhEU2bSL0mX7NaMxMDmH2/NwuOVRj8OImVa5s1F4U
zn4Kv3PFlDBjjSjXKVY9kmjUBsXQrIHeaqmUIsPIlNWUnimXS0I0abExqkbdrXbX
YwCOXhOO2pDUx3ckmJlCMUGacUTnylyQW2VsJIyIGA8V0xzdaeUXg0VZ6ZmNUr5Y
Ber/EAOLPb8NYpsAhJe2mXjMB/J9HNsoFMBFJ0lLOT/+dQvjbdRZoOT8eqJpWnVD
U+QL/qEZnz57N88OWM3rabJkRNdU/Z7x5SFIM9FrqtN8xewsiBWBI0K6XFuOBOTD
4V08o4TzJ8+Ccq5XlCUW2L48pZNCYuBDfBh7FxkB7qDgGDiaftEkZZfApRg2E+M9
G8wkNKTPLDc4wH0FDTijhgxR3Y4PiS1HL2Zhw7bD3CbslmEGgfnnZojNkJtcLeBH
BLa52/dSwNU4WWLubaYSiAmA9IUMX1/RpfpxOxd4Ykmhz97oFbUaDJFipIggx5sX
ePAlkTdWnv+RWBxlJwMQ25oEHmRguNYf4Zr/Rxr9cS93Y+mdXIZaBEE0KS2iLRqa
OiWBki9IMQU4phqPOBAaG7A+eP8PAgMBAAGjZjBkMB0GA1UdDgQWBBTh0YHlzlpf
BKrS6badZrHF+qwshzAfBgNVHSMEGDAWgBTh0YHlzlpfBKrS6badZrHF+qwshzAS
BgNVHRMBAf8ECDAGAQH/AgEEMA4GA1UdDwEB/wQEAwIBhjANBgkqhkiG9w0BAQsF
AAOCAgEAALIY1wkilt/urfEVM5vKzr6utOeDWCUczmWX/RX4ljpRdgF+5fAIS4vH
tmXkqpSCOVeWUrJV9QvZn6L227ZwuE15cWi8DCDal3Ue90WgAJJZMfTshN4OI8cq
W9E4EG9wglbEtMnObHlms8F3CHmrw3k6KmUkWGoa+/ENmcVl68u/cMRl1JbW2bM+
/3A+SAg2c6iPDlehczKx2oa95QW0SkPPWGuNA/CE8CpyANIhu9XFrj3RQ3EqeRcS
AQQod1RNuHpfETLU/A2gMmvn/w/sx7TB3W5BPs6rprOA37tutPq9u6FTZOcG1Oqj
C/B7yTqgI7rbyvox7DEXoX7rIiEqyNNUguTk/u3SZ4VXE2kmxdmSh3TQvybfbnXV
4JbCZVaqiZraqc7oZMnRoWrXRG3ztbnbes/9qhRGI7PqXqeKJBztxRTEVj8ONs1d
WN5szTwaPIvhkhO3CO5ErU2rVdUr89wKpNXbBODFKRtgxUT70YpmJ46VVaqdAhOZ
D9EUUn4YaeLaS8AjSF/h7UkjOibNc4qVDiPP+rkehFWM66PVnP1Msh93tc+taIfC
EYVMxjh8zNbFuoc7fzvvrFILLe7ifvEIUqSVIC/AzplM/Jxw7buXFeGP1qVCBEHq
391d/9RAfaZ12zkwFsl+IKwE/OZxW8AHa9i1p4GO0YSNuczzEm4=
-----END CERTIFICATE-----
-----BEGIN CERTIFICATE-----
MIIHQjCCBSqgAwIBAgICEAIwDQYJKoZIhvcNAQELBQAwcDELMAkGA1UEBhMCUlUx
PzA9BgNVBAoMNlRoZSBNaW5pc3RyeSBvZiBEaWdpdGFsIERldmVsb3BtZW50IGFu
ZCBDb21tdW5pY2F0aW9uczEgMB4GA1UEAwwXUnVzc2lhbiBUcnVzdGVkIFJvb3Qg
Q0EwHhcNMjIwMzAyMTEyNTE5WhcNMjcwMzA2MTEyNTE5WjBvMQswCQYDVQQGEwJS
VTE/MD0GA1UECgw2VGhlIE1pbmlzdHJ5IG9mIERpZ2l0YWwgRGV2ZWxvcG1lbnQg
YW5kIENvbW11bmljYXRpb25zMR8wHQYDVQQDDBZSdXNzaWFuIFRydXN0ZWQgU3Vi
IENBMIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEA9YPqBKOk19NFymrE
wehzrhBEgT2atLezpduB24mQ7CiOa/HVpFCDRZzdxqlh8drku408/tTmWzlNH/br
HuQhZ/miWKOf35lpKzjyBd6TPM23uAfJvEOQ2/dnKGGJbsUo1/udKSvxQwVHpVv3
S80OlluKfhWPDEXQpgyFqIzPoxIQTLZ0deirZwMVHarZ5u8HqHetRuAtmO2ZDGQn
vVOJYAjls+Hiueq7Lj7Oce7CQsTwVZeP+XQx28PAaEZ3y6sQEt6rL06ddpSdoTMp
BnCqTbxW+eWMyjkIn6t9GBtUV45yB1EkHNnj2Ex4GwCiN9T84QQjKSr+8f0psGrZ
vPbCbQAwNFJjisLixnjlGPLKa5vOmNwIh/LAyUW5DjpkCx004LPDuqPpFsKXNKpa
L2Dm6uc0x4Jo5m+gUTVORB6hOSzWnWDj2GWfomLzzyjG81DRGFBpco/O93zecsIN
3SL2Ysjpq1zdoS01CMYxie//9zWvYwzI25/OZigtnpCIrcd2j1Y6dMUFQAzAtHE+
qsXflSL8HIS+IJEFIQobLlYhHkoE3avgNx5jlu+OLYe0dF0Ykx1PGNjbwqvTX37R
Cn32NMjlotW2QcGEZhDKj+3urZizp5xdTPZitA+aEjZM/Ni71VOdiOP0igbw6asZ
2fxdozZ1TnSSYNYvNATwthNmZysCAwEAAaOCAeUwggHhMBIGA1UdEwEB/wQIMAYB
Af8CAQAwDgYDVR0PAQH/BAQDAgGGMB0GA1UdDgQWBBTR4XENCy2BTm6KSo9MI7NM
XqtpCzAfBgNVHSMEGDAWgBTh0YHlzlpfBKrS6badZrHF+qwshzCBxwYIKwYBBQUH
AQEEgbowgbcwOwYIKwYBBQUHMAKGL2h0dHA6Ly9yb3N0ZWxlY29tLnJ1L2NkcC9y
b290Y2Ffc3NsX3JzYTIwMjIuY3J0MDsGCCsGAQUFBzAChi9odHRwOi8vY29tcGFu
eS5ydC5ydS9jZHAvcm9vdGNhX3NzbF9yc2EyMDIyLmNydDA7BggrBgEFBQcwAoYv
aHR0cDovL3JlZXN0ci1wa2kucnUvY2RwL3Jvb3RjYV9zc2xfcnNhMjAyMi5jcnQw
gbAGA1UdHwSBqDCBpTA1oDOgMYYvaHR0cDovL3Jvc3RlbGVjb20ucnUvY2RwL3Jv
b3RjYV9zc2xfcnNhMjAyMi5jcmwwNaAzoDGGL2h0dHA6Ly9jb21wYW55LnJ0LnJ1
L2NkcC9yb290Y2Ffc3NsX3JzYTIwMjIuY3JsMDWgM6Axhi9odHRwOi8vcmVlc3Ry
LXBraS5ydS9jZHAvcm9vdGNhX3NzbF9yc2EyMDIyLmNybDANBgkqhkiG9w0BAQsF
AAOCAgEARBVzZls79AdiSCpar15dA5Hr/rrT4WbrOfzlpI+xrLeRPrUG6eUWIW4v
Sui1yx3iqGLCjPcKb+HOTwoRMbI6ytP/ndp3TlYua2advYBEhSvjs+4vDZNwXr/D
anbwIWdurZmViQRBDFebpkvnIvru/RpWud/5r624Wp8voZMRtj/cm6aI9LtvBfT9
cfzhOaexI/99c14dyiuk1+6QhdwKaCRTc1mdfNQmnfWNRbfWhWBlK3h4GGE9JK33
Gk8ZS8DMrkdAh0xby4xAQ/mSWAfWrBmfzlOqGyoB1U47WTOeqNbWkkoAP2ys94+s
Jg4NTkiDVtXRF6nr6fYi0bSOvOFg0IQrMXO2Y8gyg9ARdPJwKtvWX8VPADCYMiWH
h4n8bZokIrImVKLDQKHY4jCsND2HHdJfnrdL2YJw1qFskNO4cSNmZydw0Wkgjv9k
F+KxqrDKlB8MZu2Hclph6v/CZ0fQ9YuE8/lsHZ0Qc2HyiSMnvjgK5fDc3TD4fa8F
E8gMNurM+kV8PT8LNIM+4Zs+LKEV8nqRWBaxkIVJGekkVKO8xDBOG/aN62AZKHOe
GcyIdu7yNMMRihGVZCYr8rYiJoKiOzDqOkPkLOPdhtVlgnhowzHDxMHND/E2WA5p
ZHuNM/m0TXt2wTTPL7JH2YC0gPz/BvvSzjksgzU5rLbRyUKQkgU=
-----END CERTIFICATE-----
"""

# Внешний файл с российскими корнями — опционально, при наличии перекрывает встроенные.
MAX_RUSSIAN_CA_BUNDLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "full_certs.pem")


def _build_ca_bundle() -> str | bool:
    """Собрать общий CA-бандл для запросов к MAX API.

    Сшиваем стандартные корни (certifi, если он установлен) и сертификаты
    российского ЦС из _RUSSIAN_CA_PEMS (или из внешнего app/full_certs.pem,
    если файл есть). Так и platform-api2.max.ru, и хосты загрузки медиа
    (oneme.ru/okcdn.ru) проходят TLS-проверку. Если задан MAX_CA_BUNDLE
    в окружении — используем его как есть.
    """
    explicit = (os.getenv("MAX_CA_BUNDLE") or "").strip()
    if explicit:
        return explicit

    russian_text = _RUSSIAN_CA_PEMS
    try:
        if os.path.isfile(MAX_RUSSIAN_CA_BUNDLE):
            russian_text = open(MAX_RUSSIAN_CA_BUNDLE, "r", encoding="utf-8").read()
    except Exception:
        log.exception("Не удалось прочитать app/full_certs.pem; использую встроенные сертификаты")

    ca_text = ""
    try:
        import certifi

        ca_text = open(certifi.where(), "r", encoding="utf-8").read()
    except Exception:
        log.warning("certifi недоступен — бандл соберу только из российских корней")

    merged = os.path.join(tempfile.gettempdir(), "max_bot_ca_bundle.pem")
    with open(merged, "w", encoding="utf-8") as out:
        if ca_text:
            out.write(ca_text.rstrip() + "\n\n")
        out.write(russian_text.strip() + "\n")
    return merged


MAX_CA_BUNDLE = _build_ca_bundle()
MAX_WEBHOOK_URL = (os.getenv("MAX_WEBHOOK_URL") or "").strip()
MAX_WEBHOOK_PATH = urlparse(MAX_WEBHOOK_URL).path if MAX_WEBHOOK_URL else "/max/webhook"
MAX_WEBHOOK_SECRET = (os.getenv("MAX_WEBHOOK_SECRET") or "").strip()
MAX_QUEUE_WAKE = threading.Event()
MAX_SEND_TARGET: core.contextvars.ContextVar[dict | None] = core.contextvars.ContextVar(
    "max_send_target", default=None
)
_MAX_RATE_LOCK = threading.Lock()
_MAX_NEXT_SEND_AT: dict[tuple[str, int], float] = {}
_MAX_MESSAGE_INTERVAL = 0.51  # MAX: максимум два сообщения в секунду на диалог.


class MaxApiError(RuntimeError):
    pass


def _core_user_id(max_user_id: int) -> int:
    """Негативный namespace MAX; VK user_id в существующей БД положительные."""
    uid = int(max_user_id)
    if uid <= 0:
        raise ValueError("MAX user_id должен быть положительным числом")
    return -uid


def _validate_upload_url(url: str) -> None:
    host = (urlparse(url).hostname or "").lower()
    allowed = ("oneme.ru", "okcdn.ru")
    if not url.startswith("https://") or not any(host == d or host.endswith("." + d) for d in allowed):
        raise MaxApiError("MAX вернул неожиданный адрес загрузки медиа")


def _keyboard_to_max(raw_keyboard) -> dict | None:
    if not raw_keyboard:
        return None
    if isinstance(raw_keyboard, str):
        try:
            keyboard = json.loads(raw_keyboard)
        except json.JSONDecodeError:
            log.warning("Не удалось разобрать клавиатуру VK-формата")
            return None
    elif isinstance(raw_keyboard, dict):
        keyboard = raw_keyboard
    else:
        return None

    rows = []
    for source_row in keyboard.get("buttons") or []:
        row = []
        for source in source_row or []:
            action = source.get("action") or {}
            label = action.get("label") or action.get("payload")
            if not label:
                continue
            action_type = action.get("type")
            if action_type in (None, "text"):
                row.append({"type": "message", "text": str(label)})
            elif action_type in ("open_link", "link") and action.get("link"):
                row.append({"type": "link", "text": str(label), "url": action["link"]})
        if row:
            rows.append(row)
    if not rows:
        return None
    return {"type": "inline_keyboard", "payload": {"buttons": rows}}


class _MaxUsers:
    def __init__(self, api: "MaxApiCompat"):
        self.api = api

    def get(self, user_ids):
        ids = sorted({int(uid) for uid in (user_ids or []) if int(uid) != 0})
        result = []
        max_ids = [-uid for uid in ids if uid < 0]
        if max_ids:
            placeholders = ",".join("?" for _ in max_ids)
            with core.db_connect() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"SELECT max_user_id, first_name, last_name FROM max_user_map "
                    f"WHERE max_user_id IN ({placeholders})",
                    tuple(max_ids),
                )
                for max_user_id, first_name, last_name in cur.fetchall():
                    result.append({
                        "id": -int(max_user_id),
                        "first_name": first_name or "",
                        "last_name": last_name or "",
                    })

        vk_ids = [uid for uid in ids if uid > 0]
        if vk_ids and self.api.vk_api is not None:
            for offset in range(0, len(vk_ids), 900):
                try:
                    result.extend(self.api.vk_api.users.get(user_ids=vk_ids[offset : offset + 900]) or [])
                except Exception:
                    log.exception("Не удалось получить имена VK для статистики")
        return result


class _MaxMessages:
    def __init__(self, api: "MaxApiCompat"):
        self.api = api

    @staticmethod
    def _rate_limit(target: tuple[str, int]) -> None:
        with _MAX_RATE_LOCK:
            now = time.monotonic()
            slot = max(now, _MAX_NEXT_SEND_AT.get(target, now))
            _MAX_NEXT_SEND_AT[target] = slot + _MAX_MESSAGE_INTERVAL
        delay = slot - now
        if delay > 0:
            time.sleep(delay)

    def send(self, *, peer_id=None, random_id=None, message="", keyboard=None, attachment=None, **_ignored):
        target = MAX_SEND_TARGET.get()
        if target and target.get("chat_id") is not None:
            query = {"chat_id": int(target["chat_id"])}
            rate_key = ("chat", int(target["chat_id"]))
        elif target and target.get("user_id") is not None:
            query = {"user_id": int(target["user_id"])}
            rate_key = ("user", int(target["user_id"]))
        else:
            raw_id = abs(int(peer_id or 0))
            query = {"user_id": raw_id}
            rate_key = ("user", raw_id)
        self._rate_limit(rate_key)

        attachments = []
        max_keyboard = _keyboard_to_max(keyboard)
        if max_keyboard:
            attachments.append(max_keyboard)
        if isinstance(attachment, dict):
            attachments.append(attachment)
        elif isinstance(attachment, list):
            attachments.extend(a for a in attachment if isinstance(a, dict))
        elif attachment:
            raise MaxApiError("Передано VK-вложение; MAX-адаптер ожидает MAX attachment token")

        body = {"text": str(message or "")}
        if attachments:
            body["attachments"] = attachments

        for attempt in range(4):
            response = self.api._request("POST", "messages", params=query, json=body, raise_errors=False)
            if response.status_code == 429:
                delay = min(5.0, 0.5 * (2 ** attempt))
                log.warning("MAX ограничил частоту отправки; повтор через %.1f с", delay)
                time.sleep(delay)
                continue
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            if response.ok:
                return payload
            if payload.get("code") == "attachment.not.ready" and attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise MaxApiError(f"MAX messages.send HTTP {response.status_code}: {payload.get('code', 'request failed')}")
        raise MaxApiError("MAX не принял сообщение после повторных попыток")


class MaxApiCompat:
    """Минимальный VK-подобный фасад, чтобы вызывать общие обработчики без копии логики."""
    platform = "max"

    def __init__(self, token: str, base_url: str = MAX_API_BASE):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.messages = _MaxMessages(self)
        self.users = _MaxUsers(self)
        self._vk_session = None
        self.vk_api = None
        if core.VK_TOKEN:
            try:
                self._vk_session = core.vk_api.VkApi(token=core.VK_TOKEN)
                self.vk_api = self._vk_session.get_api()
            except Exception:
                log.exception("Не удалось инициализировать VK API для имён в сводной выгрузке")

    def _request(self, method, endpoint, *, params=None, json=None, files=None, headers=None, timeout=(10, 30), raise_errors=True, **extra):
        """Вызов MAX API: urljoin-склейка, авторизация и наш CA-бандл (см. MAX_CA_BUNDLE)."""
        request_headers = {"Authorization": self.token, "Accept": "application/json"}
        if headers:
            request_headers.update(headers)
        url = urljoin(self.base_url.rstrip("/"), endpoint.lstrip("/"))
        response = requests.request(
            method,
            url,
            params=params,
            json=json,
            files=files,
            headers=request_headers,
            timeout=timeout,
            verify=MAX_CA_BUNDLE,
            **extra,
        )
        if raise_errors:
            response.raise_for_status()
        return response

    def _upload(self, source, filename: str, media_type: str) -> dict:
        init = self._request("POST", "uploads", params={"type": media_type}).json()
        url = init.get("url")
        if not url:
            raise MaxApiError("MAX не вернул URL для загрузки")
        _validate_upload_url(url)
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        source.seek(0)
        upload_headers = {"Authorization": self.token} if media_type == "image" else {}
        response = requests.post(
            url,
            files={"data": (filename, source, mime)},
            headers=upload_headers,
            timeout=(10, 180),
            verify=MAX_CA_BUNDLE,
        )
        response.raise_for_status()
        payload = response.json()
        if media_type == "image":
            token = (payload.get("photos") or {}).get("photoIds", {}).get("token")
        else:
            token = payload.get("token")
        if not token:
            raise MaxApiError("MAX загрузил файл, но не вернул media token")
        return {"type": media_type, "payload": {"token": token}}

    def upload_document(self, bio, filename: str, _peer_id: int):
        return self._upload(bio, filename, "file")

    def upload_image(self, image_path: str, _user_id: int):
        with open(image_path, "rb") as source:
            return self._upload(source, os.path.basename(image_path), "image")


def _ensure_max_tables() -> None:
    with core.db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """CREATE TABLE IF NOT EXISTS max_user_map (
                max_user_id BIGINT PRIMARY KEY,
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                username TEXT NOT NULL DEFAULT '',
                updated_at BIGINT NOT NULL
            )"""
        )
        cur.execute(
            """CREATE TABLE IF NOT EXISTS max_webhook_queue (
                update_key TEXT PRIMARY KEY,
                received_at BIGINT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            )"""
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_max_webhook_queue ON max_webhook_queue(status, received_at)")
        cur.execute("UPDATE max_webhook_queue SET status='pending' WHERE status='processing'")
        cutoff_ms = int(time.time() * 1000) - 30 * 86400 * 1000
        cur.execute("DELETE FROM max_webhook_queue WHERE status IN ('done','failed') AND received_at < ?", (cutoff_ms,))
        conn.commit()


def _remember_max_user(user: dict) -> int:
    raw_id = int(user.get("user_id") or user.get("id") or 0)
    internal_id = _core_user_id(raw_id)
    first = str(user.get("first_name") or "")
    last = str(user.get("last_name") or "")
    username = str(user.get("username") or "")
    with core.db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO max_user_map(max_user_id, first_name, last_name, username, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(max_user_id) DO UPDATE SET
                   first_name=excluded.first_name,
                   last_name=excluded.last_name,
                   username=excluded.username,
                   updated_at=excluded.updated_at""",
            (raw_id, first, last, username, int(time.time())),
        )
        conn.commit()
    return internal_id


def _stable_update_key(update: dict) -> str:
    event_type = str(update.get("update_type") or "unknown")
    message = update.get("message") or {}
    body = message.get("body") or {}
    sender = message.get("sender") or update.get("user") or {}
    user_id = sender.get("user_id") or sender.get("id") or 0
    chat_id = update.get("chat_id") or (message.get("recipient") or {}).get("chat_id") or user_id
    message_id = body.get("mid") or message.get("message_id") or message.get("mid")
    stamp = update.get("timestamp") or message.get("timestamp") or 0
    identity = str(message_id) if message_id is not None else f"{stamp}:{user_id}:{body.get('text', '')}"
    raw = f"{event_type}:{chat_id}:{user_id}:{identity}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _enqueue_update(update: dict) -> None:
    key = _stable_update_key(update)
    with core.db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO max_webhook_queue(update_key, received_at, payload_json, status)
               VALUES (?, ?, ?, 'pending') ON CONFLICT(update_key) DO NOTHING""",
            (key, int(time.time() * 1000), json.dumps(update, ensure_ascii=False)),
        )
        inserted = cur.rowcount > 0
        conn.commit()
    if inserted:
        MAX_QUEUE_WAKE.set()


def _claim_next_update():
    with core.db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT update_key, payload_json FROM max_webhook_queue "
            "WHERE status='pending' ORDER BY received_at, update_key LIMIT 1"
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return None
        cur.execute(
            "UPDATE max_webhook_queue SET status='processing', attempts=attempts+1 "
            "WHERE update_key=? AND status='pending'",
            (row[0],),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
        return row[0], json.loads(row[1])


def _finish_update(update_key: str, status: str, error: str | None = None) -> None:
    with core.db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "UPDATE max_webhook_queue SET status=?, payload_json='', last_error=? WHERE update_key=?",
            (status, error[:1000] if error else None, update_key),
        )
        conn.commit()


def _dedup_max_message(chat_id: int, message_id: int) -> bool:
    # Негативный peer namespace не пересекается с positive VK peer_id в общей dedup-таблице.
    with core.db_connect() as conn:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO longpoll_incoming_dedup(dedup_key, seen_at) VALUES (?, ?)",
            (f"max:{-abs(int(chat_id))}:{int(message_id)}", int(time.time())),
        )
        inserted = cur.rowcount > 0
        if inserted:
            conn.commit()
        else:
            conn.rollback()
        return inserted


def _dispatch_max_update(api: MaxApiCompat, update: dict) -> None:
    update_type = update.get("update_type")
    if update_type not in ("bot_started", "message_created"):
        return
    message = update.get("message") or {}
    sender = message.get("sender") or update.get("user") or {}
    if sender.get("is_bot"):
        return
    raw_user_id = int(sender.get("user_id") or sender.get("id") or 0)
    if raw_user_id <= 0:
        return
    internal_user_id = _remember_max_user(sender)
    recipient = message.get("recipient") or {}
    target_chat = update.get("chat_id") or recipient.get("chat_id")
    if target_chat is not None:
        target = {"chat_id": int(target_chat)}
        reply_peer_id = int(target_chat)
        dedup_peer_id = int(target_chat)
    else:
        target = {"user_id": raw_user_id}
        reply_peer_id = raw_user_id
        dedup_peer_id = raw_user_id

    body = message.get("body") or {}
    text = str(body.get("text") or "")
    message_id = body.get("mid") or message.get("message_id") or message.get("mid")
    if message_id is None:
        message_id = update.get("timestamp") or int(time.time() * 1000)
    message_hash = int.from_bytes(hashlib.sha256(str(message_id).encode()).digest()[:8], "big") & ((1 << 63) - 1)
    if not _dedup_max_message(dedup_peer_id, message_hash):
        return

    peer_token = core._REPLY_PEER_ID.set(reply_peer_id)
    target_token = MAX_SEND_TARGET.set(target)
    try:
        if update_type == "bot_started":
            core.send_welcome(api, internal_user_id)
            return
        if core.dispatch_command(api, internal_user_id, text):
            return
        if core.handle_reminder_continue_choice(api, internal_user_id, text):
            return
        if text.strip().isdigit():
            core.handle_answer(api, internal_user_id, text.strip())
        else:
            core.send_message(
                api,
                internal_user_id,
                "Не понял команду. Напишите «меню» или выберите тест кнопкой.",
                keyboard=core.build_menu_keyboard(),
            )
    finally:
        MAX_SEND_TARGET.reset(target_token)
        core._REPLY_PEER_ID.reset(peer_token)


def _max_update_worker(api: MaxApiCompat) -> None:
    while True:
        row = _claim_next_update()
        if row is None:
            MAX_QUEUE_WAKE.wait(timeout=1.0)
            MAX_QUEUE_WAKE.clear()
            continue
        update_key, update = row
        try:
            _dispatch_max_update(api, update)
            _finish_update(update_key, "done")
        except Exception as exc:
            log.exception("MAX update failed; key=%s", update_key)
            try:
                _finish_update(update_key, "failed", type(exc).__name__)
            except Exception:
                log.exception("Could not mark MAX update as failed")


def _max_reminder_worker(api: MaxApiCompat) -> None:
    while True:
        try:
            for internal_id in core.users_for_reminder():
                if internal_id >= 0:
                    continue
                progress = core.get_progress(internal_id)
                if not progress or progress["status"] != "in_progress":
                    continue
                test_id = progress["test_id"]
                total = core._opg_effective_question_count(test_id)
                step_display = progress["step"] + 1
                raw_user_id = abs(int(internal_id))
                target_token = MAX_SEND_TARGET.set({"user_id": raw_user_id})
                peer_token = core._REPLY_PEER_ID.set(raw_user_id)
                try:
                    core.send_message(
                        api,
                        internal_id,
                        f"⏰ Напоминание: Вы на вопросе {step_display} из {total}.\n"
                        "Продолжим? «Да» — вернёмся к опросу, «Нет» — выход в меню.",
                        keyboard=core.build_reminder_continue_keyboard(),
                    )
                    core.set_reminder_pending(internal_id, 1)
                    core.set_reminded(internal_id)
                finally:
                    core._REPLY_PEER_ID.reset(peer_token)
                    MAX_SEND_TARGET.reset(target_token)
        except Exception:
            log.exception("MAX reminder worker failed")
        time.sleep(core.REMINDER_CHECK_EVERY_SEC)


def _write_json(handler: BaseHTTPRequestHandler, code: int, body: dict) -> None:
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class MaxWebhookHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log.info("MAX webhook %s", fmt % args)

    def do_GET(self):
        if urlparse(self.path).path == "/health":
            _write_json(self, 200, {"ok": True})
        else:
            _write_json(self, 404, {"error": "not found"})

    def do_POST(self):
        if urlparse(self.path).path != MAX_WEBHOOK_PATH:
            _write_json(self, 404, {"error": "not found"})
            return
        received_secret = self.headers.get("X-Max-Bot-Api-Secret", "")
        if not hmac.compare_digest(received_secret, MAX_WEBHOOK_SECRET):
            _write_json(self, 401, {"error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2_000_000:
                _write_json(self, 413, {"error": "invalid request size"})
                return
            update = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(update, dict):
                raise ValueError("update must be an object")
            update_type = update.get("update_type")
            if update_type in ("message_created", "bot_started"):
                _enqueue_update(update)
            _write_json(self, 200, {"ok": True})
        except Exception:
            log.exception("Invalid or unpersistable MAX webhook request")
            _write_json(self, 503, {"error": "temporarily unavailable"})


def _register_webhook(api: MaxApiCompat) -> None:
    log.info(f"[DEBUG] MAX_WEBHOOK_URL raw = '{MAX_WEBHOOK_URL}'")
    parsed = urlparse(MAX_WEBHOOK_URL)
    log.info(f"[DEBUG] parsed.scheme = '{parsed.scheme}', parsed.port = {parsed.port}, parsed.hostname = '{parsed.hostname}'")

    if parsed.scheme != "https" or parsed.port not in (None, 443) or not parsed.hostname:
        raise RuntimeError("MAX_WEBHOOK_URL должен быть публичным HTTPS-адресом на порту 443")
        
    if not re.fullmatch(r"[A-Za-z0-9_-]{5,256}", MAX_WEBHOOK_SECRET):
        raise RuntimeError("MAX_WEBHOOK_SECRET: используйте 5–256 символов A-Z, a-z, 0-9, _ или -")
    existing = api._request("GET", "subscriptions").json().get("subscriptions", [])
    for subscription in existing:
        if subscription.get("url") == MAX_WEBHOOK_URL:
            # Recreate to apply a changed secret or event list after redeploy.
            api._request("DELETE", "subscriptions", params={"url": MAX_WEBHOOK_URL})
            break
    result = api._request(
        "POST",
        "subscriptions",
        json={
            "url": MAX_WEBHOOK_URL,
            "update_types": ["message_created", "bot_started"],
            "secret": MAX_WEBHOOK_SECRET,
        },
    ).json()
    if result.get("success") is False:
        raise MaxApiError(f"MAX webhook subscription failed: {result.get('message', 'unknown error')}")
    log.info("MAX webhook subscription registered")


def main() -> None:
    if not MAX_BOT_TOKEN:
        raise SystemExit("Set MAX_BOT_TOKEN in the MAX Railway service variables")
    if not MAX_WEBHOOK_URL or not MAX_WEBHOOK_SECRET:
        raise SystemExit("Set MAX_WEBHOOK_URL and MAX_WEBHOOK_SECRET before starting the MAX service")
    if not core.USE_PG:
        log.warning("SQLite is local to this service; for VK/MAX shared state set the same DATABASE_URL PostgreSQL on both services")

    core.init_db()
    _ensure_max_tables()
    api = MaxApiCompat(MAX_BOT_TOKEN)
    server = ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8000"))), MaxWebhookHandler)
    threading.Thread(target=server.serve_forever, name="max-webhook-http", daemon=True).start()
    threading.Thread(target=_max_update_worker, args=(api,), name="max-update-worker", daemon=True).start()
    threading.Thread(target=_max_reminder_worker, args=(api,), name="max-reminder-worker", daemon=True).start()
    _register_webhook(api)
    log.info("MAX webhook service listening on port %s; DB=%s", os.getenv("PORT", "8000"), core.backend_label())
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
