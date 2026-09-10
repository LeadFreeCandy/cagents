"""Small stdlib-only tracking bridge, vendored identically in both apps."""
from pathlib import Path
import json
import os
import sqlite3
import time


def bridge_path(local_path, app):
    override = os.environ.get("CAGENTS_SHARED_DB")
    if override:
        return Path(override)
    base = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    expected = base / app / ("state.json" if app == "cagents" else "state.sqlite3")
    # Isolated stores/tests do not join the user's shared conversation list.
    return base / "cagents-shared/claude.sqlite3" if Path(local_path).resolve() == expected.resolve() else None


def sync(path, consumer, local, apply):
    """Union on first connection; subsequent explicit removals propagate too.

    `apply` persists the resulting local ID->cwd map while the bridge transaction
    is open. Its checkpoint is committed only after that succeeds, so retries
    after interruption cannot turn an unapplied remote addition into a deletion.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    try:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS conversations (id TEXT PRIMARY KEY, cwd TEXT, present INTEGER, at REAL);
            CREATE TABLE IF NOT EXISTS seen (consumer TEXT, id TEXT, PRIMARY KEY(consumer,id));
            CREATE TABLE IF NOT EXISTS clients (consumer TEXT PRIMARY KEY);
        """)
        db.execute("BEGIN IMMEDIATE")
        previous = {row[0] for row in db.execute("SELECT id FROM seen WHERE consumer=?", (consumer,))}
        for sid in set(local) - previous:
            db.execute("INSERT OR REPLACE INTO conversations VALUES (?,?,1,?)", (sid, local[sid], time.time()))
        for sid in previous - set(local):
            db.execute("UPDATE conversations SET present=0,at=? WHERE id=?", (time.time(), sid))
        shared = dict(db.execute("SELECT id,cwd FROM conversations WHERE present=1"))
        apply(shared)
        db.execute("DELETE FROM seen WHERE consumer=?", (consumer,))
        db.executemany("INSERT INTO seen VALUES (?,?)", [(consumer, sid) for sid in shared])
        db.execute("INSERT OR IGNORE INTO clients VALUES (?)", (consumer,))
        db.commit()
        return shared
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def forget_checkpoint(path, consumer):
    """Reset rejoins by union; clearing a local list must not publish removals."""
    if not Path(path).exists():
        return
    with sqlite3.connect(path, timeout=5) as db:
        db.execute("DELETE FROM seen WHERE consumer=?", (consumer,))


def seed_legacy(path, legacy_path):
    """Read-only first import when the old dashboard has not been restarted yet."""
    if not Path(legacy_path).exists():
        return
    if Path(path).exists():
        db = sqlite3.connect(path)
        try:
            if db.execute("SELECT 1 FROM clients LIMIT 1").fetchone():
                return
        except sqlite3.OperationalError:
            pass
        finally:
            db.close()
    raw = json.loads(Path(legacy_path).read_text())
    local = {sid: row.get("project_dir", "") for sid, row in raw.get("sessions", {}).items() if isinstance(row, dict)}
    sync(path, "cagents:" + str(Path(legacy_path).resolve()), local, lambda _: None)
