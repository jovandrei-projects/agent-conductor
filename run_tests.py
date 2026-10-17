"""Run this project's test suite. Convention: C:\\Projects\\TESTING.md

    python run_tests.py            quiet unless something fails
    python run_tests.py -v         per-test output
    python run_tests.py --quick    skip tests marked @slow / network
    python run_tests.py --jobs N   shard the suite across N parallel
                                   unittest subprocesses (each shard
                                   gets its own CONDUCTOR_PLANNER_SPOOL)

Full unittest output goes to logs/tests.log (UTF-8, rotating); the console
stays cp1252-safe via backslashreplace.
"""
import io
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 3


class _Tee(io.TextIOBase):
    """Line-buffered fan-out: unittest stream -> console + rotating log."""

    def __init__(self, mirror, logger):
        self._mirror = mirror
        self._logger = logger
        self._buf = ''

    def write(self, s):
        self._mirror.write(s)
        self._buf += s
        while '\n' in self._buf:
            line, self._buf = self._buf.split('\n', 1)
            if line.strip():
                self._logger.info(line)
        return len(s)

    def flush(self):
        self._mirror.flush()
        if self._buf.strip():
            self._logger.info(self._buf)
        self._buf = ''


def _iter_tests(suite):
    for sub in suite:
        if isinstance(sub, unittest.TestSuite):
            yield from _iter_tests(sub)
        else:
            yield sub


def _class_shards(suite, jobs):
    """Group discovered tests by TestCase class and bin-pack classes over
    `jobs` shards (LPT on test count - no runtime data needed)."""
    by_class = {}
    for t in _iter_tests(suite):
        name = f"{t.__class__.__module__}.{t.__class__.__name__}"
        by_class[name] = by_class.get(name, 0) + 1
    shards = [[] for _ in range(jobs)]
    loads = [0] * jobs
    for name, n in sorted(by_class.items(), key=lambda kv: -kv[1]):
        i = loads.index(min(loads))
        shards[i].append(name)
        loads[i] += n
    return [s for s in shards if s]


def _run_parallel(suite, jobs, verbosity, logger):
    shards = _class_shards(suite, jobs)
    env = dict(os.environ)
    procs = []
    logs = []
    for i, classes in enumerate(shards):
        env["CONDUCTOR_PLANNER_SPOOL"] = str(
            Path(tempfile.gettempdir())
            / f"agent-conductor-planner-shard-{os.getpid()}-{i}")
        log = Path(HERE) / "logs" / f"tests-shard-{i}.log"
        logs.append(log)
        lf = open(log, "w", encoding="utf-8", errors="replace")
        # cwd=tests/ so shard names like test_conductor.X import; each
        # module inserts the repo root itself for `import conductor`.
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "unittest",
             *(["-v"] if verbosity > 1 else []), *classes],
            cwd=os.path.join(HERE, "tests"), env=env,
            stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=(subprocess.CREATE_NO_WINDOW
                           if os.name == "nt" else 0)))
        procs[-1]._logfile = lf
    print(f"--jobs {jobs}: {len(shards)} shards, "
          f"{suite.countTestCases()} tests")
    bad = []
    for i, (proc, classes) in enumerate(zip(procs, shards)):
        proc.wait()
        proc._logfile.close()
        tail = logs[i].read_text(encoding="utf-8", errors="replace")
        logger.info("=== shard %d (%d classes) rc=%d ===\n%s",
                    i, len(classes), proc.returncode, tail)
        if proc.returncode:
            bad.append((i, tail))
        print(f"  shard {i}: {len(classes):3d} classes -> "
              f"{'ok' if proc.returncode == 0 else 'FAILED'}")
    for i, tail in bad:
        # replay the failing shard's summary + tracebacks to the console
        cut = tail.rfind("FAIL")
        sys.stderr.write(f"\n===== shard {i} tail =====\n"
                         + tail[max(0, cut - 4000):])
    return not bad


def main():
    if '--quick' in sys.argv:
        os.environ['QUICK_TESTS'] = '1'   # test modules read this at import
    jobs = 1
    for i, a in enumerate(sys.argv):
        if a == '--jobs':
            try:
                jobs = int(sys.argv[i + 1])
            except (IndexError, ValueError):
                jobs = os.cpu_count() or 4
        elif a.startswith('--jobs='):
            jobs = int(a.split('=', 1)[1])
    jobs = max(1, min(16, jobs))
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='backslashreplace')
        except AttributeError:
            pass

    logs = os.path.join(HERE, 'logs')
    os.makedirs(logs, exist_ok=True)
    handler = RotatingFileHandler(os.path.join(logs, 'tests.log'),
                                  maxBytes=LOG_MAX_BYTES,
                                  backupCount=LOG_BACKUPS,
                                  encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger = logging.getLogger('run_tests')
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.info('=== test run starting (python %s) ===', sys.version.split()[0])

    suite = unittest.TestLoader().discover(os.path.join(HERE, 'tests'))
    verbosity = 2 if '-v' in sys.argv else 1
    if jobs > 1:
        ok = _run_parallel(suite, jobs, verbosity, logger)
        logger.info('=== run finished: %s ===', 'OK' if ok else 'FAILURES')
        return 0 if ok else 1
    result = unittest.TextTestRunner(stream=_Tee(sys.stderr, logger),
                                     verbosity=verbosity).run(suite)
    logger.info('=== run finished: %s ===',
                'OK' if result.wasSuccessful() else 'FAILURES')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
