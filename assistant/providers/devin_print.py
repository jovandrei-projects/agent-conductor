"""`devin -p [-r]` provider transport - the VERIFIED baseline
(DESIGN.md sections 3.x/6.7).

One dispatch = one `devin --print --prompt-file <f> [--model uid]
[-r session] --permission-mode auto|dangerous --export <f>` child
process running in the assistant workspace dir (its own cwd-scoped
`devin list` registry, isolated from every project's sessions). The
ATIF export is the source of truth for the reply text (trailing
tool-less agent messages), the session id, the resolved model label,
and CUMULATIVE token counters - the runtime turns those into
per-exchange deltas via `usage_delta`.

Transport facts this adapter is built on (research probes 2026-10-17):
- `--model` is ignored when resuming (`-r`), so a settings change must
  spawn a fresh session - the runtime handles that, we simply never send
  `--model` on a resume (avoids the CLI warning too).
- `--permission-mode auto` hard-rejects mutation/exec/scope tools
  non-interactively while read tools still run - the provider-side half
  of the read-only capability gate. With the conversation's readonly
  flag OFF the dispatch runs `--permission-mode dangerous` instead
  (non-interactive mode can't answer confirmation prompts, so anything
  short of dangerous surfaces as 'rejected a tool call' warnings) -
  same auto-approve-all posture conductor workers run under as `bypass`.
- There is no token streaming on this transport (the ACP upgrade path is
  blocked on a devin 3000.6.2 session-db defect): progressive text comes
  from the export when it exists, else the tail of the child's stdout
  narration. PollResult.done is process-exit truth, not text heuristics.

Process-control seams are injected so tests can drive the adapter with a
fake binary and so the host can pass its own helpers (clean_devin_env,
hidden_subprocess_kwargs, kill_tree, pid_alive) without this package ever
importing conductor.
"""
import json
import re
import subprocess
import threading
import time
from pathlib import Path

from . import (PollResult, ProviderAdapter, ProviderHandle,
              register_transport)

OUT_BUF_MAX = 262_144        # stdout narration cap kept per dispatch
PARTIAL_CHARS = 4000         # progressive text budget handed to poll()


