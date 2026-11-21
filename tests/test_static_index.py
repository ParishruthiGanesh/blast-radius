"""The source index must classify lines the way the analysis relies on."""

import unittest

from blast.static_index import CLASSES, END, FUNC, INNER, KIND, NAMES, START, index_source

SOURCE = '''"""Module docstring."""
import os
from a import b as c

LIMIT = 3


class Box(Base):
    """Class docstring."""

    SIZE = 1
    TABLE = {
        "A": lambda x: x + 1,
        "B": helper("B"),
    }

    @property
    def area(
        self,
    ):
        if self.SIZE > LIMIT:
            return 1
        else:
            return 2

    class Inner:
        FLAG = True


def helper(name):
    def build(args):
        return name

    return build
'''


class TestStaticIndex(unittest.TestCase):
    def setUp(self):
        self.index = index_source(SOURCE)
        self.units = self.index["units"]

    def unit_at(self, line):
        found = [u for u in self.units if u[START] <= line <= u[END]]
        self.assertEqual(len(found), 1, f"line {line}: {found}")
        return found[0]

    def test_module_level(self):
        self.assertEqual(self.unit_at(1)[NAMES], ("__doc__",))
        self.assertEqual(self.unit_at(3)[NAMES], ("c",))
        unit = self.unit_at(5)
        self.assertEqual((unit[KIND], unit[NAMES], unit[CLASSES]), ("m", ("LIMIT",), ()))

    def test_class_body(self):
        self.assertEqual(self.unit_at(8)[KIND], "k")
        unit = self.unit_at(13)
        self.assertEqual((unit[KIND], unit[NAMES], unit[CLASSES]), ("c", ("TABLE",), ("Box",)))
        self.assertEqual((unit[START], unit[END]), (12, 15))
        self.assertEqual(unit[INNER], ((13, 13),))
        self.assertEqual(self.unit_at(27)[CLASSES], ("Box.Inner", "Box"))

    def test_functions(self):
        header = self.unit_at(18)
        self.assertEqual((header[KIND], header[START], header[END], header[FUNC]), ("d", 17, 20, (17, 24)))
        test = self.unit_at(21)
        self.assertEqual((test[KIND], test[START], test[END], test[FUNC]), ("f", 21, 21, (17, 24)))
        self.assertEqual(self.unit_at(22)[KIND], "f")
        self.assertEqual([u for u in self.units if u[START] <= 23 <= u[END]], [])  # a bare `else:`
        nested = self.unit_at(32)
        self.assertEqual((nested[KIND], nested[FUNC]), ("f", (31, 32)))
        self.assertIn((30, 34), self.index["functions"])


class TestEnums(unittest.TestCase):
    def test_enum_inheritance_is_followed_across_files(self):
        from blast.static_index import mark_enums

        index = {
            "a.py": index_source("from enum import Enum\n\n\nclass AutoName(Enum):\n    pass\n"),
            "b.py": index_source("class Kind(AutoName):\n    A = 1\n\n\nclass Box:\n    class Mode(helper.AutoName):\n        X = 1\n"),
        }
        mark_enums(index)
        self.assertEqual(index["a.py"]["enums"], ["AutoName"])
        self.assertEqual(index["b.py"]["enums"], ["Box.Mode", "Kind"])


if __name__ == "__main__":
    unittest.main()
