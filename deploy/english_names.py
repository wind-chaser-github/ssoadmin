"""Read existing English profile names for administrators, without copying tokens.

Both inputs are atomically replaced by English. Only central/local identity keys
and explicitly named learner records are used; nicknames never link identities.
"""
import json
from pathlib import Path


class EnglishNames:
    def __init__(self, directory="/var/lib/seth-english"):
        self.directory = Path(directory)

    def read(self, name):
        with (self.directory / name).open("rb") as stream:
            raw = stream.read(16 * 1024 * 1024 + 1)
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("profile file too large")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError("unsupported profile file")
        return value

    def __call__(self):
        try:
            mappings = self.read("unified-identity.json")["mappings"]
            learners = self.read("family.json")["learners"]
            if not isinstance(mappings, list) or not isinstance(learners, list):
                return {}
            central = {}
            for mapping in mappings:
                uid, local = mapping.get("centralId"), mapping.get("localId")
                if type(uid) is not int or uid < 1 or not isinstance(local, str) or local in central:
                    return {}
                central[local] = uid
            names = {}
            for learner in learners:
                name = learner.get("displayName")
                if (not isinstance(name, str) or not name.strip() or len(name) > 24
                        or any(ord(c) < 32 or ord(c) == 127 for c in name)
                        or name in {"小学习者", "微信用户", "小朋友"}):
                    continue
                for local in (learner.get("ownerUserId"), learner.get("boundStudentUserId")):
                    uid = central.get(local) if isinstance(local, str) else None
                    if uid is not None and name not in names.setdefault(uid, []):
                        names[uid].append(name)
            return names
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # A name-source failure must not prevent quota administration. The
            # stable central user number and administrator label remain usable.
            return {}
