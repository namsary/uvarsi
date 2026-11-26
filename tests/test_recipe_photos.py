"""Každý aktívny recept má fotku, ktorú appka ukáže v jedálničku."""
import re
from pathlib import Path

from app import obrazky_receptov

FOTKY = Path("app/static/recepty")


# Nové recepty druhej vlny smú do appky ísť skôr, než majú fotku (appka
# chýbajúci obrázok skryje). Každý taký recept musí byť v tomto zozname,
# aby sa na fotku nezabudlo, a po pridaní fotky sa zo zoznamu vyškrtne.
CAKAJU_NA_FOTKU = Path("docs/research/fotky-caka.txt")


def _cakaju_na_fotku():
    if not CAKAJU_NA_FOTKU.is_file():
        return set()
    return {
        riadok.strip()
        for riadok in CAKAJU_NA_FOTKU.read_text(encoding="utf-8").splitlines()
        if riadok.strip() and not riadok.startswith("#")
    }


def _ma_fotku(recipe_id):
    return (FOTKY / f"{recipe_id}.webp").is_file() and (
        FOTKY / f"{recipe_id}-n.webp"
    ).is_file()


def test_every_active_recipe_has_a_photo_and_a_thumbnail():
    cakaju = _cakaju_na_fotku()
    chybaju = [
        recept["id"]
        for recept in obrazky_receptov.nacitaj_recepty()
        if not _ma_fotku(recept["id"]) and recept["id"] not in cakaju
    ]
    assert not chybaju, "recepty bez fotky: " + ", ".join(chybaju)


def test_photo_backlog_lists_only_new_recipes_still_without_a_photo():
    from tests.recipe_waves import wave2_ids

    cakaju = _cakaju_na_fotku()
    assert cakaju <= set(wave2_ids()), sorted(cakaju - set(wave2_ids()))
    uz_maju = sorted(recipe_id for recipe_id in cakaju if _ma_fotku(recipe_id))
    assert not uz_maju, "vyškrtni zo zoznamu, fotka už existuje: " + ", ".join(uz_maju)


def test_photos_stay_small_enough_for_a_phone():
    for fotka in FOTKY.glob("*.webp"):
        limit = 20_000 if fotka.stem.endswith("-n") else 160_000
        assert fotka.stat().st_size <= limit, fotka.name


def test_app_shows_the_photo_only_for_a_safe_template_id():
    html = Path("app/static/app.html").read_text(encoding="utf-8")
    assert "/static/recepty/${foto}.webp" in html
    assert "/static/recepty/${foto}-n.webp" in html
    assert re.search(r"/\^\[a-z0-9_\]\+\$/\.test\(id\)", html)
