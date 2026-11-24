"""The kb data folder. There is no default location on purpose: a hard-coded path made another tool point at the wrong
folder (superagent lesson), so every command gets --home or the KB_HOME environment variable, and nothing else."""
import os

from . import db
from .errors import KbError

SUBDIRS = ("blobs", "staging", "segments", "backups", "logs")
DB_NAME = "kb.db"


class Home:
    def __init__(self, path):
        self.path = os.path.abspath(path)

    @property
    def db_path(self):
        return os.path.join(self.path, DB_NAME)

    def sub(self, name):
        return os.path.join(self.path, name)

    def init(self):
        os.makedirs(self.path, exist_ok=True)
        for s in SUBDIRS:
            os.makedirs(self.sub(s), exist_ok=True)
        return db.migrate(self.db_path, backup_dir=self.sub("backups"))


def resolve_home(arg):
    path = arg or os.environ.get("KB_HOME")
    if not path:
        raise KbError("no data folder: pass --home <folder> or set KB_HOME (kb has no default location)")
    return Home(path)
