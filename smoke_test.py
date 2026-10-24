"""RiverTruth smoke test: checks the Data (Zetaris) and Power (Meterless Nightshift + Nemotron) layers.

Run:  python3 smoke_test.py            -> runs all checks
      python3 smoke_test.py --models   -> also lists models Nightshift exposes (to pick FAST_MODEL / DEEP_MODEL)
"""
import os
import sys
import uuid

import requests
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
results = []


def check(name, fn):
    try:
        detail = fn() or ""
        results.append(("PASS", name, detail))
    except Exception as error:  # noqa: BLE001
        results.append(("FAIL", name, str(error)[:200]))


def need(*names):
    for name in names:
        value = os.environ.get(name, "")
        assert value and not value.startswith("your-"), f"{name} is missing or still a placeholder in .env"


def zetaris_web():
    requests.get(os.environ.get("ZETARIS_BASE_URL", "http://localhost:3000"), timeout=10).raise_for_status()


def zetaris_sql():
    need("ZETARIS_REST_URL", "ZETARIS_ORG_ID", "ZETARIS_API_KEY")
    r = requests.post(
        os.environ["ZETARIS_REST_URL"].rstrip("/") + "/sql-editor/sqls/run",
        headers={
            "Authorization": "Bearer " + os.environ["ZETARIS_API_KEY"],
            "X-Org-ID": os.environ["ZETARIS_ORG_ID"],
            "X-Request-ID": str(uuid.uuid4()),
        },
        json={"queryId": str(uuid.uuid4()), "sql": "SELECT 1", "source": "SqlEditor", "limit": 1},
        timeout=60,
    )
    r.raise_for_status()
    data = r.json().get("data")
    assert data and str(data[0][0]) == "1", "unexpected reply: " + r.text[:80]


def client():
    need("METERLESS_BASE_URL", "METERLESS_RUNTIME_TOKEN")
    return OpenAI(
        base_url=os.environ["METERLESS_BASE_URL"],
        api_key=os.environ["METERLESS_RUNTIME_TOKEN"],
        default_headers={
            "meterless-app": os.environ.get("METERLESS_APP", "rivertruth"),
            "meterless-scope": "user",
        },
        timeout=90,
    )


def list_models():
    ids = [m.id for m in client().models.list().data]
    assert ids, "Nightshift returned no models"
    return f"{len(ids)} models: " + ", ".join(ids[:25])


def make_model_check(var):
    def run():
        need(var)
        model = os.environ[var]
        reply = client().chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "Reply with the single word: ready"}],
            max_tokens=200,
        )
        assert reply.choices, "no reply from the model"
        served = getattr(reply, "model", None) or model
        text = (reply.choices[0].message.content or "").strip().replace("\n", " ")[:40]
        return f"requested={model} served_by={served} reply='{text}'"
    return run


check("Data: Zetaris web interface", zetaris_web)
check("Data: Zetaris runs SQL over its REST API", zetaris_sql)
check("Power: Nightshift model discovery", list_models)
check("Power: Lane 1 (FAST_MODEL) replies", make_model_check("FAST_MODEL"))
check("Power: Lane 2 (DEEP_MODEL) replies", make_model_check("DEEP_MODEL"))

show_models = "--models" in sys.argv
for status, name, detail in results:
    if name.startswith("Power: Nightshift model discovery") and status == "PASS" and not show_models:
        detail = detail.split(":")[0] + " (run with --models to list them)"
    print(f"{status}  {name}  {detail}")

sys.exit(0 if all(s == "PASS" for s, _, _ in results) else 1)
