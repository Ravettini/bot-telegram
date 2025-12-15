from __future__ import annotations

import os
from dotenv import load_dotenv

from common import (
    load_state, save_state, today_in_tz, fetch_mep,
    compute_board, build_daily_message, build_alert_message,
    send_telegram_message, estimate_ars_today
)

load_dotenv()

USD_INICIAL = float(os.getenv("USD_INICIAL", "1600"))
COSTO_SALIDA = float(os.getenv("COSTO_SALIDA", "0.007"))
TIMEZONE = os.getenv("TIMEZONE", "America/Argentina/Buenos_Aires")

# manual | estimate
ARS_MODE = os.getenv("ARS_MODE", "manual").strip().lower()


def main() -> None:
    state = load_state()
    users = state.get("users", {})
    if not users:
        print("No hay usuarios en state.json")
        return

    hoy = today_in_tz(TIMEZONE).isoformat()
    mep, ts = fetch_mep()

    for chat_id, user in users.items():
        if user.get("step") != "ready":
            continue
        if user.get("last_sent") == hoy:
            continue

        ars_hoy = user.get("ars_hoy")
        tna = user.get("tna_pesos")
        dias = user.get("horizonte_dias")
        fecha_inicio = user.get("fecha_inicio")

        # Si elegiste modo estimado, proyecta ars_hoy desde la última actualización
        if ARS_MODE == "estimate":
            if ars_hoy is not None and tna is not None and user.get("last_ars_update"):
                ars_hoy = estimate_ars_today(
                    last_ars=float(ars_hoy),
                    last_date=str(user["last_ars_update"]),
                    timezone=TIMEZONE,
                    tna_pesos=float(tna),
                )

        if ars_hoy is None:
            send_telegram_message(chat_id, "Me falta tu ARS actual. Actualizá state.json (ars_hoy) y listo.")
            user["last_sent"] = hoy
            continue

        if tna is None or dias is None:
            send_telegram_message(chat_id, "Me falta config (tna_pesos o horizonte_dias) en state.json.")
            user["last_sent"] = hoy
            continue

        board = compute_board(
            usd_inicial=USD_INICIAL,
            costo_salida=COSTO_SALIDA,
            timezone=TIMEZONE,
            fecha_inicio=str(fecha_inicio),
            horizonte_dias=int(dias),
            tna_pesos=float(tna),
            ars_hoy=float(ars_hoy),
            mep_hoy=float(mep),
            aportes=user.get("aportes", []),
        )

        msg = build_daily_message(board, int(dias))
        if ts:
            msg += f"\n\n(Actualización MEP: {ts})"

        send_telegram_message(chat_id, msg)

        alert = build_alert_message(board)
        if alert:
            send_telegram_message(chat_id, alert)

        user["last_sent"] = hoy

    save_state(state)
    print("OK - enviados los mensajes del día")


if __name__ == "__main__":
    main()
