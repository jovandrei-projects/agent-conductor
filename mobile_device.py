"""mobile_device.py - bounded Android restart + expected-dialog handling.

Conductor-owned helper for workers on a `device`-leased step (the lease
note in conductor.py's DEVICE_NOTE points here). Stdlib only.

Why it exists: an intentional app restart or in-page reload can hit the
app's unsaved-changes guard - Android WebView shows a native 'Confirm
Navigation' dialog ('Leave this Page' / 'Stay on this Page') that no
CDP/JS channel can answer, so the navigation never completes and every
automation call behind it waits forever (seen live in run
20261014-235630-apartment-play-edit, step mobile-parity-validation, whose
harness reloaded a dirty page; screenshot
data/attachments/20261016-005550-bdf92e96.png).

Two operations, both scoped to the leased serial:

  relaunch <pkg>          hard process restart: settle (so just-completed
                          saves flush - WebView DOM storage commits in
                          batches), `am force-stop`, launcher start,
                          bounded wait for foreground. The dialog dies
                          with the process, so this path can never block
                          on it - but in-memory unsaved state is gone, so
                          use only when that state is disposable
                          (controlled test data, already saved, or the app
                          persists on teardown).
  confirm-navigation      poll for the known confirmation after a reload
                          or navigation attempt. `--leave` taps 'Leave
                          this Page' (the caller asserts the dirty state
                          is disposable), `--stay` taps 'Stay on this
                          Page' (aborts the navigation). With neither
                          flag the tool refuses rather than guesses:
                          unsaved user work is never auto-discarded.
  surface                 print the current uiautomator package/text
                          breakdown - diagnostics only.

Unexpected dialogs (foreign-package modals, ANR/crash/permission
prompts) are never tapped: a uiautomator dump + screencap land under
--diag-dir and the tool exits with an actionable message naming the
owning package and visible texts.

Serial resolution: --serial, then CONDUCTOR_DEVICE_SERIAL (the conductor
lease - when it is set, a different --serial is REFUSED: other attached
devices belong to other workers or the user), then ANDROID_SERIAL.
Every adb call carries `-s <serial>` and its own timeout; every wait has
a deadline. Nothing here reboots, kills or touches the device itself.

Exit codes: 0 ok / 1 unexpected error / 2 usage / 3 unexpected dialog or
known dialog found without a decision / 4 timed out (or no dialog under
--require) / 5 adb or device unavailable.
"""
import argparse
import datetime
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ADB_TIMEOUT_S = 15.0          # one adb call never outlives this
DUMP_TIMEOUT_S = 30.0         # uiautomator dump+read pair
POLL_S = 1.0                  # uiautomator poll cadence
POST_TAP_S = 10.0             # verify a tapped dialog actually cleared
RELAUNCH_SETTLE_S = 3.0       # pre-force-stop flush window for saves
RELAUNCH_TIMEOUT_S = 45.0     # total budget: kill, start, foreground
CONFIRM_WAIT_S = 20.0         # default poll window for the dialog

# The known confirmation: Android WebView's beforeunload dialog. Both
# buttons must be clickable; body/title text counts as supporting
# evidence so an OEM that renames one button is still recognised. Match
# is on normalised text - never a blind coordinate tap.
LEAVE_TEXTS = {"leave this page"}
STAY_TEXTS = {"stay on this page"}
SUPPORT_RE = re.compile(
    r"navigate away|may not be saved|confirm navigation|"
    r"sure you want to (leave|navigate|reload)", re.I)

# Packages that legitimately overlay the app without being 'a dialog':
# status/nav bars and soft keyboards. Anything else with clickable
# content is a foreign interactive surface - unexpected.
OVERLAY_PKGS = {"com.android.systemui"}
OVERLAY_PREFIXES = (
    "com.android.inputmethod", "com.google.android.inputmethod",
    "com.google.android.apps.inputmethod", "com.samsung.android.inputmethod",
    "com.samsung.android.honeyboard", "com.gboard", "com.touchtype",
    "com.swiftkey", "com.baidu.input", "com.iflytek.inputmethod")

RC_OK = 0
RC_ERROR = 1
RC_USAGE = 2
RC_DIALOG = 3                   # unexpected dialog, or known one, no decision
RC_TIMEOUT = 4
RC_NO_DEVICE = 5


def _sleep(s: float) -> None:
    time.sleep(s)


