import contextlib
import importlib.util
import io
import os
from pathlib import Path
import unittest
from datetime import datetime
from unittest.mock import patch


with patch.dict(os.environ, {name: "test" for name in (
    "PORTAL_SESSION", "PORTAL_CSRF", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"
)}):
    spec = importlib.util.spec_from_file_location(
        "fve_agent", Path(__file__).with_name("fve_agent.py")
    )
    agent = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agent)


class NotificationsTest(unittest.TestCase):
    def run_agent(self, hour, minute, target="DEFAULT", success=True,
                  manual=False, portal="DEFAULT", remembered="DEFAULT"):
        state = {
            "baterie_procent": 50, "baterie_w": 0, "vyroba_w": 1000,
            "spotreba_w": 500, "odber_site_w": 0,
        }
        mocks = {
            "nacist_historii": [], "ziskat_pocasi": None,
            "ziskat_ceny": {"aktualni": 2.0}, "prihlasit_se": object(),
            "ziskat_stav_fve": state, "analyzovat_nocni": None,
            "analyzovat_denni": None,
            "telegram_cti_prikazy": {"manual_activated": manual},
            "je_manual_override": manual,
            "nacist_aktualni_mod": portal,
            "nacist_aktualni_mod_ze_souboru": remembered,
            "rozhodnout": (target, "Test duvod"),
            "nastavit_mod": success, "ulozit_aktualni_mod": None,
            "ulozit_zaznam": [], "ulozit_manual_zaznam": [],
            "commitnout_historii": None, "telegram": True,
        }
        with contextlib.ExitStack() as stack:
            clock = stack.enter_context(patch.object(agent, "datetime"))
            clock.now.return_value = datetime(2026, 11, 2, hour, minute,
                                             tzinfo=agent.TZ)
            calls = {name: stack.enter_context(patch.object(
                agent, name, return_value=value
            )) for name, value in mocks.items()}
            # These tests must never contact the portal or send real messages.
            stack.enter_context(patch.object(agent.requests, "get",
                                            side_effect=AssertionError("network")))
            stack.enter_context(patch.object(agent.requests, "post",
                                            side_effect=AssertionError("network")))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            agent.main()
        return calls

    def test_old_report_windows_are_silent_without_changes(self):
        for hour, minute in ((0, 10), (0, 15), (14, 0), (14, 15)):
            with self.subTest(hour=hour, minute=minute):
                calls = self.run_agent(hour, minute)
                calls["telegram"].assert_not_called()
                calls["nastavit_mod"].assert_not_called()
                calls["commitnout_historii"].assert_called_once()

    def test_interventions_still_send_one_message_in_report_windows(self):
        for hour, minute in ((0, 15), (14, 15)):
            with self.subTest(hour=hour, minute=minute):
                calls = self.run_agent(hour, minute, target="BLOCKING_GRID_OVERFLOW")
                calls["nastavit_mod"].assert_called_once()
                calls["telegram"].assert_called_once()
                self.assertIn("Zmena modu:", calls["telegram"].call_args.args[0])

    def test_failed_intervention_is_reported(self):
        calls = self.run_agent(14, 15, target="BLOCKING_GRID_OVERFLOW", success=False)
        calls["telegram"].assert_called_once()
        self.assertIn("nepodarilo se nastavit", calls["telegram"].call_args.args[0])
        calls["ulozit_aktualni_mod"].assert_not_called()

    def test_manual_command_prevents_intervention(self):
        calls = self.run_agent(0, 15, target="BLOCKING_GRID_OVERFLOW", manual=True)
        calls["nastavit_mod"].assert_not_called()
        calls["rozhodnout"].assert_not_called()
        calls["telegram"].assert_not_called()
        calls["ulozit_manual_zaznam"].assert_called_once()

    def test_external_portal_changes_still_reported_without_writing(self):
        calls = self.run_agent(14, 15, remembered="BLOCKING_GRID_OVERFLOW")
        calls["nastavit_mod"].assert_not_called()
        calls["telegram"].assert_called_once()
        self.assertIn("mimo agenta", calls["telegram"].call_args.args[0])


if __name__ == "__main__":
    unittest.main()
