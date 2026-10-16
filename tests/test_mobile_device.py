"""Tests for mobile_device.py - the worker-facing bounded restart +
expected-dialog helper. All adb traffic is scripted fakes; no device,
no network.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import mobile_device as md  # noqa: E402

PKG = "com.apt3d.app"
SERIAL = "emulator-5554"

RESUMED = ("ACTIVITY MANAGER ACTIVITIES (dumpsys activity activities)\n"
           "  mResumedActivity: ActivityRecord{abc u0 " + PKG +
           "/.MainActivity t42}\n")
RESUMED_OTHER = ("ACTIVITY MANAGER ACTIVITIES\n"
                 "  mResumedActivity: ActivityRecord{abc u0 "
                 "com.google.android.apps.nexuslauncher/.NexusLauncherActivity "
                 "t9}\n")
FOCUS = ("WINDOW MANAGER WINDOWS (dumpsys window)\n"
         "  mCurrentFocus=Window{deadbeef u0 " + PKG +
         "/.MainActivity}\n")


def _node(text="", pkg=PKG, cls="android.widget.TextView", clickable=False,
          bounds="[0,0][10,10]", res=""):
    return (
        f'<node text="{text}" resource-id="{res}" class="{cls}" '
        f'package="{pkg}" content-desc="" checkable="false" '
        f'checked="false" clickable="{str(clickable).lower()}" '
        f'enabled="true" focusable="false" focused="false" '
        f'scrollable="false" long-clickable="false" password="false" '
        f'selected="false" bounds="{bounds}"/>')


def _hier(*nodes):
    return ("<?xml version='1.0' encoding='UTF-8' standalone='yes'?>"
            "<hierarchy rotation='0'>" + "".join(nodes) + "</hierarchy>")


def _chrome_nodes():
    """Status bar + nav bar: the overlay packages every dump carries."""
    return [
        _node("9:41", pkg="com.android.systemui"),
        _node(res="com.android.systemui:id/navigation_bar",
              pkg="com.android.systemui",
              cls="android.widget.FrameLayout"),
    ]


def _app_nodes():
    return [
        _node("EDIT", pkg=PKG),
        _node("canvas", pkg=PKG, cls="android.view.View"),
    ]


def _confirm_nodes(owner=PKG):
    """The Android WebView beforeunload dialog as seen in the field:
    'Confirm Navigation' + 'Leave this Page'/'Stay on this Page', owned
    by the app package itself."""
    return [
        _node("Confirm Navigation", pkg=owner),
        _node("Changes you made may not be saved. Are you sure you want "
              "to navigate away from this page?", pkg=owner),
        _node("Leave this Page", pkg=owner, cls="android.widget.Button",
              clickable=True, bounds="[150,1170][520,1260]"),
        _node("Stay on this Page", pkg=owner, cls="android.widget.Button",
              clickable=True, bounds="[560,1170][930,1260]"),
    ]


def _anr_nodes():
    """A system ANR modal: package 'android' with its own buttons."""
    return [
        _node("apt3d isn't responding", pkg="android"),
        _node("Close app", pkg="android", cls="android.widget.Button",
              clickable=True, bounds="[0,0][100,50]"),
        _node("Wait", pkg="android", cls="android.widget.Button",
              clickable=True, bounds="[100,0][200,50]"),
    ]


def _cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class FakeClock:
    """monotonic + sleep in one: every sleep advances the clock so
    bounded loops terminate deterministically."""

    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += max(0.0, s)


def adb_script(xml_seq=None, resumed=RESUMED, dump_ok=True,
               tap_rc=0):
    """adb_fn replacement: uiautomator dump/cat cycles through xml_seq
    (last element repeats), input tap records coords, force-stop/monkey/
    dumpsys/screencap canned. Returns (fn, calls)."""
    calls = []
    state = {"i": 0}
    seq = list(xml_seq or [])

    def fn(serial, *args, timeout=15.0, binary=False):
        calls.append((serial,) + tuple(args))
        key = " ".join(args)
        if not dump_ok and ("uiautomator" in key or key.startswith(
                "shell cat")):
            return None
        if "uiautomator" in key and "dump" in key:
            return _cp(0, "UI hierchary dumped to: x")
        if key.startswith("shell cat"):
            xml = seq[min(state["i"], len(seq) - 1)] if seq else ""
            state["i"] += 1
            return _cp(0, xml)
        if "screencap" in key:
            return subprocess.CompletedProcess([], 0, b"\x89PNG", b"")
        if "input tap" in key:
            return _cp(tap_rc)
        if "force-stop" in key:
            return _cp(0)
        if "monkey" in key:
            return _cp(0, "Events injected: 1")
        if "dumpsys activity" in key:
            return _cp(0, resumed)
        if "dumpsys window" in key:
            return _cp(0, FOCUS)
        return _cp(0)

    return fn, calls


class MobileDeviceTests(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock()

    def _confirm(self, fn, **kw):
        kw.setdefault("adb_fn", fn)
        kw.setdefault("sleep_fn", self.clock.sleep)
        kw.setdefault("now_fn", self.clock.now)
        return md.confirm_navigation(**kw)

    def _nodes(self, xml):
        return md.parse_uiautomator(xml)

    # --- parsing / matching -------------------------------------------

    def test_parse_nodes_fields(self):
        nodes = self._nodes(_hier(*_confirm_nodes()))
        leave = next(n for n in nodes if n.text == "Leave this Page")
        self.assertTrue(leave.clickable)
        self.assertEqual(leave.package, PKG)
        self.assertEqual(leave.center, (335, 1215))

    def test_confirm_dialog_matched_app_owned(self):
        nodes = self._nodes(_hier(*_chrome_nodes(), *_app_nodes(),
                                  *_confirm_nodes()))
        dlg = md.confirm_dialog(nodes, PKG)
        self.assertIsNotNone(dlg)
        self.assertFalse(dlg["foreign"])
        self.assertEqual(dlg["leave"].text, "Leave this Page")

    def test_confirm_dialog_needs_pair_or_support(self):
        """A bare 'Leave this Page' label with no Stay button and no
        dialog text is not the known confirmation."""
        nodes = self._nodes(_hier(
            _node("Leave this Page", pkg=PKG,
                  cls="android.widget.Button", clickable=True)))
        self.assertIsNone(md.confirm_dialog(nodes, PKG))

    def test_confirm_dialog_leave_plus_support_counts(self):
        """OEM that renamed the negative button: Leave + body text still
        matches (stay stays None so --stay refuses rather than tapping
        Leave)."""
        nodes = self._nodes(_hier(
            _node("Are you sure you want to navigate away from this "
                  "page?", pkg=PKG),
            _node("Leave this Page", pkg=PKG,
                  cls="android.widget.Button", clickable=True)))
        dlg = md.confirm_dialog(nodes, PKG)
        self.assertIsNotNone(dlg)
        self.assertIsNone(dlg["stay"])

    def test_foreign_owned_signature(self):
        nodes = self._nodes(_hier(*_confirm_nodes("com.android.chrome")))
        dlg = md.confirm_dialog(nodes, PKG)
        self.assertTrue(dlg["foreign"])

    # --- confirm-navigation command -----------------------------------

    def test_leave_taps_button_and_clears(self):
        dialog = _hier(*_chrome_nodes(), *_app_nodes(), *_confirm_nodes())
        clean = _hier(*_chrome_nodes(), *_app_nodes())
        fn, calls = adb_script(xml_seq=[dialog, clean])
        with tempfile.TemporaryDirectory() as td:
            rc = self._confirm(fn, serial=SERIAL, pkg=PKG, action="leave",
                               wait_s=5, diag_dir=td)
        self.assertEqual(rc, md.RC_OK)
        taps = [c for c in calls if "input tap" in " ".join(c[1:])]
        self.assertEqual(len(taps), 1)
        self.assertIn("335", taps[0])
        self.assertIn("1215", taps[0])

    def test_stay_taps_stay_button(self):
        dialog = _hier(*_chrome_nodes(), *_app_nodes(), *_confirm_nodes())
        clean = _hier(*_chrome_nodes(), *_app_nodes())
        fn, calls = adb_script(xml_seq=[dialog, clean])
        with tempfile.TemporaryDirectory() as td:
            rc = self._confirm(fn, serial=SERIAL, pkg=PKG, action="stay",
                               wait_s=5, diag_dir=td)
        self.assertEqual(rc, md.RC_OK)
        taps = [c for c in calls if "input tap" in " ".join(c[1:])]
        self.assertEqual(len(taps), 1)
        self.assertIn("745", taps[0])      # Stay bounds center x
        self.assertIn("1215", taps[0])

    def test_stay_without_stay_button_never_taps_leave(self):
        dialog = _hier(
            *_chrome_nodes(), *_app_nodes(),
            _node("Are you sure you want to navigate away?", pkg=PKG),
            _node("Leave this Page", pkg=PKG,
                  cls="android.widget.Button", clickable=True))
        fn, calls = adb_script(xml_seq=[dialog])
        with tempfile.TemporaryDirectory() as td:
            rc = self._confirm(fn, serial=SERIAL, pkg=PKG, action="stay",
                               wait_s=5, diag_dir=td)
            self.assertEqual(rc, md.RC_DIALOG)
            self.assertFalse(any("input tap" in " ".join(c[1:])
                                 for c in calls))
            self.assertTrue((Path(td) / "uiautomator.xml").is_file())

    def test_known_dialog_no_decision_fails_no_tap(self):
        dialog = _hier(*_chrome_nodes(), *_app_nodes(), *_confirm_nodes())
        fn, calls = adb_script(xml_seq=[dialog])
        with tempfile.TemporaryDirectory() as td:
            rc = self._confirm(fn, serial=SERIAL, pkg=PKG, action=None,
                               wait_s=5, diag_dir=td)
        self.assertEqual(rc, md.RC_DIALOG)
        self.assertFalse(any("input tap" in " ".join(c[1:])
                             for c in calls))

    def test_no_dialog_success(self):
        clean = _hier(*_chrome_nodes(), *_app_nodes())
        fn, calls = adb_script(xml_seq=[clean])
        with tempfile.TemporaryDirectory() as td:
            rc = self._confirm(fn, serial=SERIAL, pkg=PKG, action="leave",
                               wait_s=3, diag_dir=td)
        self.assertEqual(rc, md.RC_OK)
        self.assertFalse(any("input tap" in " ".join(c[1:])
                             for c in calls))

    def test_no_dialog_require_times_out(self):
        clean = _hier(*_chrome_nodes(), *_app_nodes())
        fn, _ = adb_script(xml_seq=[clean])
        with tempfile.TemporaryDirectory() as td:
            rc = self._confirm(fn, serial=SERIAL, pkg=PKG, action="leave",
                               wait_s=3, require=True, diag_dir=td)
        self.assertEqual(rc, md.RC_TIMEOUT)

    def test_foreign_owned_dialog_is_unexpected(self):
        """Chrome's own beforeunload on top of our app is not ours to
        answer: scoped to --pkg it counts as an unexpected dialog."""
        foreign = _hier(*_chrome_nodes(), *_app_nodes(),
                        *_confirm_nodes("com.android.chrome"))
        fn, calls = adb_script(xml_seq=[foreign])
        with tempfile.TemporaryDirectory() as td:
            rc = self._confirm(fn, serial=SERIAL, pkg=PKG, action="leave",
                               wait_s=5, diag_dir=td)
            self.assertEqual(rc, md.RC_DIALOG)
            self.assertFalse(any("input tap" in " ".join(c[1:])
                                 for c in calls))
            self.assertTrue((Path(td) / "uiautomator.xml").is_file())
            self.assertTrue((Path(td) / "screen.png").is_file())

    def test_unexpected_dialog_captured(self):
        """ANR modal (package 'android') while polling for the known
        confirmation: refuse + capture, never tap."""
        anr = _hier(*_chrome_nodes(), *_app_nodes(), *_anr_nodes())
        fn, calls = adb_script(xml_seq=[anr])
        with tempfile.TemporaryDirectory() as td:
            rc = self._confirm(fn, serial=SERIAL, pkg=PKG, action="leave",
                               wait_s=5, diag_dir=td)
            self.assertEqual(rc, md.RC_DIALOG)
            self.assertFalse(any("input tap" in " ".join(c[1:])
                                 for c in calls))
            self.assertTrue((Path(td) / "uiautomator.xml").is_file())
            self.assertTrue((Path(td) / "screen.png").is_file())

    def test_overlay_packages_not_unexpected(self):
        """SystemUI chrome and an IME window must not read as dialogs."""
        nodes = self._nodes(_hier(
            *_chrome_nodes(), *_app_nodes(),
            _node("q", pkg="com.google.android.inputmethod.latin",
                  clickable=True)))
        self.assertIsNone(md.unexpected_surface(nodes, PKG))

    def test_unexpected_surface_needs_pkg(self):
        nodes = self._nodes(_hier(*_anr_nodes()))
        self.assertIsNone(md.unexpected_surface(nodes, None))

    def test_timeout_bounded_on_silence(self):
        """Dump calls keep failing: bounded poll, then RC_NO_DEVICE -
        'no dialog' is never reported for a screen we couldn't read."""
        fn, _ = adb_script(dump_ok=False)
        with tempfile.TemporaryDirectory() as td:
            rc = self._confirm(fn, serial=SERIAL, pkg=PKG, action="leave",
                               wait_s=4, diag_dir=td)
        self.assertEqual(rc, md.RC_NO_DEVICE)
        # bounded: clock advanced only ~wait_s + poll granularity
        self.assertLessEqual(self.clock.t, 6)

    def test_tap_failure_is_device_error(self):
        dialog = _hier(*_chrome_nodes(), *_app_nodes(), *_confirm_nodes())
        fn, _ = adb_script(xml_seq=[dialog], tap_rc=1)
        with tempfile.TemporaryDirectory() as td:
            rc = self._confirm(fn, serial=SERIAL, pkg=PKG, action="leave",
                               wait_s=5, diag_dir=td)
        self.assertEqual(rc, md.RC_NO_DEVICE)

    def test_dialog_that_wont_clear_times_out(self):
        dialog = _hier(*_chrome_nodes(), *_app_nodes(), *_confirm_nodes())
        fn, _ = adb_script(xml_seq=[dialog])       # never clears
        with tempfile.TemporaryDirectory() as td:
            rc = self._confirm(fn, serial=SERIAL, pkg=PKG, action="leave",
                               wait_s=5, diag_dir=td)
            self.assertEqual(rc, md.RC_TIMEOUT)
            self.assertTrue((Path(td) / "uiautomator.xml").is_file())

    # --- relaunch ------------------------------------------------------

    def test_relaunch_happy_path(self):
        clean = _hier(*_chrome_nodes(), *_app_nodes())
        fn, calls = adb_script(xml_seq=[clean])
        with tempfile.TemporaryDirectory() as td:
            rc = md.relaunch(SERIAL, PKG, settle_s=3, timeout_s=10,
                             diag_dir=td, adb_fn=fn,
                             sleep_fn=self.clock.sleep,
                             now_fn=self.clock.now)
        self.assertEqual(rc, md.RC_OK)
        keys = [" ".join(c[1:]) for c in calls]
        fs = next(i for i, k in enumerate(keys) if "force-stop" in k)
        mk = next(i for i, k in enumerate(keys) if "monkey" in k)
        self.assertLess(fs, mk)
        self.assertIn("am force-stop " + PKG, keys[fs])
        # settle ran before the kill so batched saves flush
        self.assertEqual(self.clock.slept[0], 3)

    def test_relaunch_uses_launcher_intent(self):
        clean = _hier(*_chrome_nodes(), *_app_nodes())
        fn, calls = adb_script(xml_seq=[clean])
        with tempfile.TemporaryDirectory() as td:
            md.relaunch(SERIAL, PKG, settle_s=0, timeout_s=10,
                        diag_dir=td, adb_fn=fn, sleep_fn=self.clock.sleep,
                        now_fn=self.clock.now)
        self.assertTrue(any("LAUNCHER" in " ".join(c[1:]) for c in calls))

    def test_relaunch_unexpected_dialog_after_start(self):
        anr = _hier(*_chrome_nodes(), *_app_nodes(), *_anr_nodes())
        fn, _ = adb_script(xml_seq=[anr])
        with tempfile.TemporaryDirectory() as td:
            rc = md.relaunch(SERIAL, PKG, settle_s=0, timeout_s=10,
                             diag_dir=td, adb_fn=fn,
                             sleep_fn=self.clock.sleep,
                             now_fn=self.clock.now)
            self.assertEqual(rc, md.RC_DIALOG)
            self.assertTrue((Path(td) / "uiautomator.xml").is_file())

    def test_relaunch_never_foreground_times_out(self):
        clean = _hier(*_chrome_nodes())
        fn, _ = adb_script(xml_seq=[clean], resumed=RESUMED_OTHER)
        with tempfile.TemporaryDirectory() as td:
            rc = md.relaunch(SERIAL, PKG, settle_s=0, timeout_s=5,
                             diag_dir=td, adb_fn=fn,
                             sleep_fn=self.clock.sleep,
                             now_fn=self.clock.now)
        self.assertEqual(rc, md.RC_TIMEOUT)
        self.assertLessEqual(self.clock.t, 8)

    def test_relaunch_force_stop_failure(self):
        def fn(serial, *args, timeout=15.0, binary=False):
            return _cp(1, "", "cmd: Can't force stop")
        with tempfile.TemporaryDirectory() as td:
            rc = md.relaunch(SERIAL, PKG, settle_s=0, timeout_s=5,
                             diag_dir=td, adb_fn=fn,
                             sleep_fn=self.clock.sleep,
                             now_fn=self.clock.now)
        self.assertEqual(rc, md.RC_NO_DEVICE)

    # --- serial ownership ----------------------------------------------

    def test_serial_from_lease_env(self):
        with mock.patch.dict(os.environ,
                             {"CONDUCTOR_DEVICE_SERIAL": SERIAL}):
            self.assertEqual(md.resolve_serial(None), SERIAL)
            self.assertEqual(md.resolve_serial(SERIAL), SERIAL)

    def test_foreign_serial_refused_under_lease(self):
        with mock.patch.dict(os.environ,
                             {"CONDUCTOR_DEVICE_SERIAL": SERIAL}):
            with self.assertRaises(SystemExit):
                md.resolve_serial("RFGL11NN0QV")

    def test_serial_flag_and_android_serial_fallback(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CONDUCTOR_DEVICE_SERIAL", "ANDROID_SERIAL")}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(md.resolve_serial("X1"), "X1")
        env["ANDROID_SERIAL"] = "Y2"
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(md.resolve_serial(None), "Y2")

    def test_no_serial_is_usage_error(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("CONDUCTOR_DEVICE_SERIAL", "ANDROID_SERIAL")}
        with mock.patch.dict(os.environ, env, clear=True):
            rc = md.main(["confirm-navigation", "--pkg", PKG, "--leave"])
        self.assertEqual(rc, md.RC_USAGE)

    def test_every_adb_call_scoped_to_serial(self):
        dialog = _hier(*_chrome_nodes(), *_app_nodes(), *_confirm_nodes())
        clean = _hier(*_chrome_nodes(), *_app_nodes())
        fn, calls = adb_script(xml_seq=[dialog, clean])
        with tempfile.TemporaryDirectory() as td:
            self._confirm(fn, serial=SERIAL, pkg=PKG, action="leave",
                          wait_s=5, diag_dir=td)
        self.assertTrue(calls)
        self.assertTrue(all(c[0] == SERIAL for c in calls))

    # --- CLI -------------------------------------------------------------

    def test_cli_confirm_leave(self):
        dialog = _hier(*_chrome_nodes(), *_app_nodes(), *_confirm_nodes())
        clean = _hier(*_chrome_nodes(), *_app_nodes())
        fn, _ = adb_script(xml_seq=[dialog, clean])
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(md, "adb", fn), \
                    mock.patch.object(md, "_sleep",
                                      self.clock.sleep), \
                    mock.patch.object(md, "_now", self.clock.now):
                rc = md.main(["--serial", SERIAL, "confirm-navigation",
                              "--pkg", PKG, "--leave", "--wait-s", "5",
                              "--diag-dir", td])
        self.assertEqual(rc, md.RC_OK)

    def test_cli_relaunch(self):
        clean = _hier(*_chrome_nodes(), *_app_nodes())
        fn, _ = adb_script(xml_seq=[clean])
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(md, "adb", fn), \
                    mock.patch.object(md, "_sleep",
                                      self.clock.sleep), \
                    mock.patch.object(md, "_now", self.clock.now):
                rc = md.main(["--serial", SERIAL, "relaunch", PKG,
                              "--settle-s", "0", "--timeout-s", "5",
                              "--diag-dir", td])
        self.assertEqual(rc, md.RC_OK)


if __name__ == "__main__":
    unittest.main()
