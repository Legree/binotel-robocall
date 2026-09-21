"""
Робо-дзвінки через Binotel (SIP-внутрішній номер + Asterisk).

Потік:
  адмінка -> POST /call -> черга (sqlite) -> TTS (edge-tts) -> wav 8kHz
  -> AMI Originate PJSIP/<номер>@binotel -> агент зняв трубку -> dialplan [robocall]
  -> UserEvent RobocallResult -> результат у sqlite + callback в адмінку.
"""
import asyncio
import logging
import os
import re
import sqlite3
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

import edge_tts
import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

import telegram_bot
from travelon import BookingNotFound, get_booking

log = logging.getLogger("robocall")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ---------- конфіг ----------
WEBHOOK_TOKEN = os.environ["WEBHOOK_TOKEN"]
AMI_HOST = os.getenv("AMI_HOST", "127.0.0.1")
AMI_PORT = int(os.getenv("AMI_PORT", "5038"))
AMI_USER = os.environ["AMI_USER"]
AMI_SECRET = os.environ["AMI_SECRET"]
ADMIN_CALLBACK_URL = os.getenv("ADMIN_CALLBACK_URL", "")
ADMIN_CALLBACK_TOKEN = os.getenv("ADMIN_CALLBACK_TOKEN", "")
TTS_VOICE = os.getenv("TTS_VOICE", "uk-UA-PolinaNeural")
SOUNDS_DIR = Path(os.getenv("SOUNDS_DIR", "/var/lib/asterisk/sounds/robocall"))
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS", "3"))
RETRY_DELAY_SEC = int(os.getenv("RETRY_DELAY_SEC", "600"))
WORK_HOURS = os.getenv("WORK_HOURS", "08:00-20:00")
DB_PATH = os.getenv("DB_PATH", "robocall.sqlite")
CALL_TIMEOUT_SEC = 45          # скільки чекаємо, поки агент зніме трубку
RESULT_TIMEOUT_SEC = 180       # максимум тривалості одного дзвінка

TEMPLATE = (
    "Просимо звернути увагу на коментар до заявки {digits}. "
    "{urgent}"
    "Повідомлення прочитано, дякуємо."
)
GATE_TEXT = ("Доброго дня! Це компанія Тревелон. "
             "Натисніть один, щоб прослухати важливе повідомлення по заявці.")
THANKS_TEXT = "Дякуємо. До побачення."

# Лише confirmed означає, що жива людина натиснула 1 і прослухала. Все інше - повтор.
FINAL_RESULTS = {"confirmed"}

# ---- TurboSMS: запасний канал після MAX_ATTEMPTS невдалих дзвінків ----
TURBOSMS_TOKEN = os.getenv("TURBOSMS_TOKEN", "")
TURBOSMS_SENDER = os.getenv("TURBOSMS_SENDER", "Travelon")
SMS_TEMPLATE = os.getenv(
    "SMS_TEMPLATE",
    "Зверніть увагу на повідомлення до заявки {booking_id}. "
    "Туроператор Тревелон, 0443334433, 0663330433")


