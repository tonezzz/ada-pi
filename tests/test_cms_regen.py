"""cms_regen / kanban_autofix unit tests — mocked MDDB + HTTP transports.

No network, no real clock: post_json/get_json are fakes routed by URL
suffix, sleep/monotonic/now are injected. Covers the card contract:
POST regenerate -> poll registry last_run -> health re-check -> phantom
marking (interval_min=0 + managed_by) -> comms + job ledger.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone

from scripts.ada import cms_regen, kanban_autofix

AUTOMATION = cms_regen.AUTOMATION_COLLECTION
PAGES = cms_regen.CMS_COLLECTION
HANDOFF = cms_regen.HANDOFF_COLLECTION

NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def regdoc(slug, **cfg):
    return {"key": slug, "contentMd": json.dumps(cfg),
            "meta": {"kind": ["automation-config"], "slug": [slug]}}


def pagedoc(slug, updated, generated_by=None, written_by=None):
    meta = {"kind": ["page"], "slug": [slug], "updated": [updated]}
    if generated_by:
        meta["generated_by"] = [generated_by]
    if written_by:
        meta["written_by"] = [written_by]
    return {"key": slug, "contentMd": "# page\n", "meta": meta}


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Wire:
    """post_json/get_json fakes. `docs` is the MDDB store;
    `mutate_on_get` lets a test flip the registry doc after N polls."""

    def __init__(self, docs=None, regen=None, verify=None, cards=None):
        self.docs = dict(docs or {})
        self.regen = regen or (200, {"status": "updated",
                                     "slug": "x", "queued": True})
        self.verify = verify if verify is not None else (200, {"ok": True})
        self.cards = cards or []
        self.adds = []        # mddb /add payloads, in order
        self.comments = []    # board /comment payloads
        self.regen_calls = 0
        self.reg_gets = 0
        self.mutate = None    # callable(docs, reg_gets) run on each get

    def post(self, url, payload=None, headers=None, timeout=20.0):
        if url.endswith("/get"):
            if payload["collection"] == AUTOMATION:
                self.reg_gets += 1
                if self.mutate:
                    self.mutate(self.docs, self.reg_gets)
            doc = self.docs.get((payload["collection"], payload["key"]))
            return (200, doc) if doc else (404, {})
        if url.endswith("/add"):
            self.adds.append(payload)
            self.docs[(payload["collection"], payload["key"])] = {
                "key": payload["key"], "contentMd": payload["contentMd"],
                "meta": payload.get("meta") or {}}
            return 200, {"ok": True}
        if url.endswith("/regenerate"):
            self.regen_calls += 1
            return self.regen() if callable(self.regen) else self.regen
        if url.endswith("/comment"):
            self.comments.append(payload)
            return 200, {"ok": True}
        return 500, {}

    def get(self, url, headers=None, timeout=20.0):
        if "/verify" in url:
            return self.verify
        if url.endswith("/cards"):
            return 200, {"cards": self.cards}
        return 404, {}

    def advance_registry(self, slug, at_get, **fields):
        """From the at_get-th automation /get on, merge fields into cfg.
        Note reg_gets=1 is run()'s initial read — the pre-POST baseline."""
        def mut(docs, n):
            if n < at_get:
                return
            key = (AUTOMATION, slug)
            cfg = json.loads(docs[key]["contentMd"])
            cfg.update(fields)
            docs[key] = regdoc(slug, **cfg)
        self.mutate = mut

    def automation_adds(self):
        return [a for a in self.adds if a["collection"] == AUTOMATION]

    def job_adds(self):
        return [a for a in self.adds
                if a["collection"] == HANDOFF
                and str(a["key"]).startswith("job/")]


