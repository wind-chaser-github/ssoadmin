"""Read existing English profile names for administrators, without copying tokens.

Both inputs are atomically replaced by English. Only central/local identity keys
and explicitly named learner records are used; nicknames never link identities.
"""
import json
import base64
import re
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

    def account_profiles(self):
        """Explicit account nicknames, separate from children and admin labels."""
        try:
            mappings = self.read("unified-identity.json")["mappings"]
            if not isinstance(mappings, list):
                return {}
            central = {}
            for row in mappings:
                uid, local = row.get("centralId"), row.get("localId")
                if type(uid) is not int or uid < 1 or not isinstance(local, str) or local in central:
                    return {}
                central[local] = uid
            # Older explicitly set account names are useful too. Never use the
            # login hash, credentials or default learner label as a nickname.
            try:
                users = self.read("users.json").get("users", [])
            except (OSError, ValueError):
                users = []
            profiles = {}
            for user in users:
                uid = central.get(user.get("id"))
                name = user.get("displayName")
                if uid and self.valid_name(name):
                    profiles[uid] = {"nickname": name.strip(), "avatar": ""}
            for local, uid in central.items():
                if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}", local):
                    continue
                try:
                    path = self.directory / "account-profiles" / (local + ".json")
                    with path.open("rb") as stream:
                        raw = stream.read(180 * 1024 + 1)
                    if len(raw) > 180 * 1024:
                        continue
                    value = json.loads(raw)
                    if value.get("version") != 1 or value.get("userId") != local or not self.valid_name(value.get("nickname")):
                        continue
                    image = value.get("avatar", "")
                    if image:
                        match = re.fullmatch(r"data:image/(png|jpeg);base64,([A-Za-z0-9+/]+={0,2})", image)
                        if not match or len(base64.b64decode(match[2], validate=True)) > 128 * 1024:
                            image = ""
                    profiles[uid] = {"nickname": value["nickname"].strip(), "avatar": image}
                except (OSError, ValueError, TypeError, AttributeError):
                    continue
            return profiles
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {}

    @staticmethod
    def valid_name(name):
        return (isinstance(name, str) and bool(name.strip()) and len(name.strip()) <= 24
                and name.strip() not in {"小学习者", "微信用户", "小朋友"}
                and not any(ord(c) < 32 or ord(c) == 127 for c in name))
