import os

try:
    from dotenv import load_dotenv
except ImportError:  # tests and sample mode run without it
    def load_dotenv(*_a, **_k):
        return False

load_dotenv()

SAMPLE_MODE = os.environ.get("SAMPLE_MODE", "false").lower() == "true"
FAST_MODEL = os.environ.get("FAST_MODEL", "nvidia/nemotron-3.5-lightning-30b-a3b")
DEEP_MODEL = os.environ.get("DEEP_MODEL", "nvidia/nemotron-3-ultra-550b-a55b")
MAX_CRITIC_ROUNDS = int(os.environ.get("MAX_CRITIC_ROUNDS", "1"))
MAX_INITIAL_CHECKS = int(os.environ.get("MAX_INITIAL_CHECKS", "4"))
MAX_NEW_CHECKS = int(os.environ.get("MAX_NEW_CHECKS", "2"))
MAX_SQL_RETRIES = int(os.environ.get("MAX_SQL_RETRIES", "2"))
RUN_DEADLINE_S = float(os.environ.get("RUN_DEADLINE_S", "420"))   # total time for one investigation
CALL_TIMEOUT_S = float(os.environ.get("CALL_TIMEOUT_S", "180"))   # one model call, never more than the time left
LOG_DIR = os.environ.get("LOG_DIR", "logs")
SAMPLE_DIR = os.environ.get("SAMPLE_DIR", "data/sample")
RECORD_SAMPLES = os.environ.get("RECORD_SAMPLES", "false").lower() == "true"   # only scripts/record_samples.sh turns it on
REPORT_TEMPERATURE = float(os.environ.get("REPORT_TEMPERATURE", "0.3"))   # prose roles; JSON/SQL roles use 0
CONTACT = os.environ.get("RIVERTRUTH_CONTACT", "rivertruth-hackathon-demo")  # user-agent sent to the public APIs
# While recording samples, read each SQL result once and reuse it for the rest of the session, so every case
# analyses the same data (live data can change between cases, which would break exact replay).
FREEZE_SQL = os.environ.get("FREEZE_SQL", "false").lower() == "true"