def run(wire, slug="flood-report", clock=None, **kw):
    clock = clock or Clock()
    kw.setdefault("post_json", wire.post)
    kw.setdefault("get_json", wire.get)
    kw.setdefault("sleep", clock.sleep)
    kw.setdefault("monotonic", clock.monotonic)
    kw.setdefault("now_fn", lambda: NOW)
    kw.setdefault("poll_s", 5)
    kw.setdefault("timeout_s", 60)
    return cms_regen.run(slug, **kw)


class QueuedRegenTests(unittest.TestCase):
    def test_last_run_advances_regenerated(self):
        slug = "flood-report"
        old = _iso(NOW - timedelta(hours=5))
        new = _iso(NOW)
        docs = {(AUTOMATION, slug): regdoc(
                    slug, enabled=True, interval_min=240,
                    last_run=old, last_status="ok"),
                (PAGES, slug): pagedoc(slug, _iso(NOW))}
        wire = Wire(docs)
        # get 1 = initial read; the first poll (get 2) sees the advance.
        wire.advance_registry(slug, at_get=2, last_run=new,
                              last_status="ok", run_now=False)
        out = run(wire, slug, card_id="cms-auto-flood-report")
        self.assertTrue(out["ok"])
        self.assertEqual(out["outcome"], "regenerated")
        self.assertEqual(out["last_run_before"], old)
        self.assertEqual(out["last_run_after"], new)
        self.assertEqual(out["health"]["state"], "ok")
        self.assertTrue(out["health"]["verify_ok"])
        self.assertTrue(out["comms_posted"])
        self.assertIn("cms-regen flood-report: regenerated",
                      wire.comments[0]["text"])
        self.assertEqual(wire.comments[0]["id"], "cms-auto-flood-report")
        jobs = wire.job_adds()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["meta"]["kind"], ["job"])
        self.assertEqual(jobs[0]["meta"]["status"], ["done"])
        self.assertEqual(jobs[0]["meta"]["result"], ["regenerated"])
        self.assertEqual(jobs[0]["meta"]["card"], ["cms-auto-flood-report"])

    def test_advanced_but_error_status(self):
        slug = "flood-report"
        docs = {(AUTOMATION, slug): regdoc(
                    slug, enabled=True, interval_min=60,
                    last_run=_iso(NOW - timedelta(hours=3))),
                (PAGES, slug): pagedoc(slug, _iso(NOW))}
        wire = Wire(docs)
        wire.advance_registry(slug, at_get=2, last_run=_iso(NOW),
                              last_status="error",
                              last_error="feeds unreachable", run_now=False)
        out = run(wire, slug)
        self.assertFalse(out["ok"])
        self.assertEqual(out["outcome"], "regen_error")
        self.assertIn("feeds unreachable", out["detail"])

    def test_run_now_cleared_counts_as_consumed(self):
        slug = "flood-report"
        old = _iso(NOW - timedelta(hours=3))
        docs = {(AUTOMATION, slug): regdoc(
                    slug, enabled=True, interval_min=60, last_run=old),
                (PAGES, slug): pagedoc(slug, _iso(NOW))}
        wire = Wire(docs)

        # run_now observed set on poll 2, cleared on poll 3 with last_run
        # unchanged — the queue was consumed even without a write-back yet.
        def mut(d, n):
            key = (AUTOMATION, slug)
            if n == 2:
                d[key] = regdoc(slug, enabled=True, interval_min=60,
                                last_run=old, run_now=True)
            elif n >= 3:
                d[key] = regdoc(slug, enabled=True, interval_min=60,
                                last_run=old, run_now=False)
        wire.mutate = mut
        out = run(wire, slug)
        self.assertEqual(out["outcome"], "regenerated")

    def test_absent_run_now_never_counts_as_cleared(self):
        # The phantom pre-bug: docs without run_now must NOT read as
        # 'queue consumed' — that false-advanced every phantom test.
        slug = "flood-report"
        docs = {(AUTOMATION, slug): regdoc(
                    slug, enabled=True, interval_min=60,
                    last_run=_iso(NOW - timedelta(days=4))),
                (PAGES, slug): pagedoc(slug, _iso(NOW))}
        wire = Wire(docs)
        out = run(wire, slug)
        self.assertEqual(out["outcome"], "phantom_marked")


