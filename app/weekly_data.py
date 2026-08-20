from datetime import date, timedelta

try:
    from .offer_data import ALLOWED_STORES, migrate_akcie_schema, offer_key_for, validate_offer
except ImportError:
    from offer_data import ALLOWED_STORES, migrate_akcie_schema, offer_key_for, validate_offer


STATUS_TABLE = "zber_stav"

# Rovnaká ponuka pozbieraná v dvoch týždňových priehradkách sa nesmie
# používateľovi ukázať dvakrát. Identitu tvoria overené fakty BEZ `tyzden`.
_DEDUP_FIELDS = (
    "obchod", "source_url", "source_page", "nazov", "jednotka",
    "cena", "povodna", "kategoria", "zlava", "valid_from", "valid_to",
)


def current_monday(today: date | None = None) -> str:
    """Priehradka, do ktorej zber ZAPISUJE (nie podľa čoho sa číta)."""
    today = today or date.today()
    return (today - timedelta(days=today.weekday())).isoformat()


def current_verified_offers(con, stores, today: date | None = None):
    """Ponuky, ktoré DNES naozaj platia — bez ohľadu na týždeň zberu.

    Slovenské letáky bežia typicky štvrtok–streda. Keby sme filtrovali podľa
    `tyzden` (pondelok dňa zberu), platný zvyšok letáka (po, ut, st) by v
    pondelok zmizol a appka by až do ďalšieho behu zbierača nemala nič.
    O viditeľnosti rozhoduje výhradne platnosť samotného letáka.
    """
    stores = [store for store in stores if store in ALLOWED_STORES]
    if not stores:
        return []

    migrate_akcie_schema(con)
    today = today or date.today()
    stamp = today.isoformat()
    marks = ",".join("?" for _ in stores)
    cursor = con.execute(
        f"""SELECT * FROM akcie
            WHERE obchod IN ({marks})
              AND valid_from IS NOT NULL AND valid_to IS NOT NULL
              AND valid_from <= ? AND ? <= valid_to
            ORDER BY cena""",
        (*stores, stamp, stamp),
    )
    columns = [column[0] for column in cursor.description]
    newest, order = {}, []
    for row in cursor.fetchall():
        offer = dict(row) if hasattr(row, "keys") else dict(zip(columns, row))
        try:
            validate_offer(offer)
            if offer.get("offer_key") != offer_key_for(offer["tyzden"], offer):
                continue
        except ValueError:
            continue
        if not offer["valid_from"] <= stamp <= offer["valid_to"]:
            continue
        identity = tuple(offer.get(field) for field in _DEDUP_FIELDS)
        previous = newest.get(identity)
        if previous is None:
            newest[identity] = (offer["tyzden"], row)
            order.append(identity)
        elif offer["tyzden"] > previous[0]:
            newest[identity] = (offer["tyzden"], row)
    return [newest[identity][1] for identity in order]


def offers_for_current_week(con, stores: list[str], today: date | None = None):
    return current_verified_offers(con, stores, today)


def _has_status_table(con) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (STATUS_TABLE,)
        ).fetchone()
    )


def collection_outcomes(con, today: date | None = None, week: str | None = None) -> dict:
    """Výsledok zberu pre každý obchod zvlášť; {} keď to DB nevie povedať."""
    if not _has_status_table(con):
        return {}
    week = week or current_monday(today)
    cursor = con.execute(
        f"SELECT obchod, stav, pocet, detail, updated FROM {STATUS_TABLE} WHERE tyzden=?",
        (week,),
    )
    columns = [column[0] for column in cursor.description]
    outcomes = {}
    for row in cursor.fetchall():
        record = dict(row) if hasattr(row, "keys") else dict(zip(columns, row))
        outcomes[record["obchod"]] = record
    return outcomes


def stores_missing_this_week(con, stores, today: date | None = None) -> list[str]:
    """Obchody, ktoré tento týždeň NEBOLI úspešne pozbierané.

    Neprázdny výsledok znamená čiastočný beh: zdravé obchody prevýšia
    akýkoľvek prah počtu riadkov, takže bez tohto sa chýbajúci obchod
    nedá odhaliť a používateľ sa nikdy nedozvie, že mu obchod chýba.
    """
    outcomes = collection_outcomes(con, today)
    return sorted(
        store for store in stores
        if store in ALLOWED_STORES and outcomes.get(store, {}).get("stav") != "ok"
    )
