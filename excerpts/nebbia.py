"""Nebbia viva: quando un ramo senza figli viene sbloccato, il generatore crea i suoi sotto-rami.

In background (coda del generatore) chiede a Claude 3-6 sotto-rami; se uno ha lo stesso argomento
di un ramo già esistente lo collega come figlio (più padri) invece di duplicarlo. Per ogni ramo
nuovo genera la banca di domande d'ingresso (12+) e lo salva solo se la banca è buona.
Lo stato del lavoro sta in `diradamenti` (in corso, fatto, fallito) per l'interfaccia.
La radice e i contenitori non si diradano: i loro figli vengono dall'albero di base.
I sotto-rami aggiunti a mano dagli utenti (innesti) non impediscono il diradamento.
"""
import contextlib
import logging
import re
import unicodedata

from . import db
from .albero import Albero, Nodo
from .domande import MINIMO_PER_RAMO
from .generatore import ErroreGeneratore, generatore

log = logging.getLogger("yggdrasil.nebbia")

TESTO = {"type": "string", "minLength": 3}
SCHEMA_RAMI = {"type": "object", "required": ["rami"], "properties": {"rami": {
    "type": "array", "minItems": 3, "maxItems": 6, "items": {
        "type": "object", "required": ["esistente", "nome", "descrizione", "linee_guida", "consigli"],
        "properties": {"esistente": {"type": "string"}, "nome": TESTO, "descrizione": TESTO,
                       "linee_guida": TESTO,
                       "consigli": {"type": "array", "minItems": 3, "maxItems": 6, "items": TESTO}}}}}}
SCHEMA_DOMANDE = {"type": "object", "required": ["domande"], "properties": {"domande": {
    "type": "array", "minItems": MINIMO_PER_RAMO, "maxItems": 20, "items": {
        "type": "object", "required": ["testo", "giusta", "sbagliate", "spiegazione"],
        "properties": {"testo": TESTO, "giusta": {"type": "string", "minLength": 1},
                       "sbagliate": {"type": "array", "minItems": 3, "maxItems": 3,
                                     "items": {"type": "string", "minLength": 1}},
                       "spiegazione": TESTO}}}}}


def da_diradare(albero: Albero, nodo: str, innestati=frozenset()) -> bool:
    """Un ramo sbloccato, non contenitore, che non ha ancora figli (salvo quelli innestati)."""
    n = albero.nodi.get(nodo)
    return (n is not None and n.padri and not n.contenitore and albero.sbloccato(nodo)
            and solo_innestati(albero.figli[nodo], innestati))


def solo_innestati(figli, innestati) -> bool:
    return all(f in innestati for f in figli)


def non_collegabili(albero: Albero, nodo: str) -> set[str]:
    """Il ramo, i suoi antenati e i suoi fratelli (argomenti alla pari, non sotto-argomenti)."""
    fratelli = {f for p in albero.nodi[nodo].padri for f in albero.figli[p]}
    return antenati(albero, nodo) | fratelli | {nodo}


def antenati(albero: Albero, nodo: str) -> set[str]:
    visti, da_vedere = set(), list(albero.nodi[nodo].padri)
    while da_vedere:
        p = da_vedere.pop()
        if p not in visti:
            visti.add(p)
            da_vedere += albero.nodi[p].padri
    return visti


def nuovo_id(nome: str, esistenti) -> str:
    base = unicodedata.normalize("NFKD", nome).encode("ascii", "ignore").decode().lower()
    base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")[:40].strip("-") or "ramo"
    candidato, i = base, 2
    while candidato in esistenti:
        candidato, i = f"{base}-{i}", i + 1
    return candidato


def percorso(albero: Albero, nodo: str) -> str:
    nomi = [albero.nodi[nodo].nome]
    while albero.nodi[nodo].padri:
        nodo = albero.nodi[nodo].padri[0]
        nomi.append(albero.nodi[nodo].nome)
    return " > ".join(reversed(nomi))


def prompt_rami(albero: Albero, nodo: str) -> str:
    n = albero.nodi[nodo]
    gia = ", ".join(f"«{albero.nodi[f].nome}»" for f in albero.figli[nodo])
    elenco = "\n".join(f"- {x.id}: {x.nome} — {x.descrizione}" for x in albero.nodi.values())
    return f"""Il ramo «{n.nome}» ({percorso(albero, nodo)}) è appena stato sbloccato.
Descrizione: {n.descrizione}

Proponi da 3 a 6 sotto-rami: gli argomenti in cui si divide naturalmente questo ramo, ognuno
abbastanza ampio da salire da Novizio a Maestro, senza sovrapporsi tra loro.
Per ognuno: nome breve (1-4 parole), descrizione di una frase, linee guida (come si sale di
livello, cosa conta davvero) e 3-5 consigli concreti su cosa studiare per cominciare.

{f"Ha già questi sotto-rami, aggiunti dall'utente: {gia}. Non riproporli: proponi gli altri." if gia else ""}

Rami che esistono già nell'albero:
{elenco}

Se un sotto-ramo che vorresti proporre ha lo stesso argomento di uno di questi, metti il suo id
in "esistente" (verrà collegato anche qui invece di essere duplicato); altrimenti "esistente"
è una stringa vuota. Collega solo rami che sono davvero un sotto-argomento di «{n.nome}»;
non indicare mai questi id: {", ".join(sorted(non_collegabili(albero, nodo)))}, né i contenitori."""


