"""Accesso: verifica dei dati `initData` che Telegram passa alla Mini App.

Telegram firma i dati con HMAC-SHA256: la chiave è HMAC("WebAppData", token del bot) e il
messaggio è l'elenco dei campi (tranne `hash`) in ordine alfabetico, uno per riga, "chiave=valore".
Oltre alla firma si controllano l'età dei dati (al massimo 24 ore) e che l'utente sia autorizzato.
"""
import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

DURATA_MASSIMA = 24 * 60 * 60  # secondi


class AccessoNegato(Exception):
    """initData assente, falsificata, scaduta o di un utente non autorizzato."""


def firma(campi: dict[str, str], token: str) -> str:
    """Calcola la firma che Telegram metterebbe nel campo `hash` per questi campi."""
    testo = "\n".join(f"{k}={v}" for k, v in sorted(campi.items()) if k != "hash")
    chiave = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    return hmac.new(chiave, testo.encode(), hashlib.sha256).hexdigest()


def verifica_init_data(init_data: str, token: str, autorizzati, adesso: float | None = None) -> dict:
    """Restituisce l'utente Telegram (dict con almeno `id`) se tutto torna, altrimenti AccessoNegato."""
    if not init_data:
        raise AccessoNegato("dati mancanti")
    try:
        campi = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        raise AccessoNegato("dati illeggibili")
    ricevuta = campi.get("hash", "")
    if not ricevuta or not hmac.compare_digest(firma(campi, token), ricevuta):
        raise AccessoNegato("firma non valida")
    try:
        data = int(campi["auth_date"])
        utente = json.loads(campi["user"])
        id_utente = int(utente["id"])
    except (KeyError, ValueError, TypeError):
        raise AccessoNegato("dati incompleti")
    adesso = time.time() if adesso is None else adesso
    if adesso - data > DURATA_MASSIMA:
        raise AccessoNegato("dati scaduti")
    if id_utente not in autorizzati:
        raise AccessoNegato("utente non autorizzato")
    return utente
