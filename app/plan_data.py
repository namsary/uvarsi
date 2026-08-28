"""Trusted reconstruction of a personal plan from content-only model output."""
import hashlib
import json
import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_CEILING

try:
    from .weekly_data import current_monday, current_verified_offers
    from .offer_data import canonical_offer_key
except ImportError:
    from weekly_data import current_monday, current_verified_offers
    from offer_data import canonical_offer_key


CENT = Decimal("0.01")
DAY_ORDER = ("PO", "UT", "ST", "ŠT", "PI", "SO", "NE")
STORE_ORDER = ("Kaufland", "Lidl", "Tesco")
_MODEL_TOP_LEVEL = frozenset({"meals"})
_MODEL_MEAL = frozenset({"day", "name", "minutes", "instructions", "items", "pantry_ingredients"})
_MODEL_ITEM = frozenset({
    "offer_key", "quantity", "amount_per_adult", "amount_per_person", "unit",
    "ingredient_role",
})

# Recept je použiteľný až vtedy, keď povie koľko, ako dlho a na čom.
# „Pridaj cibuľu a opeč" je nič; „Na 2 lyžiciach oleja opeč 2 cibule nakrájané
# na kocky 5 minút do sklovita" sa dá vziať a uvariť podľa toho.
MIN_STEPS_PER_MEAL = 3
MIN_STEP_WORDS = 6
MIN_STEP_CHARS = 30
STAPLES = ("soľ", "korenie", "olej", "voda")
GOOD_STEP_EXAMPLE = "Na 2 lyžiciach oleja opeč 2 cibule nakrájané na kocky 5 minút do sklovita."
BAD_STEP_EXAMPLE = "Pridaj cibuľu a opeč."

_NUMBER = re.compile(r"\d")
_QUANTITY = re.compile(
    r"\d+(?:[.,]\d+)?\s*(?:kg|g|ml|dl|l|ks|cm|lyžic|lyžičk|hrst|hrsť|štipk|plátk|"
    r"strúčik|balen|konzerv|porci|vajc|kus|téglik|pohár|kock)",
    re.IGNORECASE,
)
_DURATION = re.compile(r"\d+\s*(?:-\s*\d+\s*)?(?:min|sek|hodin|hod\b|h\b)", re.IGNORECASE)
_COOKS_WITH_HEAT = re.compile(
    # „podávaj s pečivom" nie je tepelná úprava, preto peč(?!iv) — inak by sme
    # od studeného šalátu pýtali teplotu rúry.
    r"opeč|peč(?!iv)|smaž|vypráž|opraž|resto|restu|zohrej|ohrej|vari|uvar|dus|griluj|zapeč|blanš|prevar",
    re.IGNORECASE,
)
_HEAT_LEVEL = re.compile(r"°\s*c|stupň|ohni|oheň|plamen|plameň|predhri|predhrej|teplot|výkon", re.IGNORECASE)

# Kto nikdy nevaril, ten z „nakrájaj mäso" nevie nič. Tvar rezu je to prvé,
# čo v postupe musí byť: na kocky, na plátky, na prúžky, na kolieska.
_CUTS = re.compile(r"krája|sekaj|sekan|nakroj|rozkroj", re.IGNORECASE)
_CUT_SHAPE = re.compile(
    r"kock|plátk|plátok|prúžk|koliesk|krúžk|najemno|na jemno|nadrobno|nahrubo|drobno|"
    r"pásik|pás\b|štvrt|osmin|polovic|rezanc|rez\b|mesiačik|dielik|hranolč|kúsk|"
    r"špalík|guľôčk|hrub|tenk|krajc|\d+\s*cm",
    re.IGNORECASE,
)
# Vňať či bylinky sa krájajú „len tak" — tvar rezu má zmysel pýtať pri surovine,
# ktorá tvorí jedlo, nie pri petržlenovej vňati na ozdobu.
_GARNISH = re.compile(
    r"vňať|vňat|petržlen|pažítk|bazalk|koriand|kôpor|mät|rozmarín|tymian|zelen\w* na ozdob",
    re.IGNORECASE,
)
# Recept sa musí skončiť tak, že jedlo je na stole — nie v hrnci.
_SERVES = re.compile(
    r"podávaj|podávame|podáva sa|podávaní|servíruj|naservíruj|rozdeľ|rozlož na tanier|"
    r"prelož na tanier|navrši|naber do|nandaj|ulož na tanier|na taniere|na tanier",
    re.IGNORECASE,
)
# A aspoň raz musí povedať, ako má výsledok vyzerať.
_LOOKS_DONE = re.compile(
    r"do sklovit|dozlat|do zlat|zlatist|zlatohned|domäkk|do mäkk|mäkk|do chrumkav|"
    r"chrumkav|do ružov|kým|kôrk|zovrie|prevrie|zhustne|zmäkn|nevsiakn|neodpar|"
    r"penist|roztopí|roztopen|nezhnedn|nepust|hustá|hustý|šťavnat|voňav|hotov",
    re.IGNORECASE,
)

# Množstvá, s ktorými vieme rátať. Všetko ostatné je pre nákupný zoznam
# nepoužiteľné — z „hrsti" sa počet balení dopočítať nedá.
UNITS = {
    "g": ("g", Decimal("1")), "gram": ("g", Decimal("1")), "gramov": ("g", Decimal("1")),
    "dkg": ("g", Decimal("10")), "kg": ("g", Decimal("1000")),
    "ml": ("ml", Decimal("1")), "dl": ("ml", Decimal("100")), "l": ("ml", Decimal("1000")),
    "ks": ("ks", Decimal("1")), "kus": ("ks", Decimal("1")), "kusov": ("ks", Decimal("1")),
}
CHILD_PORTION_FACTOR = Decimal("0.65")
PORTION_STANDARD_VERSION = 1
INGREDIENT_ROLES = frozenset({
    "protein_main", "dry_starch", "potato", "legume_dry", "vegetable",
    "vegetable_addition", "bread", "egg", "dairy_main", "dairy_addition", "sauce_liquid",
    "fat_addition", "other",
})
PORTION_RANGES = {
    "protein_main": {"g": (Decimal("120"), Decimal("200"))},
    "dry_starch": {"g": (Decimal("60"), Decimal("110"))},
    "potato": {"g": (Decimal("200"), Decimal("400"))},
    "legume_dry": {"g": (Decimal("60"), Decimal("110"))},
    "vegetable": {"g": (Decimal("120"), Decimal("350"))},
    "vegetable_addition": {"g": (Decimal("5"), Decimal("120"))},
    "bread": {"g": (Decimal("60"), Decimal("150"))},
    "egg": {"ks": (Decimal("1"), Decimal("3"))},
    "dairy_main": {"g": (Decimal("60"), Decimal("150"))},
    "dairy_addition": {"g": (Decimal("10"), Decimal("60"))},
    "sauce_liquid": {"ml": (Decimal("100"), Decimal("400"))},
    "fat_addition": {
        "g": (Decimal("5"), Decimal("40")),
        "ml": (Decimal("5"), Decimal("40")),
    },
    # Neznáma položka ostane použiteľná, ale nikdy nedostane pôvodný extrémny
    # limit 3 kg/3 l/20 ks na jednu osobu.
    "other": {
        "g": (Decimal("0"), Decimal("500")),
        "ml": (Decimal("0"), Decimal("500")),
        "ks": (Decimal("0"), Decimal("3")),
    },
}
_PACKAGE = re.compile(
    r"(?:(\d+)\s*[x×]\s*)?(\d+(?:[.,]\d+)?)\s*(dkg|kg|dl|ml|ks|g|l)\b", re.IGNORECASE
)
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)

