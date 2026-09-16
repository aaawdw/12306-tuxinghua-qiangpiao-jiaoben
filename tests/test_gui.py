import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path

from ticket_app.config import load_config
from ticket_app.demo import DEMO_CONFIG
from ticket_app.gui import App
from ticket_app.engine import Control


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, Path(self.folder.name) / "config.json", local=Path(self.folder.name) / "local")

    def tearDown(self):
        for callback in self.root.tk.call("after", "info"):
            self.root.after_cancel(callback)
        self.root.destroy()
        self.folder.cleanup()

    def test_form_save_load_and_priority_order(self):
        app = self.app
        app.populate(DEMO_CONFIG)
        app.vars['seatPosition'].set('靠窗优先')
        app.vars['berth'].set('中铺优先')
        app.vars['interval'].set('0.1')
        app.fast_start.set(False)
        app.train.set("K123")
        app.seat.set("硬卧")
        app.add_preference()
        last = app.preferences.get_children()[-1]
        app.preferences.selection_set(last)
        app.move_preference(-1)
        app.save()
        saved = load_config(app.config_path)
        self.assertEqual(saved["preferences"][0], {"train": "K123", "seat": "硬卧"})
        self.assertEqual(saved["passengers"], DEMO_CONFIG["passengers"])
        self.assertEqual(saved['seatPosition'], '靠窗优先')
        self.assertEqual(saved['berth'], '中铺优先')
        self.assertEqual(saved['pollIntervalMs'], 100)
        self.assertFalse(saved['fastStart'])
        self.assertEqual(app.banner.get(), "")
        original = app.config_path.read_bytes()
        app.vars["from"].set("")
        app.save()
        self.assertIn("车站", app.banner.get())
        self.assertEqual(original, app.config_path.read_bytes())

    def test_add_remove_duplicate_and_lock(self):
        app = self.app
        app.person_name.set("演练乘客")
        app.add_person()
        app.person_name.set("演练乘客")
        app.add_person()
        self.assertEqual(len(app.people.get_children()), 1)
        self.assertIn("重复", app.banner.get())
        first = app.people.get_children()[0]
        app.people.selection_set(first)
        app.lock_form(True)
        app.remove(app.people)
        self.assertEqual(len(app.people.get_children()), 1)
        self.assertTrue(app.mode_box.instate(["disabled"]))
        app.lock_form(False)
        app.remove(app.people)
        self.assertEqual(len(app.people.get_children()), 0)
        self.assertFalse(app.mode_box.instate(["disabled"]))

    def test_recovery_requires_explicit_review_and_uses_existing_action(self):
        app = self.app
        app.worker = SimpleNamespace(control=Control())
        app.lock_form(True)
        app.set_phase('recovery')
        self.assertEqual(app.login_button.cget('text'), '已核对无订单，继续')
        self.assertTrue(app.login_button.instate(['!disabled']))
        with patch('ticket_app.gui.messagebox.askyesno', return_value=False):
            app.login()
        self.assertFalse(app.worker.control.recover.is_set())
        with patch('ticket_app.gui.messagebox.askyesno', return_value=True):
            app.login()
        self.assertTrue(app.worker.control.recover.is_set())
        self.assertTrue(app.login_button.instate(['disabled']))
        app.set_phase('login')
        self.assertEqual(app.login_button.cget('text'), '已登录，继续')


if __name__ == "__main__":
    unittest.main()