def _now() -> float:
    return time.monotonic()


def _hidden_kwargs() -> dict:
    return ({"creationflags": subprocess.CREATE_NO_WINDOW}
            if os.name == "nt" else {})


def _adb_argv() -> list:
    """adb command prefix: CONDUCTOR_ADB wins, then PATH, then the
    standard SDK locations (same discovery order as conductor.py -
    adb is commonly absent from PATH on this machine)."""
    raw = os.environ.get("CONDUCTOR_ADB")
    if raw:
        return shlex.split(raw)
    found = shutil.which("adb")
    if found:
        return [found]
    local = os.environ.get("LOCALAPPDATA")
    for base in (os.environ.get("ANDROID_HOME"),
                 os.environ.get("ANDROID_SDK_ROOT"),
                 str(Path(local) / "Android" / "Sdk") if local else None):
        if not base:
            continue
        cand = Path(base) / "platform-tools" / (
            "adb.exe" if os.name == "nt" else "adb")
        if cand.is_file():
            return [str(cand)]
    return ["adb"]


def adb(serial: str, *args: str,
        timeout: float = ADB_TIMEOUT_S,
        binary: bool = False) -> subprocess.CompletedProcess | None:
    """One bounded adb call pinned to the leased serial; None when adb
    can't even be started or the call outlived its timeout."""
    argv = _adb_argv() + ["-s", serial] + list(args)
    try:
        kw = {} if binary else {"text": True, "encoding": "utf-8",
                                "errors": "replace"}
        return subprocess.run(argv, capture_output=True, timeout=timeout,
                              **kw, **_hidden_kwargs())
    except (OSError, subprocess.TimeoutExpired):
        return None


def resolve_serial(explicit: str | None) -> str | None:
    """Which serial this call may touch. The conductor lease
    (CONDUCTOR_DEVICE_SERIAL) is authoritative when set - a flag naming
    a different serial is refused rather than silently obeyed, because
    other attached devices belong to other workers or the user. Without
    a lease, --serial or ANDROID_SERIAL applies."""
    lease = os.environ.get("CONDUCTOR_DEVICE_SERIAL")
    if lease:
        if explicit and explicit != lease:
            raise SystemExit(
                f"refusing: --serial {explicit!r} is not the leased "
                f"device ({lease!r}) - other attached devices are not "
                "yours to drive")
        return lease
    return explicit or os.environ.get("ANDROID_SERIAL")


