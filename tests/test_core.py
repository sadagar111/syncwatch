"""Tests for settings, the error catalog, film memory, theme, protocol checks and code health."""
import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from syncwatch import errors, theme  # noqa: E402
from syncwatch.filmstate import FilmMemory, film_key  # noqa: E402
from syncwatch.net import protocol  # noqa: E402
from syncwatch.settings import SCHEMA, SECTIONS, Settings  # noqa: E402


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "settings.json"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_schema_is_sane(self):
        for s in SCHEMA:
            self.assertIn(s.section, SECTIONS, s.key)
            self.assertTrue(s.label, s.key)
            ok, value = __import__("syncwatch.settings", fromlist=["coerce"]).coerce(s, s.default)
            self.assertTrue(ok, f"default of {s.key} is invalid")
            self.assertEqual(value, s.default, s.key)
            if s.kind == "choice":
                self.assertTrue(s.choices, s.key)

    def test_round_trip_and_unknown_keys_kept(self):
        settings = Settings(self.path)
        settings.set("subs.size_pct", 7.5)
        settings.set("profile.name", "Alex")
        settings._unknown["future.thing"] = 42
        self.assertTrue(settings.save())
        again = Settings(self.path)
        self.assertEqual(again.load(), [])
        self.assertEqual(again.get("subs.size_pct"), 7.5)
        self.assertEqual(again.get("profile.name"), "Alex")
        again.save()
        self.assertEqual(json.loads(self.path.read_text())["values"]["future.thing"], 42)

    def test_invalid_values_are_cleaned(self):
        self.path.write_text(json.dumps({"_version": 1, "values": {
            "subs.size_pct": 999, "room.max_people": "abc", "subs.color": "red", "subs.encoding": "klingon",
            "profile.name": "x" * 100, "ui.toasts": "yes"}}))
        settings = Settings(self.path)
        settings.load()
        self.assertEqual(settings.get("subs.size_pct"), 15.0)          # clamped
        self.assertEqual(settings.get("room.max_people"), 10)           # default
        self.assertEqual(settings.get("subs.color"), "#FFFFFF")
        self.assertEqual(settings.get("subs.encoding"), "auto")
        self.assertEqual(len(settings.get("profile.name")), 32)
        self.assertTrue(settings.get("ui.toasts"))
        self.assertFalse(settings.set("room.max_people", "nope"))
        self.assertTrue(settings.set("room.max_people", 99))
        self.assertEqual(settings.get("room.max_people"), 20)           # clamped

    def test_damaged_file(self):
        self.path.write_text("{ this is not json")
        settings = Settings(self.path)
        self.assertEqual(settings.load(), ["SW-106"])
        self.assertTrue((self.dir / "settings.json.damaged").exists())
        self.assertEqual(settings.get("room.policy"), "ask")

    def test_listeners_and_advanced_hint(self):
        settings = Settings(None)
        seen = []
        settings.subscribe("subs.", lambda k, v: seen.append((k, v)))
        settings.set("subs.box", True)
        settings.set("room.max_people", 6)
        self.assertEqual(seen, [("subs.box", True)])
        self.assertIn("room.max_people", settings.changed_advanced())
        settings.reset("room.max_people")
        self.assertNotIn("room.max_people", settings.changed_advanced())

    def test_languages(self):
        settings = Settings(None)
        settings.set("subs.languages", " SK, cs ,,en ")
        self.assertEqual(settings.languages("subs.languages"), ["sk", "cs", "en"])


class ErrorCatalogTests(unittest.TestCase):
    def test_every_code_used_in_the_code_exists(self):
        used = set()
        for path in list((ROOT / "syncwatch").rglob("*.py")) + [ROOT / "SyncWatch.bat"]:
            if path.exists():
                used |= set(re.findall(r"SW-\d{3}", path.read_text(encoding="utf-8", errors="replace")))
        missing = sorted(code for code in used if code not in errors.CATALOG)
        self.assertEqual(missing, [], "codes used but not in the catalog")

    def test_entries_are_complete(self):
        for code, problem in errors.CATALOG.items():
            self.assertRegex(code, r"^SW-\d{3}$")
            self.assertTrue(problem.title and problem.explanation, code)
            self.assertIn(problem.severity, (errors.ERROR, errors.WARNING, errors.INFO), code)

    def test_report_never_breaks(self):
        rep = errors.build("SW-407")  # placeholder {reason} missing: text stays readable
        self.assertIn("{reason}", rep.explanation)
        rep = errors.build("SW-999", "x")
        self.assertEqual(rep.code, errors.UNKNOWN_CODE)
        try:
            raise ValueError("boom")
        except ValueError as exc:
            rep = errors.build("SW-901", exc=exc)
        self.assertIn("ValueError: boom", rep.details)
        self.assertIn("Traceback", rep.details)

    def test_dedupe(self):
        shown = []
        errors.set_handler(shown.append)
        try:
            errors.report("SW-205", "same")
            errors.report("SW-205", "same")
            errors.report("SW-205", "different")
            errors.report("SW-502", seconds="3")  # log-only: no popup
        finally:
            errors.set_handler(None)
            errors._recent.clear()
        self.assertEqual([r.technical for r in shown], ["same", "different"])


