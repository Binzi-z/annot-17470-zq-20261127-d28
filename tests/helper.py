"""测试辅助：默认合成规则表与最小输入构造。"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = REPO_ROOT / "data" / "synthetic_input.json"

ZONE = {
    "name": "合成夏令时区",
    "origin": "2026-01-01T00:00:00Z",
    "initial_offset_minutes": 0,
    "rules": [
        {"at_utc": "2026-03-29T01:00:00Z", "offset_minutes": 60},
        {"at_utc": "2026-11-01T01:00:00Z", "offset_minutes": 0},
    ],
}


def sig(alias, generation=0):
    return {"alias": alias, "generation": generation}


def anchor(rid, version, alias, root, at="2026-10-01T00:00:00Z", gen=0):
    return {
        "id": rid, "version": version, "group": "ID", "kind": "anchor",
        "at": at, "root": root, "signers": [sig(alias, gen)],
    }


def reroot(rid, version, alias, root, at, gen=1):
    return {
        "id": rid, "version": version, "group": "ID", "kind": "re_root",
        "at": at, "root": root, "signers": [sig(alias, gen)],
    }


def grant(rid, group, signers, start, end, version="v1", fold=0):
    return {
        "id": rid, "version": version, "group": group, "kind": "grant",
        "local_start": start, "local_end": end, "fold": fold,
        "signers": signers,
    }


def deny(rid, group, signers, start, end, version="v1", fold=0):
    return {
        "id": rid, "version": version, "group": group, "kind": "deny",
        "local_start": start, "local_end": end, "fold": fold,
        "signers": signers,
    }


def revoke(rid, group, target, signers, at, version="v1"):
    return {
        "id": rid, "version": version, "group": group, "kind": "revoke",
        "at": at, "target": target, "signers": signers,
    }


def payload(records):
    return {"synthetic": True, "zone": ZONE, "records": records}


def load_fixture():
    return json.loads(DATA_FILE.read_text(encoding="utf-8"))