class PhantomRegistryTests(unittest.TestCase):
    def _wire(self, slug="camwall-noble-a", page_updated=None,
              generated_by="cam-wall-cms.timer", **cfg):
        page_updated = page_updated or _iso(NOW)
        base = {"enabled": True, "interval_min": 240,
                "last_run": _iso(NOW - timedelta(days=3)),
                "last_status": "ok"}
        base.update(cfg)
        return Wire({(AUTOMATION, slug): regdoc(slug, **base),
                     (PAGES, slug): pagedoc(slug, page_updated,
                                            generated_by=generated_by)})

    def test_fresh_page_unconsumed_run_now_marks_phantom(self):
        wire = self._wire()
        out = run(wire, "camwall-noble-a", card_id="cms-auto-camwall-noble-a")
        self.assertTrue(out["ok"])
        self.assertEqual(out["outcome"], "phantom_marked")
        self.assertEqual(out["managed_by"], "cam-wall-cms.timer")
        self.assertEqual(out["health"]["state"], "managed")
        adds = wire.automation_adds()
        self.assertEqual(len(adds), 1)
        cfg = json.loads(adds[0]["contentMd"])
        self.assertEqual(cfg["interval_min"], 0)
        self.assertEqual(cfg["managed_by"], "cam-wall-cms.timer")
        self.assertFalse(cfg.get("run_now"))
        self.assertIn("phantom registry", wire.comments[0]["text"])

    def test_managed_by_prefers_explicit_arg(self):
        wire = self._wire()
        out = run(wire, "camwall-noble-a", managed_by="nightly-batch")
        self.assertEqual(out["managed_by"], "nightly-batch")

    def test_managed_by_falls_back_to_written_by(self):
        slug = "camwall-noble-a"
        docs = {(AUTOMATION, slug): regdoc(slug, enabled=True,
                                           interval_min=240,
                                           last_run=_iso(NOW - timedelta(days=1))),
                (PAGES, slug): pagedoc(slug, _iso(NOW),
                                       written_by="report-rollup.py")}
        wire = Wire(docs)
        out = run(wire, slug)
        self.assertEqual(out["managed_by"], "report-rollup.py")

    def test_stale_page_stays_stalled_no_write(self):
        wire = self._wire(page_updated=_iso(NOW - timedelta(days=5)))
        out = run(wire, "camwall-noble-a")
        self.assertFalse(out["ok"])
        self.assertEqual(out["outcome"], "stalled")
        self.assertEqual(wire.automation_adds(), [])
        self.assertEqual(wire.job_adds()[0]["meta"]["status"], ["failed"])

    def test_stalled_reports_declared_generator(self):
        wire = self._wire(page_updated=_iso(NOW - timedelta(days=5)),
                          generator={"kind": "command",
                                     "name": "host-services-cms"})
        out = run(wire, "camwall-noble-a")
        self.assertEqual(out["outcome"], "stalled")
        self.assertIn("host-services-cms", out["detail"])

    def test_unknown_generator_422_fresh_page_is_phantom(self):
        wire = self._wire(generator={"kind": "command",
                                     "name": "not-in-allowlist"})
        wire.regen = (422, {"detail": "unknown command generator "
                                      "'not-in-allowlist' — not in allowlist"})
        out = run(wire, "camwall-noble-a")
        self.assertEqual(out["outcome"], "phantom_marked")

    def test_dry_run_marks_nothing(self):
        wire = self._wire()
        out = run(wire, "camwall-noble-a", dry_run=True)
        self.assertEqual(out["outcome"], "phantom_dryrun")
        self.assertEqual(wire.automation_adds(), [])
        self.assertEqual(wire.comments, [])
        self.assertEqual(wire.job_adds(), [])

    def test_page_advancing_during_wait_is_phantom(self):
        # Page `updated` was old at dispatch but the bulk timer rewrote it
        # while we polled — same conclusion as an already-fresh page.
        slug = "camwall-noble-a"
        docs = {(AUTOMATION, slug): regdoc(slug, enabled=True,
                                           interval_min=240,
                                           last_run=_iso(NOW - timedelta(days=3))),
                (PAGES, slug): pagedoc(slug, _iso(NOW - timedelta(days=4)))}
        wire = Wire(docs)

        def bump_page(d, n):
            if n >= 3:
                d[(PAGES, slug)] = pagedoc(
                    slug, _iso(NOW), generated_by="cam-wall-cms.timer")
        wire.mutate = bump_page
        out = run(wire, slug)
        self.assertEqual(out["outcome"], "phantom_marked")


