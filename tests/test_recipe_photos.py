"""Každý aktívny recept má fotku, ktorú appka ukáže v jedálničku."""
import re
from pathlib import Path

from app import obrazky_receptov

FOTKY = Path("app/static/recepty")


def test_every_active_recipe_has_a_photo_and_a_thumbnail():
    chybaju = [
        recept["id"]
        for recept in obrazky_receptov.nacitaj_recepty()
        if not (FOTKY / f"{recept['id']}.webp").is_file()
        or not (FOTKY / f"{recept['id']}-n.webp").is_file()
    ]
    assert not chybaju, "recepty bez fotky: " + ", ".join(chybaju)


def test_photos_stay_small_enough_for_a_phone():
    for fotka in FOTKY.glob("*.webp"):
        limit = 20_000 if fotka.stem.endswith("-n") else 160_000
        assert fotka.stat().st_size <= limit, fotka.name


def test_app_shows_the_photo_only_for_a_safe_template_id():
    html = Path("app/static/app.html").read_text(encoding="utf-8")
    assert "/static/recepty/${foto}.webp" in html
    assert "/static/recepty/${foto}-n.webp" in html
    assert re.search(r"/\^\[a-z0-9_\]\+\$/\.test\(id\)", html)
