"""Devin dispatch tools (headless sessions; job ledger).

Split out of backend/tool_runner.py (card tool-runner-split, 2026-10-06).
The wildcard import reproduces the original module's global namespace —
helpers, constants, contextvars and backend module handles — so method
bodies moved verbatim and patch("backend.tool_runner.<mod>") targets keep
working (the mixin shares the same imported module objects).
"""
from __future__ import annotations

from .common import *  # noqa: F401,F403


class DevinMixin:

    # -- Devin dispatch tools (headless sessions on tony-dell; job SSOT:
    #    docs/ssot/jobs/ada/2026-09-22-ada-devin-dispatch.yml) --

    async def devin(
        self,
        action: str,
        repo: str = "",
        task: str = "",
        task_id: str = "",
        message: str = "",
    ) -> Any:
        """Devin session control — tools-merge-devin-mcp consolidated
        devin_dispatch / devin_followup / devin_answer. Every action
        mutates a dispatched session — the DEVIN_CONFIRMED_TOOLS seat
        keeps confirmed=true mandatory."""
        action = (action or "").strip().lower()
        if action == "dispatch":
            if not repo or not task:
                raise ValueError("dispatch needs repo and task")
            return await self.devin_dispatch(repo=repo, task=task)
        if action in ("followup", "answer"):
            if not task_id or not message:
                raise ValueError(f"{action} needs task_id and message")
        if action == "followup":
            return await self.devin_followup(task_id=task_id, message=message)
        if action == "answer":
            return await self.devin_answer(task_id=task_id, message=message)
        raise ValueError(
            f"invalid action {action!r}: expected dispatch|followup|answer")

    async def devin_read(
        self,
        action: str,
        task_id: str | None = None,
        status: str | None = None,
        limit: int = 30,
        publish: bool = False,
        confirmed: bool = False,
        confirm_token: str | None = None,
        request: str = "",
        title: str = "",
    ) -> Any:
        """Devin job ledger reads — tools-merge-devin-mcp consolidated
        devin_status / devin_jobs / devin_pending / devin_job_report,
        plus the absorbed ada_devteam_review (action='review',
        owner-only). 'report' with publish=true re-checks confirmation
        inside devin_job_report — confirmed/confirm_token are declared
        here so the execute gate hands them back."""
        action = (action or "").strip().lower()
        if action == "status":
            return await self.devin_status(task_id=task_id or None)
        if action == "jobs":
            return await self.devin_jobs(status=status, limit=limit)
        if action == "pending":
            return await self.devin_pending()
        if action == "report":
            return await self.devin_job_report(
                publish=publish, confirmed=confirmed,
                confirm_token=confirm_token, limit=limit)
        if action == "review":
            # The tools.d manifest carried timeout_s=300 — the expert
            # panel runs multiple model calls; keep the cap now that the
            # drop-in wrapper is gone.
            return await asyncio.wait_for(
                self.ada_devteam_review(request=request, title=title),
                timeout=300)
        raise ValueError(
            f"invalid action {action!r}: "
            "expected status|jobs|pending|report|review")

    async def devin_dispatch(
        self, repo: str, task: str | None = None,
        playbook: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start an unattended Devin session on tony-dell for a task.

        Runs in a dedicated git worktree as a systemd unit; completion is
        reported back via chaba-admin event + iPhone notification. With
        playbook=<name> (registry in backend/playbooks/) the task renders
        from the playbook's template and its gate is enforced — e.g.
        build-tool refuses unless params.spec_key names a spec-review doc
        whose status is pass.
        """
        caller = self.policy_identity()
        return await devin_dispatch_mod.dispatch(
            repo, task, playbook=playbook, params=params,
            mddb=self.mddb, caller=caller,
            is_owner=self._persona_admin(caller))

    async def devin_status(self, task_id: str | None = None) -> str:
        """List dispatched tasks, or show one task's unit state."""
        return await devin_dispatch_mod.status(task_id or None)

    async def devin_followup(self, task_id: str, message: str) -> str:
        """Send a follow-up message into a dispatched session."""
        return await devin_dispatch_mod.followup(task_id, message)

    @staticmethod
    def _job_detail(doc: dict[str, Any]) -> str:
        """One-line detail for a job doc: first body line after the standard
        'Job <id> on <host>: <status>.' preamble, truncated for the report."""
        for line in (doc.get("contentMd") or "").splitlines():
            line = line.strip()
            if not line or line.startswith("Job "):
                continue
            return line[:160]
        return ""

    def _classify_job(
        self, doc: dict[str, Any], dispatch: dict[str, dict[str, str]],
    ) -> tuple[str, dict[str, Any]]:
        """Bucket a job/<id> doc as active|failed|done|stale.

        The ledger is the cross-host source of truth; for tasks on this
        dispatch host we verify a 'running' ledger entry against
        `devin-dispatch status` — a dead unit that never wrote a transcript
        is a spawn failure (the ledger only converges once the watch timer
        notices, and pre-exit_code spawn failures it never did).
        """
        meta = doc.get("meta") or {}
        job_id = (_first(meta.get("job_id"))
                  or (doc.get("key") or "").split("/", 1)[-1])
        ledger = (_first(meta.get("status")) or "").lower()
        job = {
            "task_id": job_id,
            "host": _first(meta.get("host")),
            "status": ledger or "unknown",
            "ts": _first(meta.get("ts")),
            "question": _first(meta.get("question")),
            "detail": self._job_detail(doc),
        }
        if ledger in _JOB_STALE_STATUSES:
            return "stale", job
        if ledger in _JOB_DONE_STATUSES:
            return "done", job
        if ledger == "failed":
            return "failed", job
        rec = dispatch.get(job_id)
        if rec:
            state = (rec.get("state") or "").lower()
            result = (rec.get("result") or "").lower()
            transcript = (rec.get("transcript") or "").strip()
            no_transcript = transcript in {"", "never"}
            if state == "failed" or result.startswith("exit"):
                job["detail"] = job["detail"] or (
                    f"unit {state or 'dead'} (result {result or 'unknown'})"
                )
                return "failed", job
            if no_transcript and (
                state in {"inactive", "dead"}
                or (state == "gone" and self._job_age_s(job) > _JOB_GONE_GRACE_S)
            ):
                job["detail"] = job["detail"] or (
                    "died at spawn — unit is down and no transcript was written"
                )
                return "failed", job
            if state == "inactive" and not no_transcript:
                job["detail"] = "finished per dispatch status — ledger update pending"
                return "done", job
        return "active", job

    @staticmethod
    def _job_age_s(job: dict[str, Any]) -> float:
        """Seconds since the ledger doc's ts; unknown ts counts as old so a
        stale 'running' marker can't hide a spawn failure forever."""
        ts = job.get("ts")
        if not ts:
            return _JOB_GONE_GRACE_S + 1
        try:
            started = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - started).total_seconds()
        except ValueError:
            return _JOB_GONE_GRACE_S + 1

    async def devin_job_report(
        self, publish: bool = False, confirmed: bool = False,
        confirm_token: str | None = None, limit: int = 60,
    ) -> dict[str, Any]:
        """Compose the Devin job report page content.

        Reads the job/<id> ledger and verifies entries against
        `devin-dispatch status`, so failed jobs — including spawn failures
        the ledger still marks running — land in a dedicated 'Failed jobs'
        section instead of the active list. publish=true writes the page
        (CMS write: requires confirmed=true).
        """
        if self.mddb is None:
            return {"status": "error", "error": "mddb unavailable (guest mode)"}
        docs = await self.mddb.search_documents(
            DEVIN_JOBS_COLLECTION, filter_meta={"kind": ["job"]}, limit=limit)
        dispatch: dict[str, dict[str, str]] = {}
        try:
            dispatch = {
                r["task_id"]: r for r in await devin_dispatch_mod.tasks()
            }
        except Exception as exc:
            logger.info("devin_job_report: dispatch status unavailable: %s", exc)
        buckets: dict[str, list[dict[str, Any]]] = {
            "active": [], "failed": [], "done": [], "stale": [],
        }
        for doc in docs:
            bucket, job = self._classify_job(doc, dispatch)
            buckets[bucket].append(job)
        for jobs in buckets.values():
            jobs.sort(key=lambda j: j.get("ts") or "", reverse=True)

        updated = datetime.now(timezone.utc).isoformat(timespec="seconds")
        lines = [
            "# Devin Job Report", "",
            f"*Updated {updated} — composed from the devin-handoff job ledger.*",
            "",
        ]

        def _item(j: dict[str, Any], bucket: str) -> str:
            host = f" ({j['host']})" if j.get("host") else ""
            detail = j.get("detail") or ""
            if j.get("question"):
                note = f"**needs input**: {j['question']}"
                if detail.lower().startswith("needs input"):
                    detail = ""
            elif bucket == "stale":
                # One status label is enough — job bodies carry long
                # transcript tails that just bury the section.
                note, detail = f"{j['status']}", ""
            elif j["status"] in {"running", "failed", "done"}:
                note = ""
            else:
                note = f"{j['status']}"
            tail = " — ".join(p for p in (note, detail) if p)
            return f"- `{j['task_id']}`{host}" + (f" — {tail}" if tail else "")

        for heading, key in (
            ("Active jobs", "active"), ("Failed jobs", "failed"),
            ("Done", "done"), ("Stale / superseded", "stale"),
        ):
            jobs = buckets[key]
            lines.append(f"## {heading} — {len(jobs)}")
            lines.append("")
            if jobs:
                lines.extend(_item(j, key) for j in jobs)
            else:
                lines.append("_None._")
            lines.append("")
        markdown = "\n".join(lines)

        result: dict[str, Any] = {
            "status": "composed",
            "slug": DEVIN_JOB_REPORT_SLUG,
            "counts": {k: len(v) for k, v in buckets.items()},
            "jobs": buckets,
            "markdown": markdown,
            "note": (
                "Publish via cms_publish_page (slug 'devin-job-report') or "
                "call again with publish=true and confirmed=true."
            ),
        }
        if publish:
            self._check_cms_write_allowed(
                "devin_read", {"slug": DEVIN_JOB_REPORT_SLUG},
                confirmed, confirm_token)
            counts = result["counts"]
            pub = await self.cms_publish_page(
                DEVIN_JOB_REPORT_SLUG, "Devin Job Report", markdown,
                summary=(f"{counts['active']} active, {counts['failed']} "
                         f"failed, {counts['done']} done, "
                         f"{counts['stale']} stale — Devin dispatch job "
                         "ledger"),
                domain="devin", fresh_for="1h", confidence="high")
            result["publish"] = pub
            result["status"] = pub.get("status", "error")
        return result

    async def devin_pending(self) -> list[dict[str, Any]]:
        """Dispatched jobs blocked waiting for a user answer.

        Reads job/<id> docs (kind=job, status=awaiting-user) from the
        devin-handoff collection — the same ledger the watch timer, job-run
        wrapper, and Report tab dispatch layer use.
        """
        if self.mddb is None:
            return []
        docs = await self.mddb.search_documents(
            DEVIN_JOBS_COLLECTION,
            filter_meta={"kind": ["job"], "status": ["awaiting-user"]},
            limit=20,
        )
        out = []
        for d in docs:
            meta = d.get("meta") or {}
            out.append({
                "task_id": _first(meta.get("job_id"))
                           or (d.get("key") or "").split("/", 1)[-1],
                "question": _first(meta.get("question")) or "",
                "host": _first(meta.get("host")),
                "ts": _first(meta.get("ts")),
                "detail": (d.get("contentMd") or "")[:600],
            })
        out.sort(key=lambda j: j.get("ts") or "", reverse=True)
        return out

    async def devin_jobs(self, status: str | None = None,
                         limit: int = 30) -> list[dict[str, Any]]:
        """Dispatch ledger: all job docs (running/done/failed/awaiting-user)
        — the summary path. devin_pending is awaiting-user only; use this
        for "what's the status of my tasks" questions."""
        if self.mddb is None:
            return []
        fm: dict[str, Any] = {"kind": ["job"]}
        if status:
            fm["status"] = [status]
        docs = await self.mddb.search_documents(
            DEVIN_JOBS_COLLECTION, filter_meta=fm, limit=min(limit, 50))
        out = []
        for d in docs:
            meta = d.get("meta") or {}
            out.append({
                "task_id": _first(meta.get("job_id"))
                           or (d.get("key") or "").split("/", 1)[-1],
                "status": _first(meta.get("status")),
                "host": _first(meta.get("host")),
                "ts": _first(meta.get("ts")),
                "question": _first(meta.get("question")) or "",
            })
        out.sort(key=lambda j: j.get("ts") or "", reverse=True)
        return out

    async def devin_answer(self, task_id: str, message: str) -> dict[str, Any]:
        """Deliver the user's refined answer to a blocked job.

        Writes answer/<task_id> to the ledger, flips job/<task_id> to
        answered, and for devin-dispatch task ids also injects the message
        straight into the session via devin_followup.
        """
        if self.mddb is None:
            raise PermissionError("devin_answer needs MDDB (unavailable in guest mode)")
        now = datetime.now(timezone.utc).isoformat()
        delivered = False
        via = "mailbox"
        # Devin-dispatch task ids (YYYYMMDD-HHMMSS-slug) resume in-place.
        if re.match(r"^\d{8}-\d{6}-[a-z0-9-]+$", task_id):
            res = await devin_dispatch_mod.followup(task_id, message)
            delivered = True
            via = "followup"
            logger.info("devin_answer: followup to %s -> %s", task_id, res)
        wrote = await self.mddb.add_document(
            DEVIN_JOBS_COLLECTION,
            key=f"answer/{task_id}",
            lang="en",
            content_md=f"Answer for job {task_id} ({now}):\n\n{message}",
            meta={
                "kind": ["job-answer"], "status": ["answered"],
                "job_id": [task_id], "ts": [now],
                "subject": [f"answer-{task_id}"],
                "source": ["voice"], "written_by": ["ada"],
                "scope": ["tony"], "bank": ["devin-handoff"],
            },
            durable=True, tool="devin_answer", session_id=self.session_id,
        )
        queued = write_outbox.is_queued(wrote)
        if queued:
            self._log_session_event(
                "write_queued", tool="devin_answer",
                key=f"answer/{task_id}")
        job = await self.mddb.get_document(DEVIN_JOBS_COLLECTION, f"job/{task_id}")
        if job:
            meta = dict(job.get("meta") or {})
            meta["status"] = ["answered"]
            meta["answered_at"] = [now]
            await self.mddb.update_document(
                DEVIN_JOBS_COLLECTION, f"job/{task_id}", meta=meta,
                durable=True, tool="devin_answer",
                session_id=self.session_id)
        elif queued:
            # Same outage took the read too — queue the status flip so the
            # replay re-reads and merges once mddb is back.
            write_outbox.enqueue(
                mddb=self.mddb, op="update", collection=DEVIN_JOBS_COLLECTION,
                key=f"job/{task_id}", lang="en",
                meta={"status": ["answered"], "answered_at": [now]},
                tool="devin_answer", session_id=self.session_id)
        note = ("Answer recorded" +
                (" and sent to the running session." if delivered
                 else " — the dispatcher picks it up."))
        if queued:
            # mddb is unreachable — the write is parked in the local outbox.
            # Say it plainly: NOT yet in the ledger, will land on retry.
            note = ("mddb is unreachable — the answer is queued in the local "
                    "write outbox and will land automatically within the "
                    "retry window" +
                    ("; it was already sent to the running session."
                     if delivered else "."))
        return {"task_id": task_id, "delivered": delivered, "via": via,
                "queued_for_retry": queued, "note": note}

    # Doc actions that never wrote state before the merge —
    # ada_doc_search/ada_doc_get were free reads.
    _DOC_READ_ACTIONS = frozenset({"search", "get"})
    async def ada_devteam_review(
        self, request: str, title: str = "",
    ) -> dict[str, Any]:
        """Dev-team spec review — the absorbed tools.d drop-in
        (tools-merge-devin-mcp). Ada describes a tool she wants; the
        devteam pipeline drafts a spec, runs the parallel expert panel
        (security/privacy, standards, QA, scope), revises it, and files
        the reviewed spec into the devin-handoff bank.

        Owner-only: the manifest's owner_only policy + secondary_allowed
        =false gates live here now that the drop-in wrapper is gone."""
        if self._is_secondary_turn():
            raise PermissionError(
                "dev-team reviews run on the owner's turn only")
        ident = self.policy_identity()
        policy = self.banks.policy_for(ident) or {}
        if not policy.get("full"):
            logger.warning(
                "denied devin_read review for identity %r: owner_only", ident)
            raise PermissionError(
                "devin_read action='review' is restricted to the owner's "
                "identities")
        request = (request or "").strip()
        if not request:
            return {"ok": False, "error": "request is required"}
        if self.mddb is None:
            return {"ok": False, "error": "mddb unavailable (guest mode)"}
        from backend import devteam
        result = await devteam.review(request)

        panel = result["panel"]
        lines = [
            f"# Dev-team review: {(title or request)[:80]}",
            "",
            f"- verdict: **{result['verdict']}**",
            f"- model: {result['model']}",
            f"- reviewed: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
            "",
            "## Panel",
        ]
        lines += [
            f"- **{r['role']}** — {r['verdict']}: {r['summary']}" +
            (f" ({'; '.join(r['comments'])})" if r["comments"] else "")
            for r in panel
        ]
        lines += ["", "## Revised spec", "", result["spec"]]

        now = datetime.now(timezone.utc)
        parts = re.findall(r"[a-z0-9]+", request.lower())[:5]
        slug = "-".join(parts) or "untitled"
        key = f"spec/{now.strftime('%Y%m%d-%H%M%S')}-{slug}"
        await self.mddb.add_document(
            DEVIN_JOBS_COLLECTION,
            key=key, lang="en", content_md="\n".join(lines),
            meta={
                "kind": ["spec-review"], "status": [result["verdict"]],
                "ts": [now.isoformat()], "source": ["voice"],
                "written_by": ["ada"], "scope": ["tony"],
                "bank": ["devin-handoff"],
                "subject": ["-".join(
                    re.findall(r"[a-z0-9]+", request.lower())[:8])
                    or "untitled"],
            },
        )
        return {
            "ok": True,
            "verdict": result["verdict"],
            "spec_key": key,
            "panel": {r["role"]: r["verdict"] for r in panel},
            "summary": " | ".join(
                f"{r['role']}: {r['summary']}" for r in panel)[:600],
        }