class Node:
    __slots__ = ("text", "desc", "package", "cls", "res_id", "clickable",
                 "bounds")

    def __init__(self, el):
        self.text = el.get("text", "")
        self.desc = el.get("content-desc", "")
        self.package = el.get("package", "")
        self.cls = el.get("class", "")
        self.res_id = el.get("resource-id", "")
        self.clickable = el.get("clickable") == "true"
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
                     el.get("bounds", ""))
        self.bounds = tuple(int(g) for g in m.groups()) if m else None

    @property
    def label(self):
        return self.text or self.desc or ""

    @property
    def center(self):
        if not self.bounds:
            return None
        x1, y1, x2, y2 = self.bounds
        return ((x1 + x2) // 2, (y1 + y2) // 2)


def _norm(s: str) -> str:
    return " ".join((s or "").split()).lower()


def parse_uiautomator(xml_text: str) -> list:
    return [Node(el) for el in ET.fromstring(xml_text).iter("node")]


def ui_dump(serial: str, adb_fn=None) -> str | None:
    """Current window hierarchy XML, or None when the dump can't be
    produced/read (transient - callers poll; a persistent failure shows
    up as a timeout, not a guessed screen)."""
    adb_fn = adb_fn or adb
    tmp = "/data/local/tmp/conductor-ui-dump.xml"
    out = adb_fn(serial, "shell", "uiautomator", "dump", tmp,
                 timeout=DUMP_TIMEOUT_S)
    if out is None or out.returncode != 0:
        return None
    read = adb_fn(serial, "shell", "cat", tmp, timeout=ADB_TIMEOUT_S)
    if read is None or read.returncode != 0 or "<hierarchy" not in (
            read.stdout or ""):
        return None
    return read.stdout


def confirm_dialog(nodes: list, pkg: str | None = None) -> dict | None:
    """The known beforeunload confirmation. A clickable 'Leave this
    Page' plus a same-owner 'Stay this Page' or supporting body text.
    pkg scopes ownership: with pkg set, a signature owned by another
    package is reported foreign, never tapped."""
    leave = [n for n in nodes
             if n.clickable and _norm(n.label) in LEAVE_TEXTS]
    stay = [n for n in nodes
            if n.clickable and _norm(n.label) in STAY_TEXTS]
    support = [n for n in nodes if SUPPORT_RE.search(n.label)]
    for lv in leave:
        owner = lv.package
        st = next((s for s in stay if s.package == owner), None)
        sup = next((s for s in support if s.package == owner), None)
        if st is None and sup is None:
            continue              # a bare 'Leave' somewhere else in the UI
        return {"leave": lv, "stay": st, "owner": owner,
                "foreign": bool(pkg and owner != pkg)}
    return None


def unexpected_surface(nodes: list, pkg: str | None) -> dict | None:
    """Foreign-package interactive content: a dialog or whole other app
    sitting where the tested app should be (ANR/crash 'android' modals,
    permission controller, Chrome/document picker, launcher). pkg=None
    means we can't partition - nothing is reported."""
    if not pkg:
        return None
    foreign = {}
    for n in nodes:
        p = n.package
        if not p or p == pkg or p in OVERLAY_PKGS or \
                p.startswith(OVERLAY_PREFIXES):
            continue
        if n.clickable or n.label:
            slot = foreign.setdefault(p, {"texts": [], "clickable": 0})
            if n.clickable:
                slot["clickable"] += 1
            lbl = n.label.strip()
            if lbl and len(slot["texts"]) < 6:
                slot["texts"].append(lbl[:60])
    foreign = {p: v for p, v in foreign.items()
               if v["clickable"] or v["texts"]}
    if foreign:
        return {"packages": foreign}
    return None


def _diag_dir(given: str | None) -> Path:
    base = Path(given) if given else Path(tempfile.gettempdir()) / \
        "conductor-device-diag" / \
        datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base.mkdir(parents=True, exist_ok=True)
    return base


def _capture_diag(serial: str, diag_dir: Path, xml_text: str | None,
                  adb_fn=None) -> list:
    """uiautomator XML + screencap into diag_dir; returns the paths so
    the failure message can name them. Best-effort: capture failure must
    never mask the real error."""
    adb_fn = adb_fn or adb
    paths = []
    if xml_text:
        p = diag_dir / "uiautomator.xml"
        try:
            p.write_text(xml_text, encoding="utf-8")
            paths.append(p)
        except OSError:
            pass
    shot = adb_fn(serial, "exec-out", "screencap", "-p",
                  timeout=20, binary=True)
    if shot is not None and shot.returncode == 0 and shot.stdout:
        p = diag_dir / "screen.png"
        try:
            p.write_bytes(shot.stdout)
            paths.append(p)
        except OSError:
            pass
    return paths


def _surface_desc(surf: dict) -> str:
    parts = []
    for p, v in sorted(surf["packages"].items()):
        txt = "; ".join(v["texts"][:4])
        parts.append(f"{p} ({v['clickable']} clickable"
                     f"{': ' + txt if txt else ''})")
    return " | ".join(parts)


def _tap(serial: str, node: Node, adb_fn=None) -> bool:
    adb_fn = adb_fn or adb
    c = node.center
    if not c:
        return False
    out = adb_fn(serial, "shell", "input", "tap", str(c[0]), str(c[1]),
                 timeout=ADB_TIMEOUT_S)
    return out is not None and out.returncode == 0


def _poll_dump(serial: str, deadline: float, adb_fn, sleep_fn, now_fn):
    """Yield (nodes, xml) snapshots until deadline; transient dump
    failures yield None nodes so the caller keeps polling."""
    while now_fn() < deadline:
        xml = ui_dump(serial, adb_fn)
        if xml is None:
            yield None, None
        else:
            try:
                yield parse_uiautomator(xml), xml
            except ET.ParseError:
                yield None, xml
        remaining = deadline - now_fn()
        if remaining > 0:
            sleep_fn(min(POLL_S, remaining))


def confirm_navigation(serial: str, pkg: str | None, action: str | None,
                       wait_s: float = CONFIRM_WAIT_S,
                       require: bool = False,
                       diag_dir: str | None = None,
                       adb_fn=None, sleep_fn=None, now_fn=None) -> int:
    """Handle the known 'Confirm Navigation' confirmation after a
    reload/navigation the caller deliberately triggered.

    action: 'leave' = the dirty state is disposable (controlled test
    data or already saved) - tap Leave; 'stay' = abort the navigation -
    tap Stay; None = make no decision - the dialog must not be answered
    for the caller, so finding it fails with instructions.

    Returns RC_OK when the dialog was handled or never appeared (unless
    require), RC_DIALOG for an unexpected surface or an undecided known
    dialog, RC_TIMEOUT when nothing resolved within wait_s."""
    adb_fn = adb_fn or adb
    sleep_fn = sleep_fn or _sleep
    now_fn = now_fn or _now
    deadline = now_fn() + max(0.0, wait_s)
    last_xml = None
    got_dump = False
    for nodes, xml in _poll_dump(serial, deadline, adb_fn, sleep_fn,
                                 now_fn):
        if xml is not None:
            last_xml = xml
        if nodes is None:
            continue
        got_dump = True
        dlg = confirm_dialog(nodes, pkg)
        if dlg and dlg["foreign"]:
            paths = _capture_diag(serial, _diag_dir(diag_dir), xml,
                                  adb_fn)
            print(f"unexpected dialog: the navigation confirmation is "
                  f"owned by {dlg['owner']!r}, not {pkg!r} - refusing to "
                  f"touch a different app's prompt. Diag: "
                  + ", ".join(str(p) for p in paths), file=sys.stderr)
            return RC_DIALOG
        if dlg:
            if action is None:
                print(
                    f"'Confirm Navigation' is up on {serial} "
                    f"('Leave this Page'/'Stay on this Page'): the app "
                    "has unsaved changes. Refusing to decide for you - "
                    "rerun with --leave only when the dirty state is "
                    "disposable (your own test data, or state already "
                    "saved), --stay to abort the navigation, or use "
                    "'relaunch' for a process restart the dialog cannot "
                    "block.", file=sys.stderr)
                return RC_DIALOG
            if action == "stay" and dlg["stay"] is None:
                # never satisfy a Stay request with the Leave button
                paths = _capture_diag(serial, _diag_dir(diag_dir), xml,
                                      adb_fn)
                print(f"'Stay on this Page' has no button on {serial} "
                      "and only Leave was matched - refusing to tap "
                      "Leave as a substitute. Diag: "
                      + ", ".join(str(p) for p in paths),
                      file=sys.stderr)
                return RC_DIALOG
            target = dlg["leave"] if action == "leave" else dlg["stay"]
            if not _tap(serial, target, adb_fn):
                print(f"tap on '{target.label}' failed on {serial}",
                      file=sys.stderr)
                return RC_NO_DEVICE
            gone_deadline = now_fn() + POST_TAP_S
            x = None
            while now_fn() < gone_deadline:
                x = ui_dump(serial, adb_fn)
                if x is not None:
                    try:
                        if confirm_dialog(parse_uiautomator(x), pkg) \
                                is None:
                            print(f"{action}: '{target.label}' tapped, "
                                  "dialog cleared")
                            return RC_OK
                    except ET.ParseError:
                        pass
                sleep_fn(min(POLL_S,
                             max(0.0, gone_deadline - now_fn())))
            paths = _capture_diag(serial, _diag_dir(diag_dir),
                                  x or last_xml, adb_fn)
            print(f"tapped '{target.label}' but the dialog is still up "
                  f"after {POST_TAP_S:.0f}s. Diag: "
                  + ", ".join(str(p) for p in paths), file=sys.stderr)
            return RC_TIMEOUT
        surf = unexpected_surface(nodes, pkg)
        if surf:
            paths = _capture_diag(serial, _diag_dir(diag_dir), xml,
                                  adb_fn)
            print(f"unexpected surface on {serial} while waiting for "
                  f"the navigation confirmation: {_surface_desc(surf)}. "
                  f"Refusing to dismiss it - inspect the capture. Diag: "
                  + ", ".join(str(p) for p in paths), file=sys.stderr)
            return RC_DIALOG
    if not got_dump:
        # never saw the screen at all - 'no dialog' would be a guess
        print(f"uiautomator never returned a usable dump on {serial} "
              f"within {wait_s:.0f}s - device offline or dump broken",
              file=sys.stderr)
        return RC_NO_DEVICE
    if require:
        print(f"no 'Confirm Navigation' dialog within {wait_s:.0f}s on "
              f"{serial}", file=sys.stderr)
        return RC_TIMEOUT
    print(f"no dialog within {wait_s:.0f}s on {serial}")
    return RC_OK


def _foreground_pkg(serial: str, adb_fn=None) -> str | None:
    """Package of the resumed activity, per dumpsys (mResumedActivity or
    topResumedActivity, then mCurrentFocus)."""
    adb_fn = adb_fn or adb
    out = adb_fn(serial, "shell", "dumpsys", "activity", "activities",
                 timeout=ADB_TIMEOUT_S)
    if out is None or out.returncode != 0:
        return None
    txt = out.stdout or ""
    for pat in (r"mResumedActivity:[^\n]*?([\w.]+)/[\w.$]+",
                r"topResumedActivity=[^\n]*?([\w.]+)/[\w.$]+"):
        m = re.search(pat, txt)
        if m:
            return m.group(1)
    win = adb_fn(serial, "shell", "dumpsys", "window",
                 timeout=ADB_TIMEOUT_S)
    if win is not None and win.returncode == 0:
        m = re.search(r"mCurrentFocus=Window\{[^}]*?\s([\w.]+)/",
                      win.stdout or "")
        if m:
            return m.group(1)
    return None


def relaunch(serial: str, pkg: str, settle_s: float = RELAUNCH_SETTLE_S,
             timeout_s: float = RELAUNCH_TIMEOUT_S,
             diag_dir: str | None = None,
             adb_fn=None, sleep_fn=None, now_fn=None) -> int:
    """Process-level app restart that the beforeunload dialog cannot
    block - `am force-stop` kills it with the process.

    settle_s runs BEFORE the kill so a just-completed save's batched
    DOM-storage write reaches disk - 'authorized restart' must still not
    eat saved work. After the start we bound a foreground wait and then
    scan once: any dialog on a fresh process is unexpected and fails
    with diagnostics, never a tap."""
    adb_fn = adb_fn or adb
    sleep_fn = sleep_fn or _sleep
    now_fn = now_fn or _now
    if settle_s > 0:
        sleep_fn(settle_s)
    out = adb_fn(serial, "shell", "am", "force-stop", pkg)
    if out is None or out.returncode != 0:
        print(f"am force-stop {pkg} failed on {serial}: "
              f"{(out.stderr if out else 'adb unreachable')}",
              file=sys.stderr)
        return RC_NO_DEVICE
    sleep_fn(1.0)
    # monkey's LAUNCHER intent needs no activity name - package-agnostic
    out = adb_fn(serial, "shell", "monkey", "-p", pkg, "-c",
                 "android.intent.category.LAUNCHER", "1")
    if out is None or out.returncode != 0:
        print(f"launcher start for {pkg} failed on {serial}: "
              f"{(out.stderr if out else 'adb unreachable')}",
              file=sys.stderr)
        return RC_NO_DEVICE
    deadline = now_fn() + max(0.0, timeout_s)
    fg = None
    while now_fn() < deadline:
        fg = _foreground_pkg(serial, adb_fn)
        if fg == pkg:
            break
        sleep_fn(min(POLL_S, deadline - now_fn()))
    else:
        print(f"{pkg} never reached foreground on {serial} within "
              f"{timeout_s:.0f}s (last foreground: {fg!r})",
              file=sys.stderr)
        return RC_TIMEOUT
    xml = ui_dump(serial, adb_fn)
    if xml is not None:
        try:
            nodes = parse_uiautomator(xml)
        except ET.ParseError:
            nodes = []
        surf = unexpected_surface(nodes, pkg)
        dlg = None if surf else confirm_dialog(nodes, pkg)
        if surf or dlg:
            paths = _capture_diag(serial, _diag_dir(diag_dir), xml,
                                  adb_fn)
            what = _surface_desc(surf) if surf else \
                f"the navigation confirmation (owner {dlg['owner']!r})"
            print(f"{pkg} is foreground on {serial} but an unexpected "
                  f"surface is on top: {what}. Diag: "
                  + ", ".join(str(p) for p in paths), file=sys.stderr)
            return RC_DIALOG
    else:
        print(f"note: post-launch uiautomator scan failed on {serial} - "
              "app is foreground but the screen wasn't inspected")
    print(f"{pkg} relaunched and foreground on {serial}")
    return RC_OK


def surface(serial: str, adb_fn=None) -> int:
    adb_fn = adb_fn or adb
    xml = ui_dump(serial, adb_fn)
    if xml is None:
        print(f"uiautomator dump failed on {serial}", file=sys.stderr)
        return RC_NO_DEVICE
    try:
        nodes = parse_uiautomator(xml)
    except ET.ParseError:
        print("uiautomator XML unparsable", file=sys.stderr)
        return RC_NO_DEVICE
    by_pkg = {}
    for n in nodes:
        s = by_pkg.setdefault(n.package or "?", {"nodes": 0,
                                                 "clickable": 0,
                                                 "texts": []})
        s["nodes"] += 1
        s["clickable"] += 1 if n.clickable else 0
        lbl = n.label.strip()
        if lbl and len(s["texts"]) < 5:
            s["texts"].append(lbl[:40])
    for p, s in sorted(by_pkg.items(),
                       key=lambda kv: -kv[1]["clickable"]):
        print(f"{p}: {s['nodes']} nodes, {s['clickable']} clickable"
              + (f" | {'; '.join(s['texts'])}" if s["texts"] else ""))
    return RC_OK


def main(argv: list | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="mobile_device.py",
        description="Bounded Android restart + expected-dialog handling "
                    "for conductor device-leased steps.")
    ap.add_argument("--serial",
                    help="adb serial (default: CONDUCTOR_DEVICE_SERIAL, "
                         "then ANDROID_SERIAL)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser(
        "relaunch",
        help="process restart (force-stop + start) - cannot be blocked "
             "by the unsaved-changes dialog; in-memory unsaved state is "
             "lost, so call only when that state is disposable")
    r.add_argument("pkg", help="app package, e.g. com.apt3d.app")
    r.add_argument("--settle-s", type=float, default=RELAUNCH_SETTLE_S,
                   help="pause before force-stop so completed saves "
                        "flush to storage (default %(default)s)")
    r.add_argument("--timeout-s", type=float,
                   default=RELAUNCH_TIMEOUT_S,
                   help="foreground-wait budget (default %(default)s)")
    r.add_argument("--diag-dir",
                   help="where to write uiautomator.xml/screen.png on "
                        "failure (default %%TEMP%%/conductor-device-diag/"
                        "<ts>)")

    c = sub.add_parser(
        "confirm-navigation",
        help="handle the 'Confirm Navigation' (Leave/Stay) dialog after "
             "a deliberate reload; never decides without a flag")
    c.add_argument("--pkg", help="app package that should own the "
                                 "dialog (a foreign owner = unexpected)")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--leave", action="store_true",
                   help="tap 'Leave this Page' - asserts the dirty state "
                        "is disposable (controlled test data or already "
                        "saved)")
    g.add_argument("--stay", action="store_true",
                   help="tap 'Stay on this Page' - aborts the navigation")
    c.add_argument("--wait-s", type=float, default=CONFIRM_WAIT_S,
                   help="poll window for the dialog (default %(default)s)")
    c.add_argument("--require", action="store_true",
                   help="fail when no dialog appears within --wait-s")
    c.add_argument("--diag-dir", help="diagnostic capture dir")

    sub.add_parser("surface",
                   help="print the current uiautomator package "
                        "breakdown (diagnostics)")

    args = ap.parse_args(argv)
    try:
        serial = resolve_serial(args.serial)
    except SystemExit as e:
        print(e, file=sys.stderr)
        return RC_USAGE
    if not serial:
        print("no device: pass --serial or set CONDUCTOR_DEVICE_SERIAL /"
              " ANDROID_SERIAL - this tool only drives the leased "
              "device", file=sys.stderr)
        return RC_USAGE

    if args.cmd == "relaunch":
        return relaunch(serial, args.pkg, settle_s=args.settle_s,
                        timeout_s=args.timeout_s,
                        diag_dir=args.diag_dir)
    if args.cmd == "confirm-navigation":
        action = "leave" if args.leave else \
            ("stay" if args.stay else None)
        return confirm_navigation(serial, args.pkg, action,
                                  wait_s=args.wait_s,
                                  require=args.require,
                                  diag_dir=args.diag_dir)
    if args.cmd == "surface":
        return surface(serial)
    return RC_USAGE


if __name__ == "__main__":
    sys.exit(main())
