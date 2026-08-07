"""
Physical-table naming helper (bridge / source / error).

Convention: tbl_<source>_<base> — lowercase snake_case, ≤63 chars,
hash-suffixed on overflow. Mirrors artifacts/api-server/table_naming.py
so generated and studio code agree byte-for-byte.
"""
from __future__ import annotations
import hashlib
import re

_PG_NAMEDATALEN = 63
_SAFE_CHAR_RE = re.compile(r"[^a-z0-9_]+")
_PHYSICAL_NAME_RE = re.compile(r"^tbl_[a-z0-9]+(?:_[a-z0-9]+)+$")


def assert_physical_name(name, context="physical_name"):
    """Hard-enforce tbl_<source>_<base> at create-table call sites.
    Raises ValueError on violation; returns name on success.
    """
    if not isinstance(name, str) or not name:
        raise ValueError("%s: name must be a non-empty string (got %r)" % (context, name))
    if not _PHYSICAL_NAME_RE.match(name):
        raise ValueError(
            "%s: %r does not match tbl_<source>_<base> convention; "
            "use physical_name() / build_name_map()." % (context, name)
        )
    if len(name) > _PG_NAMEDATALEN:
        raise ValueError("%s: %r exceeds Postgres NAMEDATALEN (%d)" % (context, name, _PG_NAMEDATALEN))
    return name


def _sanitize_token(raw):
    if raw is None:
        return ""
    s = str(raw).strip().lower()
    s = _SAFE_CHAR_RE.sub("_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def sanitize_source_name(source):
    return _sanitize_token(source or "") or "unknown"


def sanitize_base_name(base):
    tok = _sanitize_token(base)
    if not tok:
        raise ValueError("base table name is empty after sanitization")
    return tok


def physical_name(stage, source, base):
    """Return tbl_<source>_<base>. The `stage` param is kept for backward
    compatibility but is not embedded in the name — the containing schema
    (bridge / source / error) carries the stage context.
    """
    src_tok = sanitize_source_name(source)
    base_tok = sanitize_base_name(base)
    name = "tbl_%s_%s" % (src_tok, base_tok)
    if len(name) > _PG_NAMEDATALEN:
        digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]  # nosemgrep: insecure-hash-algorithm-sha1
        prefix = name[: _PG_NAMEDATALEN - 1 - len(digest)].rstrip("_")
        name = "%s_%s" % (prefix, digest)
    return assert_physical_name(name, "physical_name")


def build_name_map(source, bases):
    if isinstance(bases, dict):
        flat = sorted({b for v in bases.values() for b in v})
    else:
        seen = set()
        flat = [b for b in bases if not (b in seen or seen.add(b))]
    src = sanitize_source_name(source)
    sanitized = {b: sanitize_base_name(b) for b in flat}
    counts = {}
    for tok in sanitized.values():
        counts[tok] = counts.get(tok, 0) + 1
    out = {}
    for base in flat:
        b_tok = sanitized[base]
        if counts[b_tok] > 1:
            suffix = hashlib.sha1(base.encode("utf-8")).hexdigest()[:6]  # nosemgrep: insecure-hash-algorithm-sha1
            disambiguated = "%s_%s" % (b_tok, suffix)
        else:
            disambiguated = base
        phys = physical_name("bridge", src, disambiguated)
        out[base] = {
            "base":   b_tok,
            "bridge": phys,
            "source": phys,
            "error":  phys,
        }
    return out
