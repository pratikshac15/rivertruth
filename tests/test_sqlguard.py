"""Model-written SQL passes a read-only gate before it reaches Zetaris (audit blocker 1)."""
import json
import unittest

from tests.fakes import Env

VIEW = "water.hourly_compare"


class Gate(unittest.TestCase):
    def ok(self, sql, views=(VIEW,)):
        from agent import sqlguard
        return sqlguard.check(sql, views)

    def bad(self, sql, views=(VIEW,)):
        from agent import sqlguard
        with self.assertRaises(sqlguard.Rejected):
            sqlguard.check(sql, views)

    def test_plain_select_on_the_case_view_passes_and_gets_a_limit(self):
        out = self.ok("SELECT hr_utc, usgs_northport_cfs FROM water.hourly_compare WHERE hr_utc > '2026-10-22 00:00'")
        self.assertTrue(out.rstrip().endswith("LIMIT 200"))

    def test_with_clause_and_subqueries_pass(self):
        self.ok("WITH d AS (SELECT substr(hr_utc,1,10) AS day, MAX(nws_oliver_cfs) AS m FROM water.hourly_compare "
                "GROUP BY 1) SELECT day, m FROM d ORDER BY day LIMIT 50")
        self.ok("SELECT * FROM (SELECT hr_utc FROM water.hourly_compare) t LIMIT 10")

    def test_large_limit_is_capped(self):
        self.assertIn("LIMIT 200", self.ok("SELECT hr_utc FROM water.hourly_compare LIMIT 100000"))

    def test_trailing_semicolon_is_fine(self):
        self.ok("SELECT hr_utc FROM water.hourly_compare LIMIT 5;")

    def test_ddl_and_dml_are_rejected(self):
        for sql in ("DROP VIEW water.hourly_compare",
                    "CREATE LIGHTNING REST TABLE x FROM NOAA_NWPS REQUEST(endpoint \"http://169.254.169.254/\") HEADER () BODY ()",
                    "INSERT INTO water.hourly_compare VALUES (1)", "DELETE FROM water.hourly_compare",
                    "CACHE TABLE water.hourly_compare", "UNCACHE TABLE water.hourly_compare",
                    "ALTER VIEW water.hourly_compare AS SELECT 1", "SET spark.sql.x = 1", "USE water"):
            with self.subTest(sql=sql):
                self.bad(sql)

    def test_stacked_statements_are_rejected(self):
        self.bad("SELECT 1 FROM water.hourly_compare; DROP VIEW water.hourly_compare")

    def test_comments_cannot_hide_anything(self):
        self.bad("SELECT hr_utc FROM water.hourly_compare -- ; DROP VIEW x")
        self.bad("SELECT hr_utc FROM water.hourly_compare /* x */")

    def test_keywords_inside_string_literals_are_allowed(self):
        self.ok("SELECT 'drop table' AS note, hr_utc FROM water.hourly_compare WHERE hr_utc <> ';' LIMIT 3")

    def test_only_the_case_views_may_be_read(self):
        self.bad("SELECT * FROM noaa_nwps.toda1_gauge_raw")
        self.bad("SELECT * FROM water.hc_avla1")
        self.bad("SELECT * FROM water.hourly_compare a JOIN usgs_nwis.iv_black_warrior_raw b ON a.hr_utc = b.x")

    def test_java_reflection_functions_are_rejected(self):
        self.bad("SELECT reflect('java.lang.Runtime', 'getRuntime') FROM water.hourly_compare")
        self.bad("SELECT java_method('x', 'y') FROM water.hourly_compare")

    def test_empty_or_non_select_is_rejected(self):
        self.bad("")
        self.bad("SHOW TABLES")
        self.bad("EXPLAIN SELECT 1")


class InTheLoop(unittest.TestCase):
    def test_model_written_drop_never_reaches_zetaris_and_is_sent_back_as_an_error(self):
        with Env() as env:
            from agent import llm, orchestrator
            base, seen = env.llm.chat, []

            def chat(role, messages, **k):
                if role == "investigator.sql":
                    seen.append(messages[-1]["content"])
                    first = len(seen) == 1
                    return json.dumps({"sql": "DROP VIEW water.hourly_compare" if first else
                                       "SELECT hr_utc FROM water.hourly_compare LIMIT 5"})
                return base(role, messages, **k)
            llm.chat = chat
            orchestrator.run("Why?", "TODA1")
            self.assertFalse(any(c.upper().startswith("DROP") for c in env.zet.calls))
            self.assertIn("read-only", seen[1])  # the rejection reason went back to the model


if __name__ == "__main__":
    unittest.main()


class LocalTimeMistakes(unittest.TestCase):
    """Live run Oct 23 22:34: the model wrote DATE_SUB(hr_utc, 5) meaning 5 hours; Spark subtracts 5 DAYS, so every
    day label shifted by 5 days and every 'local hour' became 0. The gate now refuses it and explains."""

    def test_date_arithmetic_on_hours_is_refused_with_the_correct_formula(self):
        from agent import sqlguard
        live = ("SELECT DATE_SUB(hr_utc, 5) AS day_cdt, HOUR(DATE_SUB(hr_utc, 5)) AS peak_hour_cdt, "
                "MAX(usgs_northport_peak_cfs) AS max_peak_cfs FROM water.hourly_compare "
                "WHERE hr_utc BETWEEN '2026-10-16 22:00' AND '2026-10-23 21:00' GROUP BY day_cdt, peak_hour_cdt LIMIT 200")
        for sql in (live, "SELECT date_add(hr_utc, 1) FROM water.hourly_compare",
                    "SELECT hour(hr_utc - INTERVAL 5 HOURS) FROM water.hourly_compare"):
            with self.assertRaises(sqlguard.Rejected) as cm:
                sqlguard.check(sql, [VIEW], tz="America/Chicago")
            self.assertIn("from_utc_timestamp(to_timestamp(hr_utc, 'yyyy-MM-dd HH:mm'), 'America/Chicago')", str(cm.exception))

    def test_the_correct_local_time_formula_passes(self):
        from agent import sqlguard
        sqlguard.check("SELECT to_date(from_utc_timestamp(to_timestamp(hr_utc, 'yyyy-MM-dd HH:mm'), 'America/Chicago')) AS d, "
                       "MAX(usgs_northport_peak_cfs) FROM water.hourly_compare GROUP BY 1", [VIEW], tz="America/Chicago")

    def test_in_the_loop_the_model_is_told_and_retries(self):
        with Env() as env:
            from agent import llm, orchestrator
            base, seen = env.llm.chat, []

            def chat(role, messages, **k):
                if role == "investigator.sql":
                    seen.append(messages[-1]["content"])
                    return json.dumps({"sql": "SELECT DATE_SUB(hr_utc, 5) AS d FROM water.hourly_compare" if len(seen) == 1
                                       else "SELECT hr_utc FROM water.hourly_compare LIMIT 5"})
                return base(role, messages, **k)
            llm.chat = chat
            case = orchestrator.run("Why?", "TODA1")
            self.assertIn("subtracts DAYS", seen[1])
            self.assertFalse(case.evidence[0].error)