# Slová, ktoré v názve jedla nesľubujú surovinu, takže ich v postupe nehľadáme.
# Sú to druhy jedál a prívlastky: „Bravčový guláš" nemusí mať v krokoch slovo
# guláš, ale musí mať bravčové mäso.
NAME_FILLER = (
    "jedlo", "obed", "večera", "raňajky", "porcia", "recept", "misa", "hrniec", "panvica",
    "pekáč", "domáci", "rýchly", "jednoduchý", "klasický", "tradičný", "sviatočný",
    "nedeľný", "babkin", "výdatný", "poctivý", "chutný", "ľahký", "letný", "zimný",
    "obľúbený", "expres", "podľa", "svojsky", "pečený", "varený", "dusený", "smažený",
    "guláš", "polievka", "prívarok", "rizoto", "nákyp", "zapekanka", "omáčka", "ragú",
    "perkelt", "pilaf", "praženica", "omeleta", "lečo", "kapustnica", "sekaná",
    "karbonátky", "fašírky", "štrúdľa", "plnka", "miska", "tanier", "hrnček",
    # Kategórie, nie suroviny: „Zeleninový šalát" menuje paradajky a uhorky,
    # slovo zelenina v krokoch stáť nemusí.
    "zeleninový", "mäsový", "ovocný", "rybí", "rybací", "bylinkový",
)
# Prílohy, na ktorých sa jedlo naozaj podáva. „Kuracie na ryži" znamená, že
# na konci je ryža na tanieri a mäso na nej — nie ryža vmiešaná v polovici.
SERVING_BASES = (
    "ryža", "zemiaky", "cestoviny", "halušky", "knedľa", "kuskus", "bulgur", "špagety",
    "pyré", "šalát", "toast", "chlieb", "placka", "tortilla", "polenta", "hranolky",
    "rezance", "krúpy", "pečivo", "bageta", "kaša", "ryžou",
)
_ON_TOP_OF = re.compile(r"\b(?:na|pod)\s+([^\W\d_]+)", re.IGNORECASE)


def meal_count_for_frequency(frequency):
    return {1: 7, 2: 4, 3: 3}.get(frequency, 3)


