"""Deterministic preparation-state validation for version-2 recipes."""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .recipe_catalog import RecipeTemplate


_TOKEN_PART = re.compile(r"^[a-z][a-z0-9_]*$")
_BONE_IN_CUT_STATES = frozenset({"cut", "diced", "cubed"})


def _equipment_key(item: str) -> str:
    if not isinstance(item, str) or not item.strip():
        raise ValueError("workflow equipment name must be non-empty text")
    folded = unicodedata.normalize("NFKD", item.casefold())
    ascii_text = "".join(
        character
        for character in folded
        if not unicodedata.combining(character)
    )
    key = re.sub(r"[^a-z0-9]+", "_", ascii_text).strip("_")
    if _TOKEN_PART.fullmatch(key) is None:
        raise ValueError(f"invalid workflow equipment resource: {item}")
    return key


def _token(token: str) -> tuple[str, str]:
    if not isinstance(token, str) or token.count(":") != 1:
        raise ValueError(f"invalid workflow token: {token!r}")
    resource, state = token.split(":")
    if (
        _TOKEN_PART.fullmatch(resource) is None
        or _TOKEN_PART.fullmatch(state) is None
    ):
        raise ValueError(f"invalid workflow token: {token!r}")
    return resource, state


def _resource_keys(recipe: RecipeTemplate) -> tuple[str, ...]:
    slot_keys = tuple(slot.key for slot in recipe.slots)
    equipment_keys = tuple(_equipment_key(item) for item in recipe.equipment)
    resources = (*slot_keys, *equipment_keys)
    if len(resources) != len(set(resources)):
        raise ValueError("duplicate workflow resource")
    return resources


def _checked_token(token: str, resources: frozenset[str]) -> tuple[str, str]:
    resource, state = _token(token)
    if resource not in resources:
        raise ValueError(f"unknown workflow resource: {resource}")
    return resource, state


def workflow_errors(recipe: RecipeTemplate) -> tuple[str, ...]:
    """Return stable workflow failures for one explicit version-2 recipe."""
    if recipe.version < 2:
        return ()

    resource_keys = _resource_keys(recipe)
    resources = frozenset(resource_keys)
    state = {slot.key: "raw" for slot in recipe.slots}
    state.update({_equipment_key(item): "free" for item in recipe.equipment})
    bone_in_slots = {
        slot.key
        for slot in recipe.slots
        if "chicken_thigh" in slot.candidates
    }
    errors: set[str] = set()

    for instruction in recipe.instructions:
        for token in instruction.requires:
            resource, expected = _checked_token(token, resources)
            if state.get(resource) != expected:
                errors.add(f"workflow_unmet_requirement:{token}")
        for token in instruction.produces:
            resource, produced = _checked_token(token, resources)
            if resource in bone_in_slots and produced in _BONE_IN_CUT_STATES:
                errors.add("incompatible_ingredient_transition")
            state[resource] = produced

    for slot in recipe.slots:
        if slot.required and state.get(slot.key) != "served":
            errors.add(f"workflow_unserved_ingredient:{slot.key}")
    return tuple(sorted(errors))
