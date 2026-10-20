"""Renewable SQLite leases for workers sharing one local database.

Token checks fence stale workers at journal/finalization boundaries. This is
not a cross-host lock; SQLite must remain on the same host, not a network FS.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import sqlite3
import threading
import time
from uuid import uuid4
from . import store

logger = logging.getLogger(__name__)
SCHEMA = '''CREATE TABLE IF NOT EXISTS execution_lease(
 key TEXT PRIMARY KEY, token TEXT NOT NULL, expires REAL NOT NULL)'''
current_lease = ContextVar('execution_lease', default=None)


def fence(db):
    lease = current_lease.get()
    if lease is None:
        return
    if not db.in_transaction:
        db.execute('BEGIN IMMEDIATE')
    row = db.execute('SELECT token,expires FROM execution_lease WHERE key=?', (lease.key,)).fetchone()
    if lease.lost.is_set() or not row or row[0] != lease.token or row[1] <= time.time():
        raise Busy('Stale executor cannot commit')


class Busy(RuntimeError):
    pass


class Lease:
    def __init__(self, key, ttl=120):
        self.key, self.ttl, self.token = key, ttl, uuid4().hex
        self.stop = threading.Event()
        self.lost = threading.Event()
        self.db_path = store.DB_PATH

    def _connect(self):
        # Heartbeat, checks and cleanup must address the acquired database,
        # even if another request/test reconfigures store.DB_PATH later.
        return sqlite3.connect(self.db_path, timeout=10)

    def acquire(self):
        with self._connect() as db:
            db.execute(SCHEMA)
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT expires FROM execution_lease WHERE key=?', (self.key,)).fetchone()
            if row and row[0] > time.time():
                raise Busy(self.key)
            db.execute('INSERT OR REPLACE INTO execution_lease VALUES(?,?,?)', (self.key, self.token, time.time()+self.ttl))
        self.worker = threading.Thread(target=self._renew, daemon=True, name='lease-heartbeat')
        self.worker.start()
        return self

    def _renew(self):
        while not self.stop.wait(self.ttl/3):
            try:
                with self._connect() as db:
                    changed = db.execute('UPDATE execution_lease SET expires=? WHERE key=? AND token=? AND expires>?',
                                         (time.time()+self.ttl, self.key, self.token, time.time())).rowcount
                if changed != 1:
                    self.lost.set()
                    return
            except Exception:
                self.lost.set()
                logger.exception('Lease heartbeat failed')
                return

    def check(self):
        with self._connect() as db:
            row = db.execute('SELECT token,expires FROM execution_lease WHERE key=?', (self.key,)).fetchone()
        if self.lost.is_set() or not row or row[0] != self.token or row[1] <= time.time():
            raise Busy('Execution lease lost')

    def release(self):
        self.stop.set()
        self.worker.join(timeout=2)
        with self._connect() as db:
            db.execute('DELETE FROM execution_lease WHERE key=? AND token=?', (self.key, self.token))


@contextmanager
def hold(key):
    lease = Lease(key).acquire()
    token = current_lease.set(lease)
    try:
        yield lease
    finally:
        current_lease.reset(token)
        lease.release()
