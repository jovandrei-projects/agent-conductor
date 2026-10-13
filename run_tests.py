"""Run this project's test suite. Convention: C:\\Projects\\TESTING.md

    python run_tests.py            quiet unless something fails
    python run_tests.py -v         per-test output
    python run_tests.py --quick    skip tests marked @slow / network

Full unittest output goes to logs/tests.log (UTF-8, rotating); the console
stays cp1252-safe via backslashreplace.
"""
import io
import logging
import os
import sys
import unittest
from logging.handlers import RotatingFileHandler

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


def main():
    if '--quick' in sys.argv:
        os.environ['QUICK_TESTS'] = '1'   # test modules read this at import
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
    result = unittest.TextTestRunner(stream=_Tee(sys.stderr, logger),
                                     verbosity=verbosity).run(suite)
    logger.info('=== run finished: %s ===',
                'OK' if result.wasSuccessful() else 'FAILURES')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
