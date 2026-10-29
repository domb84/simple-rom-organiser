"""The base selection (``library.base_selection``) is remembered: what it depends on, and that a copy never changes the memory."""

from __future__ import annotations

import dataclasses
import unittest
from dataclasses import replace

from romorg import library
from romorg.library import LibraryProfile

from tests.test_library import (AMIGA, EVERY_RULE, GAMES, ni_items, amiga_items, setUpModule, tearDownModule)  # noqa: F401  (the no-WHDLoad-list patch)

NAMES = [
    "Alpha (1990)(Acme)(Disk 1 of 2)", "Alpha (1990)(Acme)(Disk 2 of 2)",
    "Alpha (1990)(Acme)(Disk 1 of 2)[a]", "Alpha (1990)(Acme)(Disk 2 of 2)[a]",
    "Beta (1991)(Acme)(de)", "Beta (1991)(Acme)", "Beta (1992)(Acme)",
    "Gamma (1993)(Zed)(Disk 1 of 3)", "Gamma (1993)(Zed)(Disk 3 of 3)",
    "Delta (1994)(Zed)[b]", "Delta (1994)(Zed)", "Epsilon (1995)(Zed)[m]", "Zeta (1996)(Zed)(Demo)",
]
# a value for each rule that is none of the base ones: the base selection must not notice it
OTHER = {"min_rating": 7.0, "top_n": 10, "min_votes": 99, "keep_unrated": True, "rank_scope": "owned",
         "overrides": (("x", "Alpha", "exclude"),), "saved_games": "keep", "saved_overrides": (("x", "Beta", "keep"),),
         "keep_bios": False, "keep_extras": True, "convert_first": False, "delete_converted": False, "compress_7z": False,
         "prefer_saved": False, "build_whdload": False}


def view(sel: library.Selection) -> tuple:
    return (sorted((k, d.action, d.codes, d.detail, d.superseded_by, d.set_id, d.missing, d.reason, d.elsewhere) for k, d in sel.decisions.items()),
            sorted((k, None if d is None else (d.action, d.codes, d.set_id)) for k, d in sel.before_borrow.items()),
            [(cs.id, cs.dat, cs.name, cs.total, sorted(cs.slots.items()), sorted(cs.labels.items()), cs.flags, cs.cracked,
              sorted((n, sorted(b.items(), key=str)) for n, b in cs.borrowed.items())) for cs in sel.sets],
            [(x.dat, x.name, x.total, sorted(x.present.items()), x.missing, x.kept, x.covered_by) for x in sel.incomplete],
            sel.combine_alt)


def raw(items, profile):
    sel = library._select_base(items, profile, AMIGA)
    library._flawed_beside_clean(items, sel)
    return sel


class BaseSelection(unittest.TestCase):
    def setUp(self) -> None:
        library._BASES.clear()
        self.items = amiga_items(NAMES)

    def test_the_fields_the_base_reads_are_every_field_but_the_later_rules(self) -> None:
        fields = {f.name for f in dataclasses.fields(LibraryProfile)}
        self.assertEqual(fields, set(library.BASE_FIELDS) | set(OTHER))

    def test_a_later_rule_does_not_change_the_base(self) -> None:
        for profile in (EVERY_RULE, replace(EVERY_RULE, languages=(), complete_only=False)):
            want = view(raw(self.items, profile))
            for field, value in OTHER.items():
                self.assertEqual(view(raw(self.items, replace(profile, **{field: value}))), want, field)

    def test_the_copy_is_what_select_made_before(self) -> None:
        for profile in (EVERY_RULE, replace(EVERY_RULE, languages=(), borrow_other_editions=False), replace(EVERY_RULE, one_per_game=False)):
            want = view(raw(self.items, profile))
            for _ in range(2):                                   # (made, then remembered)
                self.assertEqual(view(library.base_selection(self.items, profile, AMIGA)), want)

    def test_a_change_to_the_copy_stays_out_of_the_memory(self) -> None:
        first = library.base_selection(self.items, EVERY_RULE, AMIGA)
        want = view(first)
        for d in first.decisions.values():
            d.action, d.codes, d.set_id = "excluded", ("x",), 99
        for cs in first.sets:
            cs.slots[9] = 9
            cs.borrowed[9] = {"key": 9}
        for x in first.incomplete:
            x.covered_by = "changed"
            x.present[9] = 9
        first.sets.clear()
        first.incomplete.clear()
        first.rating["x"] = 1
        self.assertEqual(view(library.base_selection(self.items, EVERY_RULE, AMIGA)), want)
        self.assertEqual(len(library._BASES), 1)

    def test_a_base_rule_or_other_files_make_another_selection(self) -> None:
        library.base_selection(self.items, EVERY_RULE, AMIGA)
        library.base_selection(self.items, replace(EVERY_RULE, languages=()), AMIGA)
        library.base_selection(self.items[:-1], EVERY_RULE, AMIGA)
        library.base_selection(self.items, replace(EVERY_RULE, top_n=5, overrides=(("x", "y", "keep"),)), AMIGA)    # (the same as the first)
        self.assertEqual(len(library._BASES), 3)

    def test_select_gives_the_same_with_and_without_the_memory(self) -> None:
        profile = replace(EVERY_RULE, overrides=((GAMES, "Beta (1992)(Acme)", "exclude"),))
        library._BASES.clear()
        cold = view(library.select(self.items, profile, AMIGA))
        warm = view(library.select(self.items, profile, AMIGA))
        self.assertEqual(cold, warm)
        self.assertEqual(len(library._BASES), 1)

    def test_only_the_newest_selections_are_kept(self) -> None:
        for n in range(library._BASES_KEPT + 3):
            library.base_selection(self.items[: len(self.items) - n], EVERY_RULE, AMIGA)
        self.assertEqual(len(library._BASES), library._BASES_KEPT)


if __name__ == "__main__":
    unittest.main()