def cooking_days_for_frequency(frequency):
    """Kalendár varenia odvodíme v Pythone; model si vyberá už len jedlá.

    Platný pondelok–nedeľa rozvrh je 7/4/3: každý deň, PO/ST/PI/NE alebo
    PO/ŠT/NE. Model nesmie vynechať koniec týždňa ani presunúť varenie inam.
    """
    count = meal_count_for_frequency(frequency)
    usable = isinstance(frequency, int) and not isinstance(frequency, bool) and frequency >= 1
    spacing = frequency if usable else 2
    if count > 1:
        spacing = min(spacing, (len(DAY_ORDER) - 1) // (count - 1))
    return tuple(DAY_ORDER[index * spacing] for index in range(count))


def days_covered_by_meal(frequency, day=None):
    """Koľko dní pokrýva dávka v konkrétny cooking day, najviac po nedeľu."""
    days = cooking_days_for_frequency(frequency)
    spans = tuple(
        (DAY_ORDER.index(days[index + 1]) if index + 1 < len(days) else len(DAY_ORDER))
        - DAY_ORDER.index(cooking_day)
        for index, cooking_day in enumerate(days)
    )
    if day is None:
        return spans[0] if spans else 1
    try:
        return spans[days.index(day)]
    except ValueError as error:
        raise ValueError(f"Deň {day} nie je dňom varenia.") from error


def portions_for(household_size, frequency, day=None):
    """Počet porcií dávky; nedeľná dávka nikdy nepresahuje do ďalšieho týždňa."""
    return household_size * days_covered_by_meal(frequency, day)


def _validated_people(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 12:
        raise ValueError(f"Počet {field} musí byť celé číslo od 0 do 12.")
    return value


def _household(household_size=None, adults=None, children=None):
    """Normalize the new profile while keeping the old household keyword usable.

    Legacy callers treated every person as an adult. New callers must either
    provide both composition fields or neither; mixing the two contracts would
    make cache signatures and ingredient quantities disagree.
    """
    explicit = adults is not None or children is not None
    if not explicit:
        size = _validated_household_size(household_size)
        return size, 0, True
    if household_size is not None:
        raise ValueError("Použi buď počet osôb, alebo dospelých a deti, nie oboje.")
    adults = _validated_people(adults, "dospelých")
    children = _validated_people(children, "detí")
    if adults + children < 1 or adults + children > 12:
        raise ValueError("Domácnosť musí mať spolu 1 až 12 osôb.")
    return adults, children, False


def servings_for(adults, children, frequency, day=None):
    """Skutočný počet podávaných tanierov pre konkrétnu várku."""
    adults, children, _ = _household(None, adults, children)
    return (adults + children) * days_covered_by_meal(frequency, day)


def adult_equivalents_for(adults, children, frequency, day=None):
    """Kuchárska dávka, nie výživové odporúčanie.

    Dieťa približne vo veku 3–12 rokov sa pre nákup ráta ako 0,65 dospelej
    porcie. Tínedžer s dospelou porciou patrí v profile medzi dospelých.
    """
    adults, children, _ = _household(None, adults, children)
    days = Decimal(days_covered_by_meal(frequency, day))
    return (Decimal(adults) + Decimal(children) * CHILD_PORTION_FACTOR) * days


def _adult_word(count):
    return "dospelý" if count == 1 else "dospelí" if count < 5 else "dospelých"


def _child_word(count):
    return "dieťa" if count == 1 else "deti" if count < 5 else "detí"


def _household_text(adults, children, legacy=False):
    if legacy:
        return f"{adults} {_people_word(adults)}"
    parts = []
    if adults:
        parts.append(f"{adults} {_adult_word(adults)}")
    if children:
        parts.append(f"{children} {_child_word(children)}")
    return " + ".join(parts)


def _day_list(days):
    return days[0] if len(days) == 1 else f"{', '.join(days[:-1])} a {days[-1]}"


def _meals_word(count):
    return "jedlo" if count == 1 else "jedlá" if count < 5 else "jedál"


def _people_word(count):
    return "osoba" if count == 1 else "osoby" if count < 5 else "osôb"


def _days_word(count):
    return "deň" if count == 1 else "dni" if count < 5 else "dní"


# --------------------------------------------------------------- slovná zhoda
def _fold(word):
    """Slovo bez diakritiky a bez striedania ie/e — „mlieka" a „mliečna" sa
    musia stretnúť, inak by kontrola názvu odmietala správne recepty."""
    text = unicodedata.normalize("NFKD", word.casefold())
    text = "".join(letter for letter in text if not unicodedata.combining(letter))
    return text.replace("ie", "e")


def _folded_words(text):
    return [_fold(word) for word in _WORD.findall(text)]


def _shared_prefix(first, second):
    limit = min(len(first), len(second))
    index = 0
    while index < limit and first[index] == second[index]:
        index += 1
    return index


def _same_word(first, second):
    """Zámerne zhovievavé: slovenčina ohýba konce slov, nie začiatky.

    Radšej občas prepustíme podobné slovo, než by sme za správny recept
    pýtali od používateľa ďalší (platený) prepočet.
    """
    shared = _shared_prefix(first, second)
    return shared >= 3 and (shared >= 4 or min(len(first), len(second)) <= 5)


def _same_ingredient(first, second):
    """Prísnejší brat `_same_word` pre otázku „menuje postup túto surovinu?".

    Tu sa smer rizika obracia: nájdená surovina vedie ku kontrole množstva,
    teda k odmietnutiu. „Mlieko" sa preto nesmie chytiť na „mletým korením".
    """
    shared = _shared_prefix(first, second)
    return shared >= 4 or (
        shared >= 3 and min(len(first), len(second)) <= 4 and abs(len(first) - len(second)) <= 1
    )


def _mentions(words, word):
    return any(_same_word(word, other) for other in words)


_FILLER_WORDS = tuple(_fold(word) for word in NAME_FILLER)
_SERVING_BASES = tuple(_fold(word) for word in SERVING_BASES)


# ------------------------------------------------------------------ množstvá
def _package_amount(jednotka):
    """Veľkosť balenia z overenej jednotky, napr. „500 g" alebo „4×125 g"."""
    if not isinstance(jednotka, str):
        return None
    match = _PACKAGE.search(jednotka)
    if match is None:
        return None
    base, factor = UNITS[match.group(3).lower()]
    amount = Decimal(match.group(2).replace(",", ".")) * factor
    if match.group(1):
        amount *= Decimal(match.group(1))
    return (base, amount) if amount > 0 else None


_ROLE_NAME_PATTERNS = (
    ("potato", ("zemiak*", "batat*")),
    # Maslo musí byť celé slovo. „Maslová tekvica" nie je tuk.
    ("fat_addition", ("olej", "oleja", "olejom", "maslo", "masla", "maslom",
                      "margarin*", "mast", "masti", "sadlo", "sadla")),
    ("egg", ("vajc*",)),
    ("protein_main", (
        "kurac*", "morac*", "bravc*", "hovadz*", "telac*", "jahnac*", "kacac*",
        "ryba", "ryby", "rybac*", "losos*", "tuniak*", "tresk*", "pstruh*", "file", "maso",
    )),
    ("legume_dry", ("sosovic*", "fazul*", "cicer*", "hrach*")),
    ("dry_starch", ("ryz*", "cestovin*", "spaget*", "kuskus*", "bulgur*", "krup*", "polent*")),
    ("bread", ("chleb*", "peciv*", "rozok*", "zeml*", "baget*", "toast*", "tortill*")),
    ("dairy_addition", ("parmezan*", "grana padano")),
    ("sauce_liquid", ("mlek*", "smotan*", "vyvar*", "pasat*")),
    ("dairy_main", ("tvaroh*", "mozzarell*", "bryndz*", "jogurt*")),
    ("vegetable", (
        "brokolic*", "karfiol*", "mrkv*", "paradaj*", "uhork*", "paprik*", "cuket*",
        "kapust*", "spenat*", "salat*", "tekvic*", "baklazan*",
    )),
)

# Tieto názvy majú dve legitímne kuchárske úlohy. Model smie vybrať iba jednu
# z uzavretého páru; bez tvrdenia používame konzervatívny predvolený variant.
_AMBIGUOUS_NAME_ROLES = (
    (("cibul*", "cesnak*"), frozenset({"vegetable", "vegetable_addition"}),
     "vegetable_addition"),
    (("syr*", "eidam*", "gouda*", "emental*", "cheddar*"),
     frozenset({"dairy_main", "dairy_addition"}), "dairy_main"),
)

_CATEGORY_ROLES = {
    "maso": frozenset({"protein_main"}),
    "ryby": frozenset({"protein_main"}),
    "zelenina": frozenset({"vegetable", "vegetable_addition", "potato"}),
    "pecivo": frozenset({"bread"}),
    "vajcia": frozenset({"egg"}),
    "mlecne": frozenset({"dairy_main", "dairy_addition", "sauce_liquid", "fat_addition"}),
    "trvanlive": frozenset({"dry_starch", "legume_dry", "fat_addition"}),
}


def _token_matches_pattern(token, pattern):
    return token.startswith(pattern[:-1]) if pattern.endswith("*") else token == pattern


def _name_has_pattern(tokens, pattern):
    parts = pattern.split()
    if len(parts) > len(tokens):
        return False
    return any(
        all(_token_matches_pattern(tokens[start + offset], part)
            for offset, part in enumerate(parts))
        for start in range(len(tokens) - len(parts) + 1)
    )


def ingredient_role_for(name, category="", claimed_role=None, base=None):
    """Classify on the server; a model claim is only a constrained fallback.

    A known food name always wins. That prevents e.g. olive oil labelled by a
    model as starch from inheriting the much larger rice allowance.
    """
    name_tokens = _folded_words(str(name))
    for patterns, allowed_roles, default_role in _AMBIGUOUS_NAME_ROLES:
        if any(_name_has_pattern(name_tokens, pattern) for pattern in patterns):
            if claimed_role is None or claimed_role == "other":
                return default_role
            if claimed_role in allowed_roles and (
                    base is None or base in PORTION_RANGES[claimed_role]):
                return claimed_role
            raise ValueError("Rola suroviny je nekompatibilná s jej porciovou triedou.")
    for role, patterns in _ROLE_NAME_PATTERNS:
        if any(_name_has_pattern(name_tokens, pattern) for pattern in patterns):
            return role

    normalized_category = _fold(str(category)).replace(" ", "")
    category_roles = _CATEGORY_ROLES.get(normalized_category, frozenset())
    if claimed_role in INGREDIENT_ROLES and claimed_role != "other":
        allowed_units = PORTION_RANGES[claimed_role]
        if (not category_roles or claimed_role in category_roles) and (
                base is None or base in allowed_units):
            return claimed_role
    if len(category_roles) == 1:
        role = next(iter(category_roles))
        if base is None or base in PORTION_RANGES[role]:
            return role
    return "other"


def validate_portion_amount(name, category, amount, unit, claimed_role=None):
    """Return canonical role/unit/amount after conservative kitchen validation."""
    if isinstance(amount, bool) or not isinstance(amount, (int, float, Decimal)):
        raise ValueError("Pri surovine chýba množstvo na osobu/dospelú porciu.")
    try:
        amount = Decimal(str(amount))
    except InvalidOperation as error:
        raise ValueError("Pri surovine chýba množstvo na osobu/dospelú porciu.") from error
    if not amount.is_finite() or amount <= 0:
        raise ValueError("Pri surovine chýba množstvo na osobu/dospelú porciu.")
    if not isinstance(unit, str) or unit.strip().casefold() not in UNITS:
        raise ValueError("Pri surovine chýba platná jednotka množstva (g, ml alebo ks).")
    base, factor = UNITS[unit.strip().casefold()]
    amount *= factor
    role = ingredient_role_for(name, category, claimed_role, base)
    limits = PORTION_RANGES[role]
    if base not in limits:
        raise ValueError(f"Jednotka suroviny nezodpovedá porciovej triede {role}.")
    minimum, maximum = limits[base]
    if amount < minimum or amount > maximum:
        raise ValueError("Množstvo suroviny na osobu je nereálne pre jej porciovú triedu.")
    return role, base, amount


def _amount_per_adult(item, row):
    has_new = "amount_per_adult" in item
    has_old = "amount_per_person" in item
    if has_new and has_old:
        raise ValueError("Surovina má dve rôzne polia množstva.")
    amount = item.get("amount_per_adult") if has_new else item.get("amount_per_person")
    unit = item.get("unit")
    role, base, amount = validate_portion_amount(
        row["nazov"], row["kategoria"], amount, unit, item.get("ingredient_role")
    )
    return role, base, amount


def _decimal_text(value):
    text = format(value.quantize(Decimal("0.001")), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",")


def _amount_text(base, total):
    """Toľko suroviny sa v recepte naozaj použije, napísané po slovensky."""
    if base == "g" and total >= 1000:
        return f"{_decimal_text(total / 1000)} kg"
    if base == "ml" and total >= 1000:
        return f"{_decimal_text(total / 1000)} l"
    return f"{_decimal_text(total)} {base}"


_AMOUNT_IN_TEXT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(dkg|kg|dl|ml|ks|g|l)\b", re.IGNORECASE)
_BARE_NUMBER = re.compile(r"(?<![\d.,])(\d+(?:[.,]\d+)?)")
AMOUNT_TOLERANCE = Decimal("0.05")


def _amount_is_in(recipe, base, total):
    """Nájdi v postupe to isté množstvo — v ľubovoľnom zápise.

    „1200 g", „1,2 kg" aj zaokrúhlené „1,25 kg" sú to isté číslo; trvať na
    jedinom tvare by znamenalo odmietať dobré recepty pre pravopis čísla.
    """
    limit = total * AMOUNT_TOLERANCE
    for match in _AMOUNT_IN_TEXT.finditer(recipe):
        unit_base, factor = UNITS[match.group(2).lower()]
        if unit_base != base:
            continue
        if abs(Decimal(match.group(1).replace(",", ".")) * factor - total) <= limit:
            return True
    if base == "ks":
        return any(
            abs(Decimal(match.group(1).replace(",", ".")) - total) <= limit
            for match in _BARE_NUMBER.finditer(recipe)
        )
    return False


def _packages_needed(jednotka, base, total, asked):
    """Nákupný zoznam sa počíta z receptu, nie z odhadu modelu."""
    package = _package_amount(jednotka)
    if package is None or package[0] != base:
        return asked
    return max(1, int((total / package[1]).to_integral_value(rounding=ROUND_CEILING)))


# ------------------------------------------------------------------- recepty
def _cookable_steps(instructions):
    """Odmietni recept, podľa ktorého sa v kuchyni nedá postupovať."""
    if not isinstance(instructions, list) or not instructions:
        raise ValueError("Chýbajú pokyny k jedlu.")
    steps = [_text(instruction, "pokyn") for instruction in instructions]
    for step in steps:
        if len(step) < MIN_STEP_CHARS or len(step.split()) < MIN_STEP_WORDS:
            raise ValueError("Pokyn je príliš všeobecný na to, aby sa podľa neho dalo variť.")
    if len(steps) < MIN_STEPS_PER_MEAL:
        raise ValueError(f"Recept musí mať aspoň {MIN_STEPS_PER_MEAL} kroky v poradí varenia.")
    recipe = " ".join(steps)
    if not _QUANTITY.search(recipe):
        raise ValueError("Recept neuvádza konkrétne množstvá surovín.")
    if not _DURATION.search(recipe):
        raise ValueError("Recept neuvádza čas prípravy pri krokoch.")
    if _COOKS_WITH_HEAT.search(recipe) and not _HEAT_LEVEL.search(recipe):
        raise ValueError("Recept neuvádza teplotu ani intenzitu ohrevu.")
    if sum(1 for step in steps if _NUMBER.search(step)) * 2 < len(steps):
        raise ValueError("Väčšina krokov musí uvádzať konkrétne množstvo, čas alebo teplotu.")
    for step in steps:
        if _CUTS.search(step) and not _CUT_SHAPE.search(step) and not _GARNISH.search(step):
            raise ValueError("Pri krájaní chýba tvar: na kocky, na plátky, na prúžky či kolieska.")
    if not _LOOKS_DONE.search(recipe):
        raise ValueError("Recept nehovorí, ako má hotové jedlo vyzerať (do sklovita, kým nezmäkne).")
    # Posledný krok býva niekedy rada o zvyškoch; podávanie preto stačí v závere.
    if not _SERVES.search(" ".join(steps[-2:])):
        raise ValueError("Recept sa nekončí podávaním: hotové jedlo treba rozdeliť na taniere.")
    return steps


def _name_fits_recipe(name, steps):
    """Názov musí opisovať to, čo kroky naozaj urobia.

    „Kuracie prsné plátky na ryži" nesmie byť jedlo, do ktorého sa ryža
    len vmieša — presne to majiteľ v appke našiel.
    """
    step_words = _folded_words(" ".join(steps))
    for word in _WORD.findall(name):
        folded = _fold(word)
        if len(folded) < 4 or _mentions(_FILLER_WORDS, folded):
            continue
        if not _mentions(step_words, folded):
            raise ValueError(f"Názov jedla sľubuje „{word}“, ale v postupe sa nikde nepoužije.")
    finish_words = _folded_words(" ".join(steps[-2:]))
    for match in _ON_TOP_OF.finditer(name):
        folded = _fold(match.group(1))
        if _mentions(_SERVING_BASES, folded) and not _mentions(finish_words, folded):
            raise ValueError(
                f"Názov sľubuje jedlo podávané na {match.group(1)}, ale v závere postupu"
                f" sa nič také na tanier nedostane. Keď sa surovina vmiešava dovnútra,"
                f" jedlo sa musí volať inak."
            )


def _validated_household_size(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 12:
        raise ValueError("Počet osôb musí byť celé číslo od 1 do 12.")
    return value


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Chýba {field} v návrhu plánu.")
    return value.strip()


def _price(value, field):
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"Neplatná {field} v overenej ponuke.") from error
    if not amount.is_finite() or amount <= 0 or amount != amount.quantize(CENT):
        raise ValueError(f"Neplatná {field} v overenej ponuke.")
    return amount


def _format(amount):
    return format(amount.quantize(CENT), "f").replace(".", ",")


def _reject_extra(mapping, allowed):
    if not isinstance(mapping, dict) or set(mapping) - allowed:
        raise ValueError("Návrh obsahuje nepovolené obchodné údaje.")


def _model_meals(model_output, offers_by_key, frequency, pantry, adults, children):
    _reject_extra(model_output, _MODEL_TOP_LEVEL)
    meals = model_output.get("meals")
    cooking_days = cooking_days_for_frequency(frequency)
    if not isinstance(meals, list) or len(meals) != len(cooking_days):
        raise ValueError("Návrh nemá správny počet jedál.")

    portions_by_day = {day: servings_for(adults, children, frequency, day) for day in cooking_days}
    equivalents_by_day = {
        day: adult_equivalents_for(adults, children, frequency, day) for day in cooking_days
    }
    pantry_by_name = {item.casefold(): item for item in pantry}
    seen_days = set()
    seen_offers = set()
    parsed = []
    for meal in meals:
        _reject_extra(meal, _MODEL_MEAL)
        day = _text(meal.get("day"), "deň")
        if day not in DAY_ORDER or day in seen_days:
            raise ValueError("Návrh obsahuje duplicitný alebo neplatný deň.")
        if day not in portions_by_day:
            raise ValueError(f"Návrh nedodržal dni varenia: {_day_list(cooking_days)}.")
        seen_days.add(day)
        portions = portions_by_day[day]
        name = _text(meal.get("name"), "názov jedla")
        minutes = meal.get("minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or minutes <= 0:
            raise ValueError("Počet minút musí byť kladné celé číslo.")
        steps = _cookable_steps(meal.get("instructions"))
        _name_fits_recipe(name, steps)
        selected_pantry = []
        # Pri bežnom zdieľanom pláne je `pantry` prázdna zámerne. Model do
        # pantry_ingredients občas svojvoľne doplní olej či soľ; nesmie tým
        # zhodiť inak bezpečný plán ani rozhodovať, čo má človek doma. Pole
        # preto úplne ignorujeme. Pri výslovnom pláne zo špajze je zoznam
        # neprázdny a zostáva prísne overený voči skutočnému vstupu používateľa.
        if pantry_by_name:
            pantry_names = meal.get("pantry_ingredients", [])
            if not isinstance(pantry_names, list):
                raise ValueError("Neplatné suroviny zo špajze.")
            for ingredient in pantry_names:
                normalized = _text(ingredient, "surovinu zo špajze").casefold()
                if normalized not in pantry_by_name or normalized in selected_pantry:
                    raise ValueError(
                        "Návrh obsahuje neznámu alebo duplicitnú surovinu zo špajze."
                    )
                selected_pantry.append(normalized)
        items = meal.get("items")
        if not isinstance(items, list) or (not items and not selected_pantry):
            raise ValueError("Jedlo nemá vybrané ponuky ani suroviny zo špajze.")
        selected_items = []
        for item in items:
            _reject_extra(item, _MODEL_ITEM)
            offer_key = item.get("offer_key")
            quantity = item.get("quantity", 1)
            if not isinstance(offer_key, str) or offer_key not in offers_by_key:
                raise ValueError("Návrh obsahuje neznáme alebo neaktuálne offer_key.")
            if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
                raise ValueError("Množstvo musí byť kladné celé číslo.")
            if offer_key in seen_offers:
                raise ValueError("Návrh obsahuje duplicitné offer_key.")
            seen_offers.add(offer_key)
            row = offers_by_key[offer_key]
            _role, base, per_adult = _amount_per_adult(item, row)
            total = per_adult * equivalents_by_day[day]
            if base == "ks":
                total = total.to_integral_value(rounding=ROUND_CEILING)
            _steps_agree_with_amount(row["nazov"], base, total, steps)
            selected_items.append(
                (row, _packages_needed(row["jednotka"], base, total, quantity), _amount_text(base, total))
            )
        parsed.append((day, name, minutes, steps, selected_items, [pantry_by_name[item] for item in selected_pantry]))
    if seen_days != set(cooking_days):
        raise ValueError(f"Návrh nedodržal dni varenia: {_day_list(cooking_days)}.")
    return sorted(parsed, key=lambda meal: DAY_ORDER.index(meal[0]))


def _steps_agree_with_amount(nazov, base, total, steps):
    """Keď postup surovinu menuje, musí pri nej stáť to isté množstvo, aké sa kupuje.

    Keď ju nemenuje, nekontrolujeme nič — inak by značka z letáku („Rama")
    zhodila aj úplne správny recept.
    """
    recipe = " ".join(steps)
    step_words = _folded_words(recipe)
    named = any(
        _same_ingredient(word, other)
        for word in _folded_words(nazov) if len(word) >= 4
        for other in step_words
    )
    if named and not _amount_is_in(recipe, base, total):
        raise ValueError(
            f"Množstvo suroviny {nazov} v postupe nesúhlasí s nákupom ({_amount_text(base, total)})."
        )


# Rovnaký profil = rovnaký plán, takže ho stačí poskladať raz a podať všetkým.
# Aby však susedia s rovnakou domácnosťou nedostali bajt na bajt to isté menu,
# podpis sa delí na malú sadu variantov a každý z nich pýta iný smer kuchyne.
PLAN_VARIANT_HINTS = (
    "Drž sa domácej slovenskej klasiky: polievky, guláše, zemiakové a múčne jedlá.",
    "Stav na rýchle jedlá z panvice a pekáča: cestoviny, rizoto, zapekané misy.",
    "Uprednostni ľahšie a zeleninovejšie jedlá: strukoviny, wok, pečená zelenina.",
)


# Zvýš pri KAŽDEJ zmene, ktorá mení podobu vygenerovaného plánu.
# 1 = pôvodný, 2 = rozvrh dní podľa frekvencie + konkrétne recepty s dávkami,
# 3 = plán bez špajze (špajza sa dopočíta až nad nákupným zoznamom) + krátky
#     `offer_key`, teda iný katalóg ponúk v prompte.
# 4 = plný kalendár 7 dní (7/4/3) a dávky ukončené v nedeľu, bez zvyškov cez
#     hranicu týždňa.
# 5 = osobitne dospelí/deti, detská kuchárska porcia 0,65, amount_per_adult a
#     serverový porciový štandard; kalendár 7/4/3 ostáva ukončený v nedeľu.
# 6 = pri bežnom pláne špajzu neurčuje model; pri pláne zo špajze ostáva prísna.
# Zvýš aj túto verziu pri každej ďalšej zmene formátu alebo výpočtu plánu.
PLAN_ALGO_VERSION = 6


def plan_variant_for(user_id, variants):
    """Deterministicky rozdelí používateľov medzi varianty jedného podpisu."""
    if not isinstance(variants, int) or isinstance(variants, bool) or variants < 2:
        return 0
    return int(user_id) % variants


def plan_signature(week, stores, household_size, frequency, offer_keys, pantry=(),
                   pantry_driven=False, adults=None, children=None):
    """Všetko, od čoho plán závisí, v jednom kľúči — a nič iné.

    Podpis je zároveň pravidlo neplatnosti: keď sa zmení týždeň, profil alebo
    ponuková sada, zmení sa kľúč a starý plán sa už nikdy netrafí. Preto sa do
    neho dávajú `offer_keys` — obsahujú overené fakty aj týždeň, takže nový
    leták či vypršaná ponuka zdieľaný plán automaticky odstavia.

    Špajza v podpise ZÁMERNE nie je. Kým tam bola, mal každý platiaci účet
    vlastný podpis, nikdy sa netrafil do zdieľanej cache a čakal 60–120 sekúnd
    — čakali teda najdlhšie práve tí, ktorí platia. Špajza sa dopočíta až nad
    hotovým nákupným zoznamom (`apply_pantry_to_shopping_list`).

    Jediná výnimka je výslovné „navrhni jedlá z toho, čo mám doma"
    (`pantry_driven=True`). Ten plán je z podstaty osobný, preto dostane iný
    podpis — a do zdieľanej tabuľky sa neukladá vôbec.
    """
    adults, children, _legacy = _household(household_size, adults, children)
    facts = {
        # Verzia generátora. MUSÍ sa zvýšiť pri každej zmene, ktorá mení podobu
        # plánu (rozvrh dní, prompt, validácia, formát receptu). Bez toho by sa
        # po oprave kódu ďalej servírovali staré uložené plány a používateľ by
        # opravu nikdy neuvidel — presne to sa 21. 8. 2026 stalo s rozvrhom dní.
        "algo": PLAN_ALGO_VERSION,
        "portion_standard": PORTION_STANDARD_VERSION,
        "week": week,
        "stores": sorted({str(store) for store in stores}),
        "adults": adults,
        "children": children,
        "frequency": frequency,
        "offers": sorted({str(key) for key in offer_keys}),
    }
    if pantry_driven:
        facts["pantry"] = sorted(
            {item.strip().casefold() for item in map(str, pantry) if item.strip()})
    canonical = json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def offers_catalog(rows):
    """Ponuky ako samostatný blok — pre celý týždeň a rovnaké obchody rovnaký.

    Je to zďaleka najväčšia časť promptu a jediná, ktorá sa medzi používateľmi
    nelíši. Preto stojí na začiatku správy a nesmie obsahovať nič osobné:
    len tak sa dá poslať s cache_control a čítať z cache namiesto prepočítania.
    """
    offers = "\n".join(
        f"- offer_key: {row['offer_key']}; názov: {row['nazov']}; kategória: {row['kategoria'] or 'iné'}"
        for row in rows
    )
    return f"""Overené ponuky z tohtotýždňových letákov. Vyberať smieš výhradne z nich
a odkazovať sa na ne výhradne cez offer_key.

{offers}"""


EXAMPLE_PORTIONS = 4


def example_recipe(portions=EXAMPLE_PORTIONS):
    """Vzorový recept do promptu — presne ten register, ktorý od modelu chceme.

    Pravidlá samy osebe formulácie neopravia, jeden poriadne napísaný slovenský
    recept áno: povie tvar rezu, teplotu, čas, ako to má vyzerať a skončí sa
    tým, že jedlo je na tanieri. Prechádza tou istou kontrolou ako odpoveď
    modelu — stráži to test, aby sme od modelu nepýtali nemožné.
    """
    kura = _amount_text("g", Decimal(150) * portions)
    ryza = _amount_text("g", Decimal(75) * portions)
    voda = _amount_text("ml", Decimal(150) * portions)
    return {
        "name": "Kuracie prsia na ryži",
        "minutes": 35,
        "portions": portions,
        "amounts": {"kura": kura, "ryza": ryza, "voda": voda},
        "instructions": [
            f"{kura} kuracích pŕs opláchni, osuš a nakrájaj na plátky hrubé 1 cm,"
            f" potom ich z oboch strán osoľ a okoreň.",
            f"{ryza} ryže prepláchni v sitku pod studenou vodou, zalej {voda} vody"
            f" so štipkou soli a na miernom ohni ju var pod pokrievkou 12 minút,"
            f" kým sa voda nevsiakne.",
            "Na panvici rozohrej 2 lyžice oleja na stredný oheň a plátky opekaj"
            " 4 minúty z každej strany, kým nie sú zvonka zlatisté.",
            "Stiahni plameň na mierny, prilej 100 ml vody, prikry pokrievkou a nechaj"
            " mäso 5 minút dôjsť, aby ostalo šťavnaté.",
            f"Ryžu rozdeľ na {portions} taniere, navrch polož opečené plátky,"
            f" prelej ich výpekom z panvice a hneď podávaj.",
        ],
    }


def recipe_rules():
    """Ako sa píše recept — rovnaké pre každého, takže to patrí do cache.

    Je to druhá najväčšia časť promptu a nezávisí od profilu ani od špajze.
    Keby stála v osobnom chvoste, platili by sme ju pri každom prepočte znova.
    """
    example = example_recipe()
    steps = ",\n".join(f'    "{step}"' for step in example["instructions"])
    return f"""Si slovenský kuchár a skladáš jedálniček pre jednu domácnosť.
Vraciaš iba JSON v tomto tvare, nič iné:
{{"meals":[{{"day":"PO","name":"...","minutes":30,"items":[{{"offer_key":"offer_...","amount_per_adult":150,"unit":"g","quantity":1,"ingredient_role":"protein_main"}}],"pantry_ingredients":["..."],"instructions":["..."]}}]}}

MNOŽSTVÁ
- amount_per_adult = kuchárske množstvo suroviny pre jedného dospelého na jedno
  jedlo, číslo bez jednotky. Nie je to výživové ani zdravotné odporúčanie.
- ingredient_role = jedna z: {", ".join(sorted(INGREDIENT_ROLES))}. Server názov,
  kategóriu, jednotku aj bezpečný kuchársky rozsah overí; rola sama nič neprepíše.
- Príklady: Maslová tekvica 200 g = vegetable; Cibuľa 50 g alebo Cesnak 10 g
  ako aróma = vegetable_addition; Syr Eidam 30 g na posypanie = dairy_addition.
- unit = g, ml alebo ks. Nič iné (žiadna „hrsť", „balenie" ani „lyžica").
- V krokoch píš CELKOVÉ množstvo na celú dávku, teda amount_per_adult × počet
  dospelých kuchárskych ekvivalentov uvedený pri danom dni.
- Každé množstvo napíš naraz pri prvom použití suroviny, nerozdeľuj ho medzi kroky.
- quantity = koľko balení z letáku treba kúpiť.
- Základné suroviny ({", ".join(STAPLES)}) používateľ doma má — pokojne ich v krokoch
  použi a vyčísli. Nikdy ich neuvádzaj v items ani ako ponuku s cenou či zľavou.

NÁZOV JEDLA
- Názov musí opisovať presne to, čo kroky naozaj urobia. Každé slovo z názvu sa
  musí v krokoch objaviť.
- „na ryži" znamená, že v poslednom kroku ryžu rozdelíš na taniere a mäso položíš
  na ňu. Keď ryžu vmiešaš dovnútra, jedlo sa volá „s ryžou" alebo „rizoto" —
  nie „na ryži". To isté platí pre zemiaky, cestoviny aj knedľu.
- Žiadne ozdoby typu „od babky" ani „sviatočné". Iba suroviny a spôsob prípravy.

POSTUP
- Píš tak, aby podľa toho uvaril aj človek, ktorý nikdy nevaril.
- Poradie krokov: príprava surovín → rozohriatie → tepelná úprava → spojenie →
  dochutenie → podávanie.
- Pri každom krájaní povedz tvar: na kocky, na plátky, na prúžky, na kolieska, najemno.
- Pri každom ohreve povedz intenzitu alebo teplotu (stredný oheň, rúra 180 °C)
  a čas v minútach.
- Aspoň raz povedz, ako má výsledok vyzerať: do sklovita, dozlata, kým nezmäkne.
- Posledný krok musí jedlo rozdeliť na taniere a podávať. Recept sa končí na stole.
- Napíš 5 až 7 krokov, aspoň {MIN_STEPS_PER_MEAL} kroky sú minimum.
- Píš „{GOOD_STEP_EXAMPLE}", nikdy nie „{BAD_STEP_EXAMPLE}"
- Slovenčina ako v kuchárke: rozkazovací spôsob, krátke vety, žiadne prekladové obraty.

TAKTO VYZERÁ DOBRÉ JEDLO (vzor je na {example['portions']} porcie, ty rátaj s počtom porcií zo zadania nižšie):
{{"day":"PO","name":"{example['name']}","minutes":{example['minutes']},
  "items":[{{"offer_key":"<offer_key kuracích pŕs>","amount_per_adult":150,"unit":"g","quantity":1,"ingredient_role":"protein_main"}},
           {{"offer_key":"<offer_key ryže>","amount_per_adult":75,"unit":"g","quantity":1,"ingredient_role":"dry_starch"}}],
  "pantry_ingredients":[],
  "instructions":[
{steps}
  ]}}

ČO NESMIEŠ
- Nesmieš uvádzať ani meniť obchod, názov položky, jednotku, cenu, bežnú cenu,
  úsporu, zdroj ani súčty.
- Každá položka smie obsahovať iba offer_key, quantity, amount_per_adult, unit a ingredient_role.
- Suroviny zo špajze uveď výlučne v pantry_ingredients a len z ponuky používateľa.
- Každý offer_key a deň použi najviac raz. Pokyny musia byť neprázdne."""


def personal_plan_messages(rows, frequency, pantry, household_size=None, variant=0,
                           pantry_driven=False, *, adults=None, children=None):
    """Správa pre model: cachovaná predpona + osobný zvyšok.

    Do predpony patrí všetko, čo je pre celý týždeň rovnaké — ponuky aj
    pravidlá písania receptu. Osobné je len zadanie domácnosti.
    """
    return [
        {
            "type": "text",
            "text": f"{offers_catalog(rows)}\n\n{recipe_rules()}",
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": personal_plan_prompt(
                rows, frequency, pantry, household_size, variant, pantry_driven,
                adults=adults, children=children),
        },
    ]


def personal_plan_prompt(rows, frequency, pantry, household_size=None, variant=0,
                         pantry_driven=False, *, adults=None, children=None):
    """Expose only food content and opaque offer references to the model.

    Špajza sa do promptu dostane VÝHRADNE pri `pantry_driven=True`, teda po
    výslovnom „navrhni jedlá z toho, čo mám doma". Bežný plán je zdieľaný medzi
    ľuďmi s rovnakým profilom, takže by v ňom osobná špajza bola aj únikom, aj
    dôvodom, prečo by sa taký plán nedal zdieľať.
    """
    adults, children, legacy = _household(household_size, adults, children)
    style = PLAN_VARIANT_HINTS[variant % len(PLAN_VARIANT_HINTS)] if PLAN_VARIANT_HINTS else ""
    days = cooking_days_for_frequency(frequency)
    days_text = _day_list(days)
    people = _household_text(adults, children, legacy)
    batches = []
    for day in days:
        covered = days_covered_by_meal(frequency, day)
        portions = servings_for(adults, children, frequency, day)
        adult_equivalents = adult_equivalents_for(adults, children, frequency, day)
        portions_word = "porcia" if portions == 1 else "porcie" if portions < 5 else "porcií"
        batches.append(
            f"- {day}: navar {portions} {portions_word} na {covered} {_days_word(covered)};"
            f" suroviny rátaj pre {_decimal_text(adult_equivalents)} dospelej kuchárskej porcie."
        )
    batch_text = "\n".join(batches)
    pantry_text = ", ".join(str(item).strip() for item in pantry if str(item).strip())
    pantry_task = (
        f"\n\nČO MÁ POUŽÍVATEĽ DOMA (ŠPAJZA)\n{pantry_text}\n"
        "- Postav jedlá okolo týchto surovín: čo najviac ich zapoj, aby sa dokupovalo málo.\n"
        "- Použité suroviny zo špajze vymenuj v pantry_ingredients presne tak, ako sú"
        " napísané vyššie, a nikdy ich neuvádzaj v items."
        if pantry_driven and pantry_text else ""
    )
    return f"""ZADANIE TEJTO DOMÁCNOSTI
Navrhni presne {len(days)} {_meals_word(len(days))} na dni {days_text}. Vráť iba JSON, nič iné.

- Varí sa len v dňoch {days_text}. Presne tieto dni použi ako "day", každý práve raz,
  iný deň nepoužívaj.
- Jedlá sú pre {people}; množstvá aj porcie sú rozdielne podľa cooking day a spolu pokrývajú presne 7 dní.
- Dieťa približne vo veku 3–12 rokov sa pre množstvo surovín ráta ako 0,65 dospelej
  kuchárskej porcie. Tínedžera s dospelou porciou profil uvádza medzi dospelými.
  Ide o odhad na varenie a nákup, nie o výživové odporúčanie.
{batch_text}
- Dni medzi varením sú zvyšky z predchádzajúcej dávky; po nedeľnom jedle už nič
  neplánuj cez hranicu týždňa.
- Do krokov každého jedla píš amount_per_adult × počet dospelých kuchárskych
  ekvivalentov uvedený pri jeho dni.
- minutes musí byť kladný celý počet minút prípravy.
- Vyberaj výhradne z {len(rows)} overených ponúk uvedených vyššie a drž sa pravidiel nad týmto zadaním.
- Smerovanie tohto jedálnička: {style}{pantry_task}"""


def build_personal_plan(con, model_output, stores, frequency, household_size=None, pantry=(),
                        today=None, *, adults=None, children=None):
    """Validate selection content and deterministically derive all purchasable data."""
    adults, children, legacy = _household(household_size, adults, children)
    today = today or date.today()
    offers = current_verified_offers(con, stores, today)
    offers_by_key = {row["offer_key"]: row for row in offers}
    meals = _model_meals(model_output, offers_by_key, frequency, pantry, adults, children)

    plan_meals = []
    purchases = []
    total = Decimal("0")
    regular = Decimal("0")
    for day, name, minutes, instructions, selected_items, pantry_names in meals:
        covered = days_covered_by_meal(frequency, day)
        portions = servings_for(adults, children, frequency, day)
        adult_equivalents = adult_equivalents_for(adults, children, frequency, day)
        for_whom = _household_text(adults, children, legacy)
        if covered > 1:
            for_whom += f" × {covered} {_days_word(covered)}"
        ingredients = []
        doses = []
        for row, quantity, davka in selected_items:
            price = _price(row["cena"], "akciová cena") * quantity
            original = _price(row["povodna"], "bežná cena") * quantity if row["povodna"] is not None else None
            total += price
            regular += original if original is not None else price
            ingredient = {
                "offer_key": row["offer_key"], "nazov": row["nazov"], "obchod": row["obchod"],
                "jednotka": row["jednotka"], "mnozstvo": quantity,
                # Koľko sa naozaj použije v recepte — dopočítané z porcií, nie od modelu.
                "davka": davka, "cena": _format(price),
                "povodna": _format(original) if original is not None else None,
                "zlava": row["zlava"] or "",
                # Sľub „skontroluj si ich" platí len vtedy, keď zdroj ceny dôjde
                # až na obrazovku: ktorý leták, ktorá strana a dokedy cena platí.
                "source_url": row["source_url"], "source_page": row["source_page"],
                "valid_from": row["valid_from"], "valid_to": row["valid_to"],
            }
            ingredients.append(ingredient)
            purchases.append(ingredient)
            doses.append(f"{row['nazov']} – {davka}")
        ingredients.extend({"spajza": item} for item in pantry_names)
        doses.extend(f"{item} zo špajze" for item in pantry_names)
        recipe = {"min": minutes, "porcie": portions, "pre": for_whom,
                  "davky": doses, "kroky": instructions}
        if not legacy:
            recipe["domacnost"] = {"dospeli": adults, "deti": children}
            recipe["dni"] = covered
            recipe["dospely_ekvivalent"] = _decimal_text(adult_equivalents)
            recipe["poznamka"] = "Kuchársky odhad na plánovanie nákupu."
        plan_meals.append({
            "den": day, "nazov": name,
            "recept": recipe,
            "suroviny": ingredients,
        })

    grouped = {}
    for item in purchases:
        grouped.setdefault(item["obchod"], []).append({
            key: item[key] for key in (
                "offer_key", "nazov", "jednotka", "mnozstvo", "cena", "povodna", "zlava",
                "source_url", "source_page", "valid_from", "valid_to",
            )
        })
    shopping = [
        {"obchod": store, "polozky": sorted(items, key=lambda item: (item["nazov"].casefold(), item["offer_key"]))}
        for store, items in sorted(grouped.items(), key=lambda pair: STORE_ORDER.index(pair[0]))
    ]
    return {
        "tyzden": current_monday(today), "jedla": plan_meals, "nakupny_zoznam": shopping,
        "nakup_spolu": _format(total), "bezne": _format(regular),
        "usetris": _format(max(Decimal("0"), regular - total)),
    }


def cached_offer_keys(plan):
    """Return the stable offer keys a reconstructed cached plan depends on."""
    try:
        keys = [ingredient["offer_key"] for meal in plan["jedla"] for ingredient in meal["suroviny"] if "offer_key" in ingredient]
    except (KeyError, TypeError):
        return None
    if not keys or not all(isinstance(item, str) and item for item in keys):
        return None
    # Plán uložený pred skrátením kľúčov nesie dlhé kľúče. Porovnanie na
    # kanonickom (krátkom) tvare zabráni tomu, aby ľuďom po nasadení naraz
    # „zneplatnel" úplne v poriadku poskladaný jedálniček.
    return {canonical_offer_key(key) for key in keys}


def cached_plan_is_current(plan, rows):
    selected = cached_offer_keys(plan)
    if selected is None:
        return False
    return selected.issubset({canonical_offer_key(row["offer_key"]) for row in rows})


# ------------------------------------------------------- špajza nad zoznamom
# Špajza je oddelený systém. Do skladania jedálnička nevstupuje (to robí len
# výslovné „navrhni jedlá z toho, čo mám doma"); tu sa iba dopočíta nad hotovým
# nákupným zoznamom. Je to čistý Python bez volania modelu, takže sa prepočíta
# pri každom načítaní a zmena špajze je vidieť okamžite.
#
# Párovanie voľného textu na názvy z letáku je zámerne opatrné. Falošná zhoda
# znamená, že človek príde do obchodu a surovinu nemá — to je horšie než mať
# v zozname niečo, čo už doma leží. Preto sa tu radšej nespáruje, a čo sa
# spárovalo, sa používateľovi vypíše, aby to vedel zrušiť.
PANTRY_MIN_TERM = 3

# Slová, ktoré o surovine nehovoria nič: jednotky, obaly a výplň.
PANTRY_STOPWORDS = frozenset(_fold(word) for word in (
    "kg", "dkg", "ml", "dl", "ks", "kus", "kusy", "kusov", "gram", "gramy", "gramov",
    "liter", "litra", "litre", "litrov", "balenie", "balenia", "balení", "balík",
    "téglik", "tégliky", "pohár", "poháre", "konzerva", "konzervy", "vrecko", "vrecká",
    "cca", "asi", "zopár", "trochu", "kúsok", "kúsky", "ešte", "mám", "doma", "nejaké",
    "trvanlivé", "čerstvé", "čerstvý", "chladené", "mrazené", "bio", "domáce", "domáci",
))

# Slovenčina ohýba konce slov. Odseknutie pádovej koncovky spojí „ryža/ryže",
# „zemiaky/zemiakov" aj „mlieko/mlieka" — a nechá „maslo" a „mäso" oddelené.
_PANTRY_ENDINGS = ("ami", "ach", "och", "iam", "om", "ov", "mi", "ou", "ia", "ej", "im", "ym",
                   "y", "u", "e", "a", "i", "o")

# Nepravidelné tvary, ktoré odseknutie koncovky nespojí. Zoznam je zámerne
# krátky: každý riadok navyše je ďalšia príležitosť na falošnú zhodu.
_PANTRY_IRREGULAR = {}
for _canonical, _forms in (
    ("vajc", ("vajce", "vajcia", "vajec", "vajca", "vajíčko", "vajíčka", "vajíčok", "vajíčkami")),
    # „pečivo" tu zámerne NIE je: je to kategória, nie surovina, a spárovať ho
    # s konkrétnym chlebom by človeka poslalo do obchodu bez toho, čo chcel.
    ("chleb", ("chlieb", "chleba", "chleby")),
):
    for _form in _forms:
        _PANTRY_IRREGULAR[_fold(_form)] = _canonical


def _pantry_stem(folded):
    if folded in _PANTRY_IRREGULAR:
        return _PANTRY_IRREGULAR[folded]
    for ending in _PANTRY_ENDINGS:
        if folded.endswith(ending) and len(folded) - len(ending) >= PANTRY_MIN_TERM:
            return folded[:-len(ending)]
    return folded


def _pantry_terms(text):
    """Slová, ktoré o surovine naozaj niečo hovoria, zredukované na kmeň."""
    terms = []
    for word in _WORD.findall(str(text or "")):
        folded = _fold(word)
        if len(folded) < PANTRY_MIN_TERM or folded in PANTRY_STOPWORDS:
            continue
        terms.append(_pantry_stem(folded))
    return terms


def pantry_matches_offer(pantry_item, offer_name):
    """Je táto položka špajze tá istá surovina ako táto ponuka z letáku?

    Pravidlo je jednosmerné a prísne: KAŽDÉ slovo zo špajze musí sedieť na
    nejaké slovo v názve ponuky. „Kuracie prsia" sa preto nechytia na „Kuracie
    stehná" — a práve to je zmyslom, lebo taká zhoda by človeka poslala do
    obchodu bez mäsa. Opačný smer sa nekontroluje: leták si k názvu pridáva
    značku, hmotnosť aj „chladené", a to o surovine nič nemení.
    """
    wanted = _pantry_terms(pantry_item)
    if not wanted:
        return False
    available = set(_pantry_terms(offer_name))
    if not available:
        return False
    return all(term in available for term in wanted)


def _pantry_owner(pantry, offer_name):
    """Prvá položka špajze (v poradí používateľa), ktorá na ponuku sedí."""
    for item in pantry:
        text = str(item).strip()
        if text and pantry_matches_offer(text, offer_name):
            return text
    return None


def apply_pantry_to_shopping_list(plan, pantry):
    """Označ v nákupnom zozname to, čo používateľ už doma má.

    Vracia NOVÝ plán; ten pôvodný sa nesmie dotknúť, lebo je to zdieľaný objekt
    načítaný z cache a číta ho viac ľudí naraz. Jedlá ostávajú presne také, aké
    boli — špajza nikdy nepreskladá jedálniček.
    """
    pantry = [str(item).strip() for item in (pantry or []) if str(item).strip()]
    upraveny = dict(plan)
    zoznam = []
    pokryte = []
    usetrene = Decimal("0")
    claimed = set()
    exact_offer_names = {
        _fold(str(item.get("nazov") or "").strip())
        for group in plan.get("nakupny_zoznam") or []
        for item in group.get("polozky") or []
        if str(item.get("nazov") or "").strip()
    }
    for group in plan.get("nakupny_zoznam") or []:
        polozky = []
        for item in group.get("polozky") or []:
            owner = None
            nazov = item.get("nazov")
            for candidate in pantry:
                if candidate in claimed:
                    continue
                candidate_exact = _fold(candidate)
                offer_exact = _fold(str(nazov or "").strip())
                if candidate_exact in exact_offer_names and candidate_exact != offer_exact:
                    continue
                if pantry_matches_offer(candidate, nazov):
                    if item.get("mnozstvo") == 1:
                        owner = candidate
                    else:
                        # Bez množstva v špajzi nevieme potvrdiť všetky balenia.
                        # Kandidát však patrí tomuto riadku a nesmie sa neskôr
                        # nepresne priradiť k podobne pomenovanej položke.
                        claimed.add(candidate)
                    break
            oznaceny = dict(item, mas_doma=owner is not None, spajza=owner)
            polozky.append(oznaceny)
            if owner is None:
                continue
            claimed.add(owner)
            pokryte.append({
                "offer_key": item.get("offer_key"), "nazov": nazov,
                "spajza": owner, "cena": item.get("cena"),
            })
            try:
                usetrene += _price(item.get("cena"), "cena položky")
            except ValueError:
                pass
        zoznam.append(dict(group, polozky=polozky))

    try:
        spolu = _price(plan.get("nakup_spolu"), "sumu nákupu")
    except ValueError:
        spolu = usetrene
    upraveny["nakupny_zoznam"] = zoznam
    upraveny["spajza_pokryte"] = pokryte
    upraveny["spajza_usetri"] = _format(usetrene)
    upraveny["nakup_bez_spajze"] = _format(max(Decimal("0"), spolu - usetrene))
    return upraveny


# Kľúče, ktoré vznikli zo špajze konkrétneho človeka. Do zdieľaného riadku
# nesmie prísť ani jeden — odkedy podpis špajzu neobsahuje, je to jediné, čo
# bráni tomu, aby sa špajza jedného používateľa ukázala druhému.
PANTRY_PLAN_KEYS = ("spajza", "spajza_pokryte", "spajza_usetri", "nakup_bez_spajze")
PANTRY_ITEM_KEYS = ("spajza", "mas_doma")
PANTRY_DOSE_SUFFIX = "zo špajze"


def plan_without_pantry(plan):
    """Plán zbavený všetkého, čo pochádza zo špajze jedného používateľa.

    Doteraz stačilo odstrániť vrchný kľúč `spajza`, lebo špajza bola v podpise
    a zdieľaný plán tak nikdy neprešiel medzi dvoma rôznymi špajzami. Odkedy
    v podpise nie je, musí to odstrihnúť tento kód — vrátane surovín a dávok
    vnútri jedál.
    """
    if not isinstance(plan, dict):
        return plan
    ocisteny = {key: value for key, value in plan.items() if key not in PANTRY_PLAN_KEYS}

    jedla = []
    for meal in plan.get("jedla") or []:
        if not isinstance(meal, dict):
            jedla.append(meal)
            continue
        upravene = dict(meal)
        suroviny = meal.get("suroviny")
        if isinstance(suroviny, list):
            upravene["suroviny"] = [
                item for item in suroviny
                if not (isinstance(item, dict) and "spajza" in item and "offer_key" not in item)
            ]
        recept = meal.get("recept")
        if isinstance(recept, dict) and isinstance(recept.get("davky"), list):
            upravene["recept"] = dict(
                recept,
                davky=[dose for dose in recept["davky"]
                       if not str(dose).endswith(PANTRY_DOSE_SUFFIX)],
            )
        jedla.append(upravene)
    if "jedla" in ocisteny:
        ocisteny["jedla"] = jedla

    zoznam = []
    for group in plan.get("nakupny_zoznam") or []:
        if not isinstance(group, dict):
            zoznam.append(group)
            continue
        polozky = [
            {key: value for key, value in item.items() if key not in PANTRY_ITEM_KEYS}
            if isinstance(item, dict) else item
            for item in group.get("polozky") or []
        ]
        zoznam.append(dict(group, polozky=polozky))
    if "nakupny_zoznam" in ocisteny:
        ocisteny["nakupny_zoznam"] = zoznam
    return ocisteny