# ---------- БД ----------
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY,
            number TEXT, booking_id TEXT, text TEXT,
            status TEXT, result TEXT, attempts INTEGER DEFAULT 0,
            next_attempt REAL, created REAL, updated REAL,
            tg_chat_id INTEGER)""")


# ---------- допоміжне ----------
def normalize_number(raw: str) -> str:
    d = re.sub(r"\D", "", raw)
    if len(d) == 10 and d.startswith("0"):
        d = "38" + d
    if len(d) == 12 and d.startswith("380"):
        return d
    raise ValueError(f"bad phone number: {raw}")


def in_work_hours() -> bool:
    start, end = WORK_HOURS.split("-")
    now = datetime.now().strftime("%H:%M")
    return start <= now <= end


def digits_spoken(s: str) -> str:
    return " ".join(ch for ch in s)


async def make_wav(job_id: str, text: str) -> Path:
    SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    mp3 = SOUNDS_DIR / f"{job_id}.mp3"
    wav = SOUNDS_DIR / f"{job_id}.wav"
    await edge_tts.Communicate(text, TTS_VOICE, rate="-5%").save(str(mp3))
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3),
         "-ar", "8000", "-ac", "1", "-acodec", "pcm_s16le", str(wav)],
        check=True,
    )
    mp3.unlink(missing_ok=True)
    return wav


async def ensure_static_files():
    for name, text in (("gate", GATE_TEXT), ("thanks", THANKS_TEXT)):
        if not (SOUNDS_DIR / f"{name}.wav").exists():
            await make_wav(name, text)


async def send_sms(number: str, booking_id: str) -> bool:
    """TurboSMS HTTP API: POST /message/send.json, Bearer-токен."""
    if not TURBOSMS_TOKEN:
        log.warning("TURBOSMS_TOKEN не задано - SMS пропущено")
        return False
    payload = {"recipients": [number],
               "sms": {"sender": TURBOSMS_SENDER, "text": SMS_TEMPLATE.format(booking_id=booking_id)}}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post("https://api.turbosms.ua/message/send.json", json=payload,
                             headers={"Authorization": f"Bearer {TURBOSMS_TOKEN}"})
        data = r.json()
        ok = r.status_code == 200 and data.get("response_code") == 0
        log.info("sms to %s: %s", number, data.get("response_status"))
        return ok
    except Exception as e:
        log.warning("sms failed: %s", e)
        return False


# ---------- AMI ----------
class AMI:
    """Мінімальний асинхронний клієнт Asterisk Manager Interface."""

    def __init__(self):
        self.reader = self.writer = None
        self.waiters: dict[str, asyncio.Future] = {}   # job_id -> future(result)
        self.lock = asyncio.Lock()

    async def connect(self):
        while True:
            try:
                self.reader, self.writer = await asyncio.open_connection(AMI_HOST, AMI_PORT)
                await self.reader.readline()  # banner
                await self._send({"Action": "Login", "Username": AMI_USER,
                                  "Secret": AMI_SECRET, "Events": "on"})
                asyncio.create_task(self._reader_loop())
                log.info("AMI connected")
                return
            except OSError as e:
                log.warning("AMI connect failed: %s, retry in 5s", e)
                await asyncio.sleep(5)

    async def _send(self, msg: dict):
        data = "".join(f"{k}: {v}\r\n" for k, v in msg.items()) + "\r\n"
        async with self.lock:
            self.writer.write(data.encode())
            await self.writer.drain()

    async def _reader_loop(self):
        packet: dict = {}
        try:
            while True:
                line = await self.reader.readline()
                if not line:
                    raise ConnectionError("AMI closed")
                line = line.decode(errors="replace").rstrip("\r\n")
                if line == "":
                    if packet:
                        self._dispatch(packet)
                    packet = {}
                elif ":" in line:
                    k, v = line.split(":", 1)
                    packet[k.strip()] = v.strip()
        except Exception as e:
            log.error("AMI reader died: %s - reconnecting", e)
            for fut in self.waiters.values():
                if not fut.done():
                    fut.set_result("failed")
            await self.connect()

    def _dispatch(self, p: dict):
        ev = p.get("Event")
        if ev == "UserEvent" and p.get("UserEvent") == "RobocallResult":
            self._finish(p.get("JobId"), p.get("Result", "hangup"))
        elif ev == "OriginateResponse" and p.get("Response") == "Failure":
            reason = p.get("Reason", "")
            # 0/1/5 = не змогли/no answer, 3 = ringing timeout, 5 = busy
            self._finish(p.get("ActionID"), "busy" if reason == "5" else "noanswer")

    def _finish(self, job_id, result):
        fut = self.waiters.get(job_id)
        if fut and not fut.done():
            fut.set_result(result)

    async def call(self, job_id: str, number: str) -> str:
        fut = asyncio.get_event_loop().create_future()
        self.waiters[job_id] = fut
        try:
            await self._send({
                "Action": "Originate",
                "ActionID": job_id,
                "Channel": f"PJSIP/{number}@binotel",
                "Context": "robocall",
                "Exten": "s",
                "Priority": "1",
                "Timeout": str(CALL_TIMEOUT_SEC * 1000),
                "Variable": f"JOB_ID={job_id}",
                "CallerID": f"Travelon <{number}>",
                "Async": "true",
            })
            return await asyncio.wait_for(fut, CALL_TIMEOUT_SEC + RESULT_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            return "failed"
        finally:
            self.waiters.pop(job_id, None)


ami = AMI()


# ---------- воркер черги ----------
def create_job(number_raw: str, booking_id: str, urgent: bool = True,
               text: str | None = None, tg_chat_id: int | None = None) -> str:
    number = normalize_number(number_raw)
    text = text or TEMPLATE.format(digits=digits_spoken(booking_id),
                                   urgent="Терміново! " if urgent else "")
    job_id = uuid.uuid4().hex[:12]
    now = time.time()
    with db() as con:
        con.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (job_id, number, booking_id, text, "pending", None, 0, now, now, now, tg_chat_id))
    return job_id


async def notify_admin(job: sqlite3.Row):
    await telegram_bot.notify_result(job["tg_chat_id"], job["booking_id"],
                                     job["result"], job["status"], job["attempts"])
    if not ADMIN_CALLBACK_URL:
        return
    payload = {k: job[k] for k in ("id", "booking_id", "number", "status", "result", "attempts")}
    headers = {"X-Token": ADMIN_CALLBACK_TOKEN} if ADMIN_CALLBACK_TOKEN else {}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.post(ADMIN_CALLBACK_URL, json=payload, headers=headers)
    except Exception as e:
        log.warning("callback failed: %s", e)


async def process(job: sqlite3.Row):
    wav = SOUNDS_DIR / f"{job['id']}.wav"
    if not wav.exists():
        await make_wav(job["id"], job["text"])
    log.info("calling %s for booking %s (attempt %d)", job["number"], job["booking_id"], job["attempts"] + 1)
    result = await ami.call(job["id"], job["number"])
    attempts = job["attempts"] + 1
    if result in FINAL_RESULTS:
        status, nxt = "done", None
    elif attempts >= MAX_ATTEMPTS:
        # не додзвонились - запасний канал
        result = "sms_sent" if await send_sms(job["number"], job["booking_id"]) else "sms_failed"
        status, nxt = "done", None
    else:
        status, nxt = "pending", time.time() + RETRY_DELAY_SEC
    if status == "done":
        wav.unlink(missing_ok=True)
    with db() as con:
        con.execute("UPDATE jobs SET status=?, result=?, attempts=?, next_attempt=?, updated=? WHERE id=?",
                    (status, result, attempts, nxt, time.time(), job["id"]))
        job = con.execute("SELECT * FROM jobs WHERE id=?", (job["id"],)).fetchone()
    log.info("job %s -> %s (%s)", job["id"], result, status)
    await notify_admin(job)


async def worker():
    await ami.connect()
    await ensure_static_files()
    while True:
        try:
            if in_work_hours():
                with db() as con:
                    job = con.execute(
                        "SELECT * FROM jobs WHERE status='pending' AND next_attempt<=? "
                        "ORDER BY next_attempt LIMIT 1", (time.time(),)).fetchone()
                if job:
                    with db() as con:
                        con.execute("UPDATE jobs SET status='calling' WHERE id=?", (job["id"],))
                    await process(job)
                    continue
        except Exception as e:
            log.exception("worker error: %s", e)
        await asyncio.sleep(2)


# ---------- HTTP ----------
app = FastAPI(title="Binotel robocall")


class CallRequest(BaseModel):
    number: str            # телефон агента: 0671234567 або +380671234567
    booking_id: str        # номер заявки, напр. "54443"
    urgent: bool = True
    text: str | None = None   # якщо задано - озвучується замість шаблону


@app.on_event("startup")
async def startup():
    init_db()
    asyncio.create_task(worker())
    asyncio.create_task(telegram_bot.run_polling())


@app.post("/call")
async def create_call(req: CallRequest, x_token: str = Header(default="")):
    """Прямий виклик: адмінка вже знає телефон агента."""
    if x_token != WEBHOOK_TOKEN:
        raise HTTPException(401)
    try:
        job_id = create_job(req.number, req.booking_id, req.urgent, req.text)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"job_id": job_id, "status": "queued"}


@app.post("/call/booking/{booking_id}")
async def call_by_booking(booking_id: str, urgent: bool = True, x_token: str = Header(default="")):
    """Бот сам заходить у заявку, бере телефон агента і дзвонить."""
    if x_token != WEBHOOK_TOKEN:
        raise HTTPException(401)
    try:
        b = await get_booking(booking_id)
        job_id = create_job(b["agent_phone"], booking_id, urgent)
    except BookingNotFound as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"job_id": job_id, "status": "queued", "agent": b["agent_name"]}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str, x_token: str = Header(default="")):
    if x_token != WEBHOOK_TOKEN:
        raise HTTPException(401)
    with db() as con:
        job = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        raise HTTPException(404)
    return dict(job)


@app.get("/health")
async def health():
    return {"ok": True, "ami": ami.writer is not None}