class _OutBuffer:
    """Append-only stdout sink written by the reader thread; `text_tail`
    normalizes \\r progress frames (each \\n-line keeps its LAST frame)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._parts: list[str] = []
        self._size = 0

    def feed(self, chunk: str) -> None:
        if not chunk:
            return
        with self._lock:
            self._parts.append(chunk)
            self._size += len(chunk)
            while self._size > OUT_BUF_MAX and self._parts:
                self._size -= len(self._parts.pop(0))

    def text_tail(self, n: int = PARTIAL_CHARS) -> str:
        with self._lock:
            raw = "".join(self._parts)
        lines = []
        for ln in raw.split("\n"):
            # a \r-redrawn progress line keeps only its final frame
            lines.append(ln.rsplit("\r", 1)[-1])
        return "\n".join(lines)[-n:]


class _PrintHandle(ProviderHandle):
    """ProviderHandle + the live-process plumbing the adapter needs."""

    def __init__(self, *, proc=None, out=None, **kw):
        self.proc = proc
        self.out = out
        super().__init__(**kw)


def _read_export(path) -> dict | None:
    """Export file -> parsed dict, or None (missing/mid-write/corrupt)."""
    try:
        d = json.loads(Path(path).read_text(encoding="utf-8",
                                             errors="replace"))
    except (OSError, ValueError, TypeError):
        return None
    return d if isinstance(d, dict) else None


def export_metrics(doc: dict | None) -> dict | None:
    """ATIF final_metrics -> raw cumulative counters (ATIF key names)."""
    fm = (doc or {}).get("final_metrics")
    return dict(fm) if isinstance(fm, dict) and fm else None


def export_session_id(doc: dict | None) -> str | None:
    sid = (doc or {}).get("session_id") or (doc or {}).get("session")
    return sid if isinstance(sid, str) and sid else None


def export_model_label(doc: dict | None) -> str | None:
    name = ((doc or {}).get("agent") or {}).get("model_name")
    return name if isinstance(name, str) and name else None


def export_answer(doc: dict | None) -> str:
    """The dispatch's reply: the trailing run of agent messages with no
    tool calls (same extraction as the conductor's final_report, minus
    its STATUS:/NEXT: contract stripping - a chat turn has none)."""
    texts: list[str] = []
    for s in reversed((doc or {}).get("steps") or []):
        if not isinstance(s, dict) or s.get("source") != "agent" \
                or s.get("tool_calls"):
            break
        msg = (s.get("message") or "").strip()
        if msg:
            texts.append(msg)
    return "\n\n".join(reversed(texts)).strip()


_AUTH_FAIL_RE = re.compile(
    r"not logged in|not authenticated|no credentials|unauthorized|"
    r"401\b|run `?devin auth", re.I)


class DevinPrintProvider(ProviderAdapter):
    """ProviderAdapter over `devin -p [-r]` child processes.

    Injected seams (all default to the real implementation):
    - argv: the devin command prefix (["devin"], or a test's fake binary)
    - env(): environment map for children (host passes clean_devin_env)
    - spawn: callable running Popen (host passes subprocess.Popen +
      hidden flags); tests inject a recorder
    - kill(proc): tree-kill (host passes conductor.kill_tree)
    - alive(pid): liveness probe for ADOPTED handles whose Popen object
      died with an old serve process (host passes conductor.pid_alive)
    - run_list(cwd): `devin list --format json` runner -> (rc, stdout);
      injectable so tests never need a real CLI
    - clock/sleep: test seams
    """

    def __init__(self, argv=("devin",), *, env=None, spawn=None,
                 spawn_kwargs=None, kill=None, alive=None, run_list=None,
                 clock=time.time):
        self._argv = list(argv)
        self._env = env
        self._spawn = spawn or subprocess.Popen
        self._spawn_kwargs = dict(spawn_kwargs or {})
        self._kill = kill or self._default_kill
        self._alive = alive or self._default_alive
        self._run_list = run_list or self._default_list
        self._clock = clock

    # ----------------------------------------------------------- spawn
    def argv_for(self, prompt_path, *, session_id=None, model_uid=None,
                 export_path=None, readonly=True) -> list[str]:
        cmd = list(self._argv) + ["--print", "--prompt-file",
                                  str(prompt_path)]
        if session_id:
            cmd += ["-r", str(session_id)]
            # `--model` is ignored on resume - don't emit the warning
        elif model_uid:
            cmd += ["--model", str(model_uid)]
        # Non-interactive mode can't answer tool confirmations: `auto`
        # keeps the session read-only (the structural gate); the
        # conversation's readonly-off opt-in needs `dangerous` or every
        # write/exec tool call is rejected with warnings.
        cmd += ["--permission-mode",
                "auto" if readonly else "dangerous",
                "--export", str(export_path),
                "--respect-workspace-trust", "false"]
        return cmd

    def send(self, prompt_path: str, *, session_id: str | None,
             model_uid: str, export_path: str, workspace,
             readonly: bool = True) -> ProviderHandle:
        cmd = self.argv_for(prompt_path, session_id=session_id,
                            model_uid=model_uid, export_path=export_path,
                            readonly=readonly)
        kw = dict(self._spawn_kwargs)
        env = self._env() if self._env else None
        proc = self._spawn(
            cmd, cwd=str(workspace), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            env=env, **kw)
        out = _OutBuffer()

        def drain():
            try:
                for chunk in proc.stdout:
                    out.feed(chunk)
            except (OSError, ValueError):
                pass
            finally:
                try:
                    proc.stdout.close()
                except Exception:
                    pass

        threading.Thread(target=drain, daemon=True,
                         name="devin-print-drain").start()
        return _PrintHandle(pid=proc.pid, session_id=session_id,
                            prompt_path=str(prompt_path),
                            export_path=str(export_path),
                            proc=proc, out=out)

    def attach(self, exchange: dict) -> ProviderHandle:
        """Re-create a handle for a dispatch whose Popen died with an old
        serve process (the child itself may still be alive - Popen children
        are not lifetime-bound). Poll then works off pid liveness + the
        recorded export path."""
        return _PrintHandle(pid=exchange.get("pid"),
                            session_id=exchange.get("provider_session_id"),
                            prompt_path=exchange.get("prompt_path"),
                            export_path=exchange.get("export_path"),
                            proc=None, out=None)

    def alive_pid(self, pid) -> bool:
        try:
            return bool(pid) and self._alive(int(pid))
        except (TypeError, ValueError):
            return False

    # ------------------------------------------------------------ poll
    def poll(self, handle: ProviderHandle) -> PollResult:
        res = PollResult()
        doc = (_read_export(handle.export_path)
               if handle.export_path else None)
        if doc:
            sid = export_session_id(doc)
            if sid:
                handle.session_id = sid
            res.raw_metrics = export_metrics(doc)
            res.model_label = export_model_label(doc)
            res.text = export_answer(doc)
        res.session_id = handle.session_id
        if not res.text and getattr(handle, "out", None) is not None:
            res.text = handle.out.text_tail().strip()
        proc = getattr(handle, "proc", None)
        if proc is not None:
            rc = proc.poll()
            res.done = rc is not None
            res.exit_code = rc
        else:                                # adopted handle: pid probe
            res.done = not (handle.pid and self._alive(handle.pid))
        if res.done:
            tail = (handle.out.text_tail(8000)
                    if getattr(handle, "out", None) is not None else "")
            if _AUTH_FAIL_RE.search(tail):
                res.error = ("provider is not authenticated "
                             "(devin auth status)")
            elif res.exit_code not in (0, None):
                res.error = f"provider exited {res.exit_code}"
            elif not res.text:
                res.error = "provider finished without a reply"
        return res

    # ---------------------------------------------------------- cancel
    def cancel(self, handle: ProviderHandle) -> None:
        proc = getattr(handle, "proc", None)
        if proc is not None:
            self._kill(proc)
        elif handle.pid:
            # adopted handle: no Popen to kill through - best-effort pid
            self._kill_pid(handle.pid)

    # -------------------------------------------------------- sessions
    def sessions(self, workspace) -> set[str] | None:
        rc, out = self._run_list(Path(workspace))
        if rc != 0 or not out:
            return None
        try:
            items = json.loads(out)
        except ValueError:
            return None
        ids = set()
        for s in items if isinstance(items, list) else []:
            sid = s.get("id") or s.get("session_id")
            if sid:
                ids.add(str(sid))
        return ids

    # -------------------------------------------------------- internals
    @staticmethod
    def _default_kill(proc) -> None:
        try:
            proc.kill()
        except OSError:
            pass

    @staticmethod
    def _default_kill_pid(pid: int) -> None:
        if hasattr(subprocess, "run") and _is_windows():
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True)
        else:
            try:
                import os
                os.kill(pid, 15)
            except OSError:
                pass

    @staticmethod
    def _default_alive(pid: int) -> bool:
        if _is_windows():
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, int(pid))
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == 259     # STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        import os
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _default_list(self, cwd: Path) -> tuple[int, str]:
        try:
            out = subprocess.run(
                self._argv + ["list", "--format", "json"], cwd=str(cwd),
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30)
            return out.returncode, out.stdout or ""
        except (OSError, subprocess.SubprocessError):
            return 1, ""


def _is_windows() -> bool:
    import os
    return os.name == "nt"


register_transport("cli", DevinPrintProvider)
