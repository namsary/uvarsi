from datetime import date, timedelta

from app.plan_shortlist import select_offers


TODAY = date.today()
WEEK = (TODAY - timedelta(days=TODAY.weekday())).isoformat()
STORES = ("Lidl", "Kaufland", "Tesco")
CORE_CATEGORIES = {"maso", "zelenina", "mliecne", "trvanlive"}


def offer_key(row):
    return row["offer_key"]


def normalized_category(row):
    return row["kategoria"].casefold().replace("ä", "a")


def offer(store, category, number, *, price=2.0, original=4.0):
    return {
        "tyzden": WEEK,
        "obchod": store,
        "nazov": f"{category} {store} {number}",
        "kategoria": category,
        "cena": price,
        "povodna": original,
        "zlava": "-50 %",
        "jednotka": "500 g",
        "source_url": f"https://example.test/{store.casefold()}/{number}",
        "source_page": number + 1,
        "valid_from": (TODAY - timedelta(days=1)).isoformat(),
        "valid_to": (TODAY + timedelta(days=1)).isoformat(),
        "offer_key": f"offer_{number:012x}",
    }


def fixture_with_582_offers():
    rows = []
    for store_index, store in enumerate(STORES):
        for category_index, category in enumerate(("mäso", "zelenina", "mliecne", "trvanlive")):
            for number in range(48 + (store_index == 0 and category_index == 0)):
                rows.append(offer(store, category, store_index * 200 + category_index * 50 + number))
    assert len(rows) == 577
    rows.extend(offer("Lidl", "ovocie", 600 + number) for number in range(5))
    assert len(rows) == 582
    return rows


def test_shortlist_is_bounded_deterministic_and_covers_each_store_and_core_category():
    rows = fixture_with_582_offers()

    first = select_offers(rows, STORES)
    second = select_offers(list(reversed(rows)), tuple(reversed(STORES)))

    assert [offer_key(row) for row in first] == [offer_key(row) for row in second]
    assert len(first) == 120
    assert {row["obchod"] for row in first} == set(STORES)
    assert CORE_CATEGORIES <= {normalized_category(row) for row in first}


def test_shortlist_honors_a_caller_bound_even_when_coverage_needs_more_slots():
    rows = fixture_with_582_offers()

    shortlisted = select_offers(rows, STORES, limit=5)

    assert len(shortlisted) == 5


def test_shortlist_excludes_invalid_expired_wrong_week_duplicate_and_unrequested_offers():
    good = offer("Lidl", "mäso", 1)
    duplicate = dict(good, nazov="Duplicated row")
    expired = dict(offer("Lidl", "zelenina", 2), valid_to=(TODAY - timedelta(days=1)).isoformat())
    wrong_week = dict(offer("Lidl", "mliecne", 3), tyzden="2000-01-03")
    invalid = dict(offer("Lidl", "trvanlive", 4), cena=0)
    unrequested = offer("Tesco", "mäso", 5)

    shortlisted = select_offers(
        [good, duplicate, expired, wrong_week, invalid, unrequested], ["Lidl"], limit=120
    )

    assert [offer_key(row) for row in shortlisted] == [offer_key(good)]


def test_shortlist_chooses_the_same_duplicate_representative_regardless_of_input_order():
    first = offer("Lidl", "mäso", 10)
    second = dict(first, nazov="Alternatívny názov")

    forward = select_offers([first, second], ["Lidl"])
    backward = select_offers([second, first], ["Lidl"])

    assert forward == backward