class FilmMemoryTests(unittest.TestCase):
    def test_memory(self):
        folder = Path(tempfile.mkdtemp())
        try:
            old = folder / "positions.json"
            old.write_text(json.dumps({"film.mkv|123": 4567}))
            memory = FilmMemory(folder / "films.json")
            self.assertEqual(memory.load(old), [])
            self.assertEqual(memory.get("film.mkv|123")["pos_ms"], 4567)
            memory.update(film_key("a.mkv", 1), pos_ms=10, sub_delay_ms=-1500, subs="mkv:3", bogus=1)
            memory.save_if_dirty()
            again = FilmMemory(folder / "films.json")
            again.load()
            self.assertEqual(again.get("a.mkv|1")["sub_delay_ms"], -1500)
            self.assertNotIn("bogus", again.get("a.mkv|1"))
            (folder / "films.json").write_text("garbage")
            self.assertEqual(FilmMemory(folder / "films.json").load(), ["SW-107"])
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class ThemeTests(unittest.TestCase):
    def test_shipped_theme_loads_cleanly(self):
        loaded, problems = theme.load(ROOT / "assets")
        self.assertEqual(problems, [])
        self.assertEqual(loaded.background_image, "background.png")
        self.assertIn("rgba(", theme.stylesheet(loaded))

    def test_broken_theme_falls_back(self):
        folder = Path(tempfile.mkdtemp())
        try:
            (folder / "theme.json").write_text('{"colors": {"text": "not a colour"}, "logo": "../../evil.png"}')
            loaded, problems = theme.load(folder)
            self.assertEqual(problems, ["SW-112"])
            self.assertEqual(loaded.colors["text"], theme.DEFAULT_COLORS["text"])
            self.assertEqual(loaded.logo, "evil.png")  # paths are stripped: only files in assets/
            (folder / "theme.json").write_text("{")
            self.assertEqual(theme.load(folder)[1], ["SW-112"])
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class ProtocolTests(unittest.TestCase):
    HEAD = {"s": "0123456789abcdef", "n": 1}

    def test_valid_and_invalid(self):
        good = protocol.validate(protocol.ROOM, {"t": "state", **self.HEAD, "pos": 1000.0, "playing": True, "at": 5,
                                                 "rules": {"host_only": False}, "extra": "ignored"})
        self.assertEqual((good["pos"], type(good["pos"]), "extra" in good), (1000, int, False))
        self.assertIsNone(protocol.validate(protocol.ROOM, {"t": "newer_feature", **self.HEAD}))
        member = {"id": "A" * 43 + "=", "dh": "A" * 43 + "=", "name": "Marek"}
        self.assertEqual(protocol.validate(protocol.ROOM, {"t": "members", **self.HEAD, "no": 1, "list": [member]})
                         ["list"][0]["id"], bytes(32))
        bad_bodies = [
            {"t": "state", **self.HEAD, "pos": -5, "playing": True, "at": 1, "rules": {"host_only": False}},
            {"t": "state", **self.HEAD, "pos": 1},                                  # missing fields
            {"t": "req", **self.HEAD, "action": "format_c_drive"},
            {"t": "profile", **self.HEAD, "name": "a" * 40},
            {"t": "state", **self.HEAD, "pos": float("nan"), "playing": True, "at": 1, "rules": {"host_only": False}},
            {"t": "bye", "s": "not hex!", "n": 1},
            {"t": "bye", "s": "0123456789abcdef", "n": -1},
            {"t": "members", **self.HEAD, "no": 1, "list": [dict(member, id="short")]},
            {"t": "members", **self.HEAD, "no": 1, "list": [member] * 65},          # too many people
            {"t": "profile", **self.HEAD, "name": "x", "avatar": "a" * 300_000},
            {"t": "card", **self.HEAD},                                             # lobby message in a room envelope
        ]
        for body in bad_bodies:
            with self.assertRaises(protocol.BadMessage, msg=str(body)[:80]):
                protocol.validate(protocol.ROOM, body)

    def test_text_is_cleaned(self):
        body = protocol.validate(protocol.ROOM, {"t": "profile", **self.HEAD, "name": "Ma\nrek\x00\x1b[31m"})
        self.assertEqual(body["name"], "Marek[31m")


class CodeHealthTests(unittest.TestCase):
    def test_python_39_syntax(self):
        for path in (ROOT / "syncwatch").rglob("*.py"):
            ast.parse(path.read_text(encoding="utf-8"), str(path), feature_version=(3, 9))

    def test_logic_modules_do_not_need_qt(self):
        code = ("import sys; import syncwatch.settings, syncwatch.errors, syncwatch.subtitles.parsers, "
                "syncwatch.subtitles.mkv, syncwatch.sync.controller, syncwatch.net.relay, syncwatch.theme, "
                "syncwatch.room.session, syncwatch.room.friends, syncwatch.room.context, "
                "syncwatch.filmstate, syncwatch.log; print('PySide6' in sys.modules)")
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.strip(), "False")


if __name__ == "__main__":
    unittest.main()
