"""Read-only gate for SQL written by a model, applied before anything reaches Zetaris.

The agent may only read the unified hourly view(s) of the river it is investigating. Allowed: one SELECT (or WITH ...
SELECT) statement over those views, with a LIMIT of at most 200. Rejected: anything else, including DDL/DML, Zetaris
REST registration, cache control, session settings, stacked statements, comments, Java reflection functions, and
reads of any other table (the raw agency tables, other rivers' views). A rejected query raises `Rejected`, a
zetaris.SQLError, so the investigator's normal retry loop sends the reason back to the model.

Infrastructure DDL (the Source Scout registering sources) does not go through this gate; see config.ZETARIS keys.
"""
import re

from .zetaris import SQLError

MAX_LIMIT = 200
FORBIDDEN = ("DROP", "CREATE", "ALTER", "INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE", "REPLACE", "GRANT",
             "REVOKE", "CACHE", "UNCACHE", "REFRESH", "SET", "RESET", "USE", "LOAD", "ADD", "MSCK", "ANALYZE",
             "EXPORT", "IMPORT", "CALL", "EXEC", "EXECUTE", "SHOW", "DESCRIBE", "EXPLAIN", "LIGHTNING", "REQUEST",
             "ENDPOINT", "SCHEMASTORE", "OPTIMIZE", "VACUUM", "COPY", "REFLECT", "JAVA_METHOD", "INTO")


class Rejected(SQLError):
    pass


def _mask_strings(sql):
    """Replace the contents of '...' and "..." literals with x's so keywords inside them are not seen."""
    out, i, n = [], 0, len(sql)
    while i < n:
        ch = sql[i]
        if ch in ("'", '"'):
            q, j = ch, i + 1
            while j < n:
                if sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == q:
                    if j + 1 < n and sql[j + 1] == q:  # doubled quote inside a literal
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                raise Rejected("Rejected by the read-only SQL gate: unterminated string literal.")
            out.append(q + "x" * (j - i - 1) + q)
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def local_time_hint(tz):
    local = f"from_utc_timestamp(to_timestamp(hr_utc, 'yyyy-MM-dd HH:mm'), '{tz}')"
    return f"local hour = hour({local}); local day = to_date({local})"


def check(sql, allowed_views, tz="America/Chicago"):
    """Return the SQL to run (with a LIMIT of at most 200) or raise Rejected with a reason for the model."""
    allowed = {v.lower() for v in allowed_views}
    s = (sql or "").strip()
    if not s:
        raise Rejected("Rejected by the read-only SQL gate: empty query.")
    while s.endswith(";"):
        s = s[:-1].rstrip()
    masked = _mask_strings(s)
    if "--" in masked or "/*" in masked or "*/" in masked:
        raise Rejected("Rejected by the read-only SQL gate: comments are not allowed.")
    if ";" in masked:
        raise Rejected("Rejected by the read-only SQL gate: exactly one statement is allowed.")
    if not re.match(r"(?is)^\s*(SELECT|WITH)\b", masked):
        raise Rejected("Rejected by the read-only SQL gate: only a SELECT (or WITH ... SELECT) query is allowed.")
    if re.search(r"(?i)\b(DATE_SUB|DATE_ADD|DATEADD|DATESUB)\s*\(|\bINTERVAL\b", masked):
        raise Rejected("Rejected by the read-only SQL gate: date_sub/date_add subtracts DAYS, not hours, and hr_utc is "
                       f"a UTC hour. For local time use: {local_time_hint(tz)}.")
    words = set(re.findall(r"[A-Za-z_]+", masked.upper()))
    bad = sorted(words & set(FORBIDDEN))
    if bad:
        raise Rejected(f"Rejected by the read-only SQL gate: {', '.join(bad)} is not allowed in a read-only query.")
    ctes = {m.lower() for m in re.findall(r"(?i)(?:\bWITH\b|,)\s*([A-Za-z_]\w*)\s+AS\s*\(", masked)}
    for ref in re.findall(r"(?i)\b(?:FROM|JOIN)\s+([`A-Za-z_][\w.`]*)", masked):
        name = ref.replace("`", "").lower()
        if name in ctes or name in allowed:
            continue
        raise Rejected(f"Rejected by the read-only SQL gate: you may only read {', '.join(sorted(allowed))} "
                       f"(not {name}).")
    lim = list(re.finditer(r"(?i)\bLIMIT\s+(\d+)\s*$", masked))
    if lim:
        n = int(lim[-1].group(1))
        if n > MAX_LIMIT:
            s = s[:lim[-1].start(1)] + str(MAX_LIMIT) + s[lim[-1].end(1):]
    else:
        s = f"{s} LIMIT {MAX_LIMIT}"
    return s