def prompt_domande(albero: Albero, padre: str, ramo: dict, fonte: str = "") -> str:
    return f"""Scrivi la prova d'ingresso del ramo «{ramo['nome']}» (sotto
{percorso(albero, padre)}): {ramo['descrizione']}

Servono 14 domande a scelta multipla di livello Novizio (le basi che chi comincia deve sapere),
varie e non ripetitive, ognuna con una sola risposta giusta, tre risposte sbagliate plausibili
ma chiaramente sbagliate per chi conosce l'argomento, e una spiegazione breve (1-2 frasi) della
risposta giusta. Controlla che ogni risposta giusta sia corretta e aggiornata.""" + (
        f"\nChi studia questo ramo lo sta imparando da «{fonte}»: tienine conto." if fonte else "")


def banca_da(dati: dict) -> list[dict]:
    """Converte le domande generate nel formato delle banche (prima risposta = giusta),
    scartando quelle con risposte ripetute. Servono almeno MINIMO_PER_RAMO domande buone."""
    banca = []
    for d in dati["domande"]:
        risposte = [d["giusta"].strip(), *(s.strip() for s in d["sbagliate"])]
        if len(set(r.lower() for r in risposte)) == 4 and all(risposte):
            banca.append({"testo": d["testo"].strip(), "risposte": risposte,
                          "spiegazione": d["spiegazione"].strip()})
    if len(banca) < MINIMO_PER_RAMO:
        raise ErroreGeneratore(f"solo {len(banca)} domande buone")
    return banca


def dirada(nodo: str, apri=db.apri, gen=generatore) -> int:
    """Crea i sotto-rami di `nodo`; restituisce quanti figli ha ora. Pensata per la coda."""
    with contextlib.closing(apri()) as conn:
        try:
            figli = _dirada(conn, nodo, gen)
        except Exception:
            log.exception("nebbia di %s: diradamento fallito", nodo)
            figli = 0
        db.imposta_diradamento(conn, nodo, "fatto" if figli else "fallito")
        log.info("nebbia di %s: %d sotto-rami", nodo, figli)
        return figli


def _dirada(conn, nodo: str, gen) -> int:
    albero = db.carica_albero(conn, 0)
    gia = albero.figli[nodo]
    if not solo_innestati(gia, db.innestati(conn)):
        return len(gia)
    vietati = non_collegabili(albero, nodo)
    proposti = gen.genera(f"sotto-rami {nodo}", prompt_rami(albero, nodo), SCHEMA_RAMI)["rami"]
    figli = len(gia)
    for ramo in proposti:
        esistente = ramo["esistente"].strip()
        if esistente:
            if esistente in albero.nodi and esistente not in vietati | set(gia) \
                    and not albero.nodi[esistente].contenitore:
                db.aggiungi_padre(conn, esistente, nodo)
                figli += 1
            continue  # un id sbagliato si scarta: meglio un ramo in meno che un doppione
        try:
            dati = gen.genera(f"domande {ramo['nome']}", prompt_domande(albero, nodo, ramo),
                              SCHEMA_DOMANDE)
            banca = banca_da(dati)
        except ErroreGeneratore as errore:
            log.warning("nebbia di %s: ramo %r scartato (%s)", nodo, ramo["nome"], errore)
            continue
        nuovo = Nodo(nuovo_id(ramo["nome"], albero.nodi), ramo["nome"].strip(),
                     ramo["descrizione"].strip(), ramo["linee_guida"].strip(),
                     [c.strip() for c in ramo["consigli"]], False, [nodo])
        db.salva_nodo(conn, nuovo)
        db.salva_banca(conn, nuovo.id, banca)
        albero.nodi[nuovo.id] = nuovo  # per gli id dei rami successivi
        figli += 1
    return figli


def avvia(conn, utente: int, nodo: str, gen=generatore) -> bool:
    """Se l'utente ha appena sbloccato un ramo da diradare, segna 'in corso' e lo mette in coda."""
    if nodo in db.diradamenti(conn) or not da_diradare(db.carica_albero(conn, utente), nodo,
                                                       db.innestati(conn)):
        return False
    db.imposta_diradamento(conn, nodo, "in corso")
    gen.accoda(dirada, nodo)
    return True


def riprendi(conn, gen=generatore) -> None:
    """All'avvio: rimette in coda i diradamenti interrotti da un riavvio."""
    for nodo in db.diradamenti(conn, "in corso"):
        gen.accoda(dirada, nodo)