class CommandGeneratorTests(unittest.TestCase):
    def test_done_writeback_regenerated(self):
        slug = "services-michael-ha"
        docs = {(AUTOMATION, slug): regdoc(
                    slug, enabled=True, interval_min=10080,
                    generator={"kind": "command", "name": "host-services-cms"},
                    last_run=_iso(NOW - timedelta(days=8))),
                (PAGES, slug): pagedoc(slug, _iso(NOW))}
        wire = Wire(docs, regen=(200, {"status": "done",
                                       "generator": "host-services-cms",
                                       "exit_code": 0}))
        wire.advance_registry(slug, at_get=2, last_run=_iso(NOW))
        out = run(wire, slug)
        self.assertEqual(out["outcome"], "regenerated")

    def test_error_reports_output_tail(self):
        slug = "services-michael-ha"
        docs = {(AUTOMATION, slug): regdoc(
                    slug, generator={"kind": "command",
                                     "name": "host-services-cms"},
                    last_run=_iso(NOW - timedelta(days=8))),
                (PAGES, slug): pagedoc(slug, _iso(NOW))}
        wire = Wire(docs, regen=(200, {"status": "error",
                                       "generator": "host-services-cms",
                                       "exit_code": 1,
                                       "output": "ssh: connect timeout"}))
        out = run(wire, slug)
        self.assertFalse(out["ok"])
        self.assertEqual(out["outcome"], "generator_error")
        self.assertIn("ssh: connect timeout", out["detail"])

    def test_http_failure_is_honest(self):
        slug = "flood-report"
        wire = Wire({(AUTOMATION, slug): regdoc(slug, enabled=True)})
        wire.regen = (0, {})
        out = run(wire, slug)
        self.assertEqual(out["outcome"], "regen_http_error")
        self.assertFalse(out["ok"])

    def test_mddb_unreachable_is_not_no_registry(self):
        # status 0 = transport failure — must not be misread as a doc
        # being absent (phantom handling would write against a lie).
        wire = Wire()
        def dead_post(url, payload=None, headers=None, timeout=20.0):
            return (0, {})
        wire.post = dead_post
        out = run(wire, "flood-report")
        self.assertEqual(out["outcome"], "mddb_unreachable")
        self.assertFalse(out["ok"])

    def test_no_registry_doc_reports_cleanly(self):
        wire = Wire()
        out = run(wire, "gone-page", card_id="cms-auto-gone-page")
        self.assertEqual(out["outcome"], "no_registry")
        self.assertFalse(out["ok"])
        self.assertEqual(wire.regen_calls, 0)
        self.assertIn("no ada-cms-automation doc", wire.comments[0]["text"])


