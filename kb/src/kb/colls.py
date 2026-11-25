"""Collections: a named group of sources with one profile. Loading a profile fails closed on anything malformed."""
import dataclasses
import time

from . import audit
from .errors import KbError
from .ids import new_id
from .profile import Profile


def create(conn, name, profile=None):
    profile = profile or Profile()
    cid = new_id("coll")
    conn.execute("INSERT INTO collections(collection_id,name,profile_json,created_at) VALUES (?,?,?,?)",
                 (cid, name, profile.to_json(), time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())))
    return cid


def set_flags(conn, name, agent_visible=None, allow_external_model=None):
    """Change who may receive the content of a collection: to an agent (`kb ask`), to a model that is not on this machine. Returns (collection id, old
    profile, new profile). The change is a visibility change like any other: it takes effect at the next commit (a commit made under the old rules cannot
    be activated or rolled back to), and an invariant of the profile still holds (a 'secret' collection can be neither). The content of a collection that
    is closed to agents stops being given to a model at once for new questions; answers already recorded from it are removed at the next commit."""
    row = conn.execute("SELECT collection_id, profile_json FROM collections WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise KbError(f"no collection called {name!r}")
    old = Profile.from_json(row[1])
    changes = {}
    if agent_visible is not None:
        changes["agent_visible"] = bool(agent_visible)
    if allow_external_model is not None:
        changes["allow_external_model"] = bool(allow_external_model)
    new = dataclasses.replace(old, **changes)                   # the profile checks its own invariants again
    if new != old:
        conn.execute("UPDATE collections SET profile_json = ? WHERE collection_id = ?", (new.to_json(), row[0]))
        audit.append(conn, "profile_changed", {"collection": row[0], "agent_visible": new.agent_visible, "allow_external_model": new.allow_external_model})
    return row[0], old, new


def get_profile(conn, collection_id):
    row = conn.execute("SELECT profile_json FROM collections WHERE collection_id=?", (collection_id,)).fetchone()
    if row is None:
        raise KbError(f"unknown collection {collection_id}")
    return Profile.from_json(row[0])           # tampered or hand-edited JSON that violates the invariants raises here
