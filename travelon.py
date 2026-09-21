"""
Читання заявки з адмінки travelon.to.

Токен НЕ зашивається в код - береться з .env (TRAVELON_TOKEN).
Формат відповіді (JSON чи XML) поки невідомий, тому тут гнучкий парсер:
вказуєте у .env, як називаються поля з телефоном/ім'ям агента, - решта підлаштується.
Коли побачите реальний XML - підправимо get_booking() під точну структуру за 5 хвилин.
"""
import os
import re
import xml.etree.ElementTree as ET

import httpx

TRAVELON_BOOKING_URL = os.getenv("TRAVELON_BOOKING_URL", "")   # напр. https://travelon.to/api/booking/{id}.xml
TRAVELON_TOKEN = os.getenv("TRAVELON_TOKEN", "")
TRAVELON_TOKEN_HEADER = os.getenv("TRAVELON_TOKEN_HEADER", "Authorization")  # або X-Api-Key
TRAVELON_TOKEN_PREFIX = os.getenv("TRAVELON_TOKEN_PREFIX", "Bearer ")           # "" якщо без Bearer
PHONE_FIELDS = os.getenv("TRAVELON_PHONE_FIELDS", "agent_phone,phone,tel,manager_phone").split(",")
NAME_FIELDS = os.getenv("TRAVELON_NAME_FIELDS", "agent_name,agent,manager,company").split(",")
COMMENT_FIELDS = os.getenv("TRAVELON_COMMENT_FIELDS", "comment,last_comment,note").split(",")


class BookingNotFound(Exception):
    pass


def _flatten_xml(el, out, prefix=""):
    tag = re.sub(r"\{.*?\}", "", el.tag).lower()
    key = f"{prefix}{tag}"
    if el.text and el.text.strip():
        out.setdefault(tag, el.text.strip())
        out.setdefault(key, el.text.strip())
    for k, v in el.attrib.items():
        out.setdefault(k.lower(), v)
    for child in el:
        _flatten_xml(child, out, key + ".")


def _flatten_json(obj, out, prefix=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten_json(v, out, f"{prefix}{k.lower()}.")
            if not isinstance(v, (dict, list)) and v is not None:
                out.setdefault(k.lower(), str(v))
    elif isinstance(obj, list):
        for i in obj:
            _flatten_json(i, out, prefix)


def _pick(flat: dict, fields: list[str]) -> str | None:
    for f in fields:
        f = f.strip().lower()
        if f in flat and flat[f]:
            return flat[f]
    return None


async def get_booking(booking_id: str) -> dict:
    """Повертає {"id", "agent_phone", "agent_name", "comment", "raw"}."""
    if not TRAVELON_BOOKING_URL:
        raise RuntimeError("TRAVELON_BOOKING_URL не задано в .env")
    url = TRAVELON_BOOKING_URL.format(id=booking_id)
    headers = {TRAVELON_TOKEN_HEADER: f"{TRAVELON_TOKEN_PREFIX}{TRAVELON_TOKEN}"} if TRAVELON_TOKEN else {}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(url, headers=headers)
    if r.status_code == 404:
        raise BookingNotFound(booking_id)
    r.raise_for_status()

    flat: dict = {}
    body = r.text.strip()
    if body.startswith("<"):
        _flatten_xml(ET.fromstring(body), flat)
    else:
        _flatten_json(r.json(), flat)

    phone = _pick(flat, PHONE_FIELDS)
    if not phone:
        raise BookingNotFound(f"{booking_id}: телефон агента не знайдено, поля: {', '.join(list(flat)[:30])}")
    return {
        "id": booking_id,
        "agent_phone": phone,
        "agent_name": _pick(flat, NAME_FIELDS) or "",
        "comment": _pick(flat, COMMENT_FIELDS) or "",
        "raw": flat,
    }
