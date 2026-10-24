"""Data layer: every query goes to Zetaris over its REST API (or to the sample cache in SAMPLE_MODE)."""
import os
import time
import uuid

from . import cache, config, log


class SQLError(Exception):
    pass


def run_sql(sql, limit=200, purpose="", agent=False):
    """Run SQL on Zetaris. agent=True marks model-written queries (already passed sqlguard): they use the
    read-only key ZETARIS_AGENT_API_KEY when one is configured."""
    sql = sql.strip().rstrip(";")
    t0 = time.time()
    if config.FREEZE_SQL and not config.SAMPLE_MODE:
        hit = cache.get("sql", sql)
        if hit is not None:
            val = hit["value"]
            log.event("sql", purpose=purpose, sql=sql, source="frozen", rows=len(val["rows"]))
            return val
    if config.SAMPLE_MODE:
        hit = cache.get("sql", sql)
        if hit is None:
            log.event("sql", purpose=purpose, sql=sql, source="sample", error="not in sample cache")
            raise SQLError("SAMPLE_MODE: this query is not in the sample cache")
        val = hit["value"]
        log.event("sql", purpose=purpose, sql=sql, source="sample", rows=len(val["rows"]))
        return val
    import requests  # only the live path needs it
    r = requests.post(
        os.environ["ZETARIS_REST_URL"].rstrip("/") + "/sql-editor/sqls/run",
        headers={
            "Authorization": "Bearer " + ((agent and os.environ.get("ZETARIS_AGENT_API_KEY")) or os.environ["ZETARIS_API_KEY"]),
            "X-Org-ID": os.environ["ZETARIS_ORG_ID"],
            "X-Request-ID": str(uuid.uuid4()),
        },
        json={"queryId": str(uuid.uuid4()), "sql": sql, "source": "SqlEditor", "limit": limit},
        timeout=120,
    )
    dt = round(time.time() - t0, 2)
    try:
        body = r.json()
    except ValueError:
        body = {"msg": r.text[:500]}
    if r.status_code != 200 or body.get("error"):
        msg = str(body.get("msg") or body.get("error") or body)[:600]
        log.event("sql", purpose=purpose, sql=sql, source="zetaris", seconds=dt, error=msg)
        raise SQLError(msg)
    cols = (body.get("columns") or body.get("schema") or body.get("columnNames") or body.get("header")
            or body.get("headers") or (body.get("meta") or {}).get("columns") or [])
    cols = [c.get("name", str(c)) if isinstance(c, dict) else str(c) for c in cols]
    rows = body.get("data") or []
    val = {"columns": cols, "rows": rows}
    cache.put("sql", sql, val)
    log.event("sql", purpose=purpose, sql=sql, source="zetaris", seconds=dt, rows=len(rows))
    return val