class KanbanAutofixTests(unittest.TestCase):
    def test_match_rules(self):
        self.assertEqual(kanban_autofix.match("cms-auto-flood-report"),
                         ("cms-auto-", "cms-regen", "flood-report"))
        self.assertIsNone(kanban_autofix.match("cms-auto-"))
        self.assertIsNone(kanban_autofix.match("ada-tools-manifest"))
        self.assertIsNone(kanban_autofix.match("cms-auto-Bad Slug!"))
        rules = kanban_autofix.parse_rules("cms-auto-*:cms-regen")
        self.assertEqual(rules, {"cms-auto-": "cms-regen"})

    def _board_wire(self, comms=None):
        card = {"id": "cms-auto-flood-report", "column": "review",
                "comms": comms or []}
        return Wire(cards=[card])

    def test_dispatch_mode_fires_playbook(self):
        wire = self._board_wire()
        calls = []

        def fake_dispatch(repo, playbook, params):
            calls.append((repo, playbook, params))
            return {"ok": True, "task_id": "20261005-230000-cms-regen-x"}

        out = kanban_autofix.handle_card(
            "cms-auto-flood-report", "review", mode="dispatch",
            dispatch_fn=fake_dispatch, post_json=wire.post,
            get_json=wire.get)
        self.assertTrue(out["ok"])
        self.assertEqual(calls, [("ada-pi", "cms-regen",
                                  {"slug": "flood-report",
                                   "card": "cms-auto-flood-report"})])
        self.assertIn("dispatched", wire.comments[0]["text"])
        self.assertIn("20261005-230000-cms-regen-x",
                      wire.comments[0]["text"])

    def test_run_mode_executes_regen(self):
        wire = self._board_wire()
        calls = []
        out = kanban_autofix.handle_card(
            "cms-auto-camwall-noble-a", "review", mode="run",
            run_fn=lambda slug, card, board: (
                calls.append((slug, card)) or {"ok": True}),
            post_json=wire.post, get_json=wire.get)
        self.assertTrue(out["ok"])
        self.assertEqual(calls, [("camwall-noble-a",
                                  "cms-auto-camwall-noble-a")])

    def test_non_cms_card_never_acts(self):
        wire = Wire()
        for cid in ("ada-tools-manifest", "board-api-auth", "cms-regen-autofix"):
            out = kanban_autofix.handle_card(
                cid, "review", post_json=wire.post, get_json=wire.get)
            self.assertEqual(out["skipped"], "no auto_fix rule")
        self.assertEqual(wire.comments, [])

    def test_wrong_column_skipped(self):
        wire = Wire()
        out = kanban_autofix.handle_card(
            "cms-auto-flood-report", "doing",
            post_json=wire.post, get_json=wire.get)
        self.assertIn("column", out["skipped"])

    def test_recent_fix_dedups(self):
        recent = (datetime.now() - timedelta(minutes=5)).strftime(
            "%Y-%m-%d %H:%M")
        wire = self._board_wire(comms=[{
            "at": recent, "from": "devin",
            "text": "auto_fix cms-regen: dispatched for 'flood-report'"}])
        calls = []
        out = kanban_autofix.handle_card(
            "cms-auto-flood-report", "review",
            dispatch_fn=lambda *a: calls.append(a),
            post_json=wire.post, get_json=wire.get)
        self.assertIn("recent", out["skipped"])
        self.assertEqual(calls, [])

    def test_dispatch_failure_posted_to_comms(self):
        wire = self._board_wire()
        out = kanban_autofix.handle_card(
            "cms-auto-flood-report", "review",
            dispatch_fn=lambda *a: {"ok": False, "error": "gate refused"},
            post_json=wire.post, get_json=wire.get)
        self.assertFalse(out["ok"])
        self.assertIn("dispatch failed", wire.comments[0]["text"])

    def test_dry_run_reports_without_side_effects(self):
        wire = self._board_wire()
        out = kanban_autofix.handle_card(
            "cms-auto-flood-report", "review", dry_run=True,
            post_json=wire.post, get_json=wire.get)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["slug"], "flood-report")
        self.assertEqual(wire.comments, [])


if __name__ == "__main__":
    unittest.main()
