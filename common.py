from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, Optional, Tuple, List

import requests


# -----------------------------
# Formatting
# -----------------------------
def money_ars(x: float) -> str:
    s = f"{x:,.2f}"
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return s

def money_usd(x: float) -> str:
    return f"{x:,.2f}"

def pct(x: float) -> str:
    return f"{x*100:.2f}%"

def today_in_tz(tz_name: str) -> date:
    return datetime.now(ZoneInfo(tz_name)).date()

def parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


# -----------------------------
# State (simple JSON storage)
# -----------------------------
STATE_FILE = os.getenv("STATE_FILE", "state.json")

def load_state() -> Dict[str, Any]:
    if not os.path.exists(STATE_FILE):
        return {"users": {}}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def save_state(state: Dict[str, Any]) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


# -----------------------------
# MEP Fetch
# -----------------------------
def fetch_mep() -> Tuple[float, Optional[str]]:
    """
    Returns (mep_price, fechaActualizacion_or_None)
    Defaults to DolarApi MEP endpoint, field 'venta'
    """
    url = os.getenv("MEP_API_URL", "https://dolarapi.com/v1/dolares/bolsa")
    field = os.getenv("MEP_FIELD", "venta")

    r = requests.get(url, timeout=20)
    r.raise_for_status()
    j = r.json()
    mep = float(j[field])
    ts = j.get("fechaActualizacion")
    return mep, ts


# -----------------------------
# Telegram send
# -----------------------------
def send_telegram_message(chat_id: str | int, message: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN en .env")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": str(chat_id),
        "text": message,
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Telegram API error {r.status_code}: {r.text}")


# -----------------------------
# Carry math
# -----------------------------
@dataclass
class CarryConfig:
    usd_inicial: float
    costo_salida: float
    timezone: str

@dataclass
class CarryUser:
    step: str  # ask_ars / ask_tna / ask_days / ready
    ars_hoy: Optional[float]
    tna_pesos: Optional[float]
    horizonte_dias: Optional[int]
    fecha_inicio: str  # YYYY-MM-DD
    aportes: List[List]  # [["YYYY-MM-DD", monto], ...]
    last_sent: Optional[str]  # YYYY-MM-DD
    last_ars_update: Optional[str]  # YYYY-MM-DD

def compute_board(
    *,
    usd_inicial: float,
    costo_salida: float,
    timezone: str,
    fecha_inicio: str,
    horizonte_dias: int,
    tna_pesos: float,
    ars_hoy: float,
    mep_hoy: float,
    aportes: List[List],
) -> dict:
    hoy = today_in_tz(timezone)
    fi = parse_date(fecha_inicio)
    fecha_90 = fi.fromordinal(fi.toordinal() + horizonte_dias)

    dias_transcurridos = (hoy - fi).days
    dia_n = dias_transcurridos + 1
    dias_restantes = max(0, horizonte_dias - dias_transcurridos)

    usd_hoy = (ars_hoy * (1 - costo_salida)) / mep_hoy
    delta_usd = usd_hoy - usd_inicial

    ars_90 = ars_hoy * (1 + tna_pesos * (dias_restantes / 365))

    for f_ap, m_ap in aportes or []:
        try:
            ap_date = parse_date(str(f_ap))
        except Exception:
            continue
        if ap_date > fecha_90:
            continue
        dias_hasta_90 = max(0, (fecha_90 - ap_date).days)
        ars_90 += float(m_ap) * (1 + tna_pesos * (dias_hasta_90 / 365))

    be_90 = (ars_90 * (1 - costo_salida)) / usd_inicial
    margen_pct = (be_90 - mep_hoy) / be_90 if be_90 > 0 else float("nan")

    umbral_amarillo = be_90 * 0.95
    umbral_rojo = be_90 * 1.00

    if mep_hoy < umbral_amarillo:
        sem, txt = "🟢", "Vas bien, margen cómodo."
        alerta = None
    elif mep_hoy < umbral_rojo:
        sem, txt = "🟡", "Cerca del BE, ojo con volatilidad."
        alerta = "amarilla"
    else:
        sem, txt = "🔴", "MEP ≥ BE: el carry ya no suma USD vs tu base."
        alerta = "roja"

    return {
        "hoy": hoy.isoformat(),
        "dia_n": dia_n,
        "dias_restantes": dias_restantes,
        "fecha_90": fecha_90.isoformat(),
        "mep_hoy": mep_hoy,
        "ars_hoy": ars_hoy,
        "usd_hoy": usd_hoy,
        "delta_usd": delta_usd,
        "be_90": be_90,
        "margen_pct": margen_pct,
        "semaforo": sem,
        "texto_senal": txt,
        "umbral_amarillo": umbral_amarillo,
        "umbral_rojo": umbral_rojo,
        "alerta": alerta,
    }

def build_daily_message(board: dict, horizonte_dias: int) -> str:
    return (
        f"📌 Carry {horizonte_dias} días — Día {board['dia_n']}/{horizonte_dias} "
        f"(faltan {board['dias_restantes']} días)\n"
        f"MEP hoy: ${money_ars(board['mep_hoy'])}\n"
        f"Pesos hoy (FCI): ${money_ars(board['ars_hoy'])}\n"
        f"Si salís hoy: {money_usd(board['usd_hoy'])} USD (Δ vs inicio: {money_usd(board['delta_usd'])} USD)\n\n"
        f"Break-even (a día {horizonte_dias} / {board['fecha_90']}): ${money_ars(board['be_90'])}\n"
        f"Margen vs BE: {pct(board['margen_pct'])}\n"
        f"Señal: {board['semaforo']} {board['texto_senal']}\n"
        f"Alertas: 🟡 ${money_ars(board['umbral_amarillo'])} | 🔴 ${money_ars(board['umbral_rojo'])}"
    )

def build_alert_message(board: dict) -> Optional[str]:
    if board.get("alerta") == "amarilla":
        return (
            "⚠️ Alerta (cerca del BE)\n"
            f"MEP: ${money_ars(board['mep_hoy'])} | BE: ${money_ars(board['be_90'])} "
            f"(margen {pct(board['margen_pct'])})\n"
            f"Si cerrás hoy: {money_usd(board['usd_hoy'])} USD (Δ {money_usd(board['delta_usd'])})"
        )
    if board.get("alerta") == "roja":
        return (
            "🛑 Alerta (cruzó BE)\n"
            f"MEP: ${money_ars(board['mep_hoy'])} ≥ BE: ${money_ars(board['be_90'])}\n"
            "El carry ya no suma USD vs base.\n"
            f"Si cerrás hoy: {money_usd(board['usd_hoy'])} USD (Δ {money_usd(board['delta_usd'])})"
        )
    return None
