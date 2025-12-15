from __future__ import annotations

import os
from flask import Flask, request
from dotenv import load_dotenv

from common import (
    load_state, save_state, today_in_tz, fetch_mep,
    compute_board, build_daily_message,
    send_telegram_message,
)

load_dotenv()

app = Flask(__name__)

TIMEZONE = os.getenv("TIMEZONE", "America/Argentina/Buenos_Aires")
USD_INICIAL = float(os.getenv("USD_INICIAL", "1600"))
COSTO_SALIDA = float(os.getenv("COSTO_SALIDA", "0.007"))

# opcional (recomendado) para asegurar el webhook:
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")


def normalize_number(text: str) -> float:
    # soporta "2.450.000", "2450000", "45", "0.45", "45%"
    t = text.strip().lower().replace("%", "")
    t = t.replace(".", "").replace(",", ".")  # arg style -> float
    return float(t)


def ensure_user(state: dict, chat_id: str) -> dict:
    users = state.setdefault("users", {})
    if chat_id not in users:
        users[chat_id] = {
            "step": "ask_ars",
            "ars_hoy": None,
            "tna_pesos": None,
            "horizonte_dias": None,
            "fecha_inicio": today_in_tz(TIMEZONE).isoformat(),
            "aportes": [],
            "last_sent": None,
            "last_ars_update": None,
        }
    return users[chat_id]


def help_text(user: dict) -> str:
    return (
        "Comandos:\n"
        "- status  → tablero ahora\n"
        "- ars 2450000  → actualizar pesos hoy\n"
        "- tna 45  (o 0.45) → actualizar TNA\n"
        "- dias 90 → actualizar horizonte\n"
        "- inicio 2025-12-01 → setear fecha inicio\n"
        "- aporte 800000 → agrega un aporte con fecha de hoy\n"
        "- ayuda → ver esto\n"
    )


def handle_onboarding(user: dict) -> str:
    step = user.get("step")

    if step == "ask_ars":
        return "¿Cuánto tenés invertido en pesos? (ej: 2450000)"

    if step == "ask_tna":
        return "¿A qué TNA lo tenés? (ej: 45 o 0.45)"

    if step == "ask_days":
        return "¿Cuántos días querés que dure el carry? (ej: 90)"

    return "Listo. Mandá 'status' para ver el tablero."


def advance_onboarding(user: dict, text: str) -> str | None:
    step = user.get("step")

    if step == "ask_ars":
        user["ars_hoy"] = normalize_number(text)
        user["last_ars_update"] = today_in_tz(TIMEZONE).isoformat()
        user["step"] = "ask_tna"
        return "Perfecto. ¿A qué TNA lo tenés? (ej: 45 o 0.45)"

    if step == "ask_tna":
        tna = normalize_number(text)
        if tna > 1.5:  # si ponen 45, lo paso a 0.45
            tna = tna / 100.0
        user["tna_pesos"] = tna
        user["step"] = "ask_days"
        return "Genial. ¿Cuántos días querés que dure el carry? (ej: 90)"

    if step == "ask_days":
        user["horizonte_dias"] = int(normalize_number(text))
        user["step"] = "ready"
        return (
            "✅ Listo. Quedó configurado.\n"
            "Mandá 'status' para ver el tablero.\n"
            "Tip: cada vez que cambie tu FCI, mandá: ars <monto>\n"
        )

    return None


def compute_and_format_status(user: dict) -> str:
    if user.get("step") != "ready":
        return handle_onboarding(user)

    ars_hoy = user.get("ars_hoy")
    tna = user.get("tna_pesos")
    dias = user.get("horizonte_dias")
    fecha_inicio = user.get("fecha_inicio")

    if ars_hoy is None or tna is None or dias is None:
        return "Falta configuración. Mandá 'ayuda' o escribí cualquier cosa para retomar."

    mep, ts = fetch_mep()

    board = compute_board(
        usd_inicial=USD_INICIAL,
        costo_salida=COSTO_SALIDA,
        timezone=TIMEZONE,
        fecha_inicio=fecha_inicio,
        horizonte_dias=int(dias),
        tna_pesos=float(tna),
        ars_hoy=float(ars_hoy),
        mep_hoy=float(mep),
        aportes=user.get("aportes", []),
    )

    msg = build_daily_message(board, int(dias))
    if ts:
        msg += f"\n\n(Actualización MEP: {ts})"
    return msg


def process_message(user: dict, text: str) -> str:
    t = (text or "").strip()
    low = t.lower()

    # onboarding
    if user.get("step") != "ready":
        reply = advance_onboarding(user, t)
        return reply or handle_onboarding(user)

    if low in ("ayuda", "help", "/help", "/start"):
        return help_text(user)

    if low in ("status", "/status"):
        return compute_and_format_status(user)

    if low.startswith("ars "):
        user["ars_hoy"] = normalize_number(t[4:])
        user["last_ars_update"] = today_in_tz(TIMEZONE).isoformat()
        return "✅ ARS actualizado. Mandá 'status' cuando quieras."

    if low.startswith("tna "):
        v = normalize_number(t[4:])
        if v > 1.5:
            v = v / 100.0
        user["tna_pesos"] = v
        return "✅ TNA actualizada. Mandá 'status' cuando quieras."

    if low.startswith("dias "):
        user["horizonte_dias"] = int(normalize_number(t[5:]))
        return "✅ Días actualizados. Mandá 'status' cuando quieras."

    if low.startswith("inicio "):
        user["fecha_inicio"] = t.split(maxsplit=1)[1].strip()
        return f"✅ Fecha inicio seteada a {user['fecha_inicio']}. Mandá 'status'."

    if low.startswith("aporte "):
        monto = normalize_number(t[7:])
        hoy = today_in_tz(TIMEZONE).isoformat()
        user.setdefault("aportes", []).append([hoy, float(monto)])
        # opcional: sumar al ars_hoy guardado (si querés)
        if user.get("ars_hoy") is not None:
            user["ars_hoy"] = float(user["ars_hoy"]) + float(monto)
        return "✅ Aporte registrado."

    return "No entendí. Mandá 'ayuda' para ver comandos."


@app.get("/")
def health():
    return "OK", 200


@app.post("/telegram")
def telegram_webhook():
    # Seguridad opcional: Telegram puede mandar header X-Telegram-Bot-Api-Secret-Token
    if TELEGRAM_WEBHOOK_SECRET:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if got != TELEGRAM_WEBHOOK_SECRET:
            return "unauthorized", 401

    update = request.get_json(silent=True) or {}
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return "ok", 200

    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id", ""))
    text = msg.get("text", "")

    if not chat_id:
        return "ok", 200

    state = load_state()
    user = ensure_user(state, chat_id)

    reply = process_message(user, text)

    save_state(state)
    send_telegram_message(chat_id, reply)

    return "ok", 200
