"""Local SQLite checkpoints and OS-owned lock; no remote service."""
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def home_dir(value=None):
    home = Path(value or os.environ.get('WB_PURE_HOME') or Path.home() / '.wb-pure').expanduser().resolve()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    return home


@contextmanager
def lock(home):
    # ponytail: one writer per local store directory; split directories for multiple stores.
    with open(home / 'worker.lock', 'a+b') as stream:
        if os.name == 'nt':
            import msvcrt
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise ValueError('another_local_operation_is_running') from None
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('another_local_operation_is_running') from None
        yield


class State:
    def __init__(self, home):
        self.home = home
        self.db = sqlite3.connect(home / 'jobs.sqlite', timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (sku TEXT PRIMARY KEY, data TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)')
        self.db.commit()

    def config(self):
        row = self.db.execute('SELECT data FROM config WHERE id=1').fetchone()
        if not row:
            raise ValueError('bind_store_first')
        return json.loads(row[0])

    def bind(self, config):
        old = self.db.execute('SELECT data FROM config WHERE id=1').fetchone()
        if old:
            before = json.loads(old[0])
            if (before['seller_id'], before['warehouse_id']) != (config['seller_id'], config['warehouse_id']):
                raise ValueError('use_separate_home_for_another_store_or_warehouse')
        self.db.execute('INSERT OR REPLACE INTO config VALUES (1, ?)', (json.dumps(config),))
        self.db.commit()

    def jobs(self):
        return [json.loads(r[0]) for r in self.db.execute('SELECT data FROM jobs ORDER BY rowid')]

    def get(self, sku):
        row = self.db.execute('SELECT data FROM jobs WHERE sku=?', (sku,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, job):
        self.db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?)', (job['sku'], json.dumps(job, ensure_ascii=False)))
        self.db.commit()


def save_token(home, token):
    if not isinstance(token, str) or not token.strip() or '\n' in token or '\r' in token:
        raise ValueError('invalid_token_input')
    import keyring
    keyring.set_password('wb-fast-listing-skill-pure', str(home), token.strip())


def load_token(home):
    if token := os.environ.get('WB_API_TOKEN'):
        return token.strip()
    import keyring
    token = keyring.get_password('wb-fast-listing-skill-pure', str(home))
    if not token:
        raise ValueError('wb_token_not_found')
    return token
