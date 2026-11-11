"""G-5: a worker whose capture_jobs lease expired cannot complete or requeue a job another worker re-leased."""
from __future__ import annotations

import hashlib
import time

import pytest

from memorymaster.capture.repository import CaptureRepository, LeaseLost
from memorymaster.core.service import MemoryService


@pytest.fixture()
def repo(tmp_path):
    svc = MemoryService(tmp_path / "g5.db", workspace_root=tmp_path)
    svc.init_db()
    r = CaptureRepository(svc.store)
    orig = svc.store.connect

    def _nofk():  # fixture shortcut: no real source_items row needed to exercise the lease SQL
        c = orig()
        c.execute("PRAGMA foreign_keys=OFF")
        return c

    svc.store.connect = _nofk
    return r, svc


def _row(svc, job_id):
    return tuple(svc.store.connect().execute("select status, lease_owner from capture_jobs where id=?", (job_id,)).fetchone())


def test_a_stale_worker_cannot_finish_a_re_leased_job(repo):
    r, svc = repo
    job, _ = r.queue_job(source_item_id=1, content_hash=hashlib.sha256(b"x").hexdigest(), stage="extract_claims")
    a = r.lease_jobs(owner="worker-A", limit=1, lease_seconds=1)[0]
    time.sleep(2.2)
    b = r.lease_jobs(owner="worker-B", limit=1, lease_seconds=300)[0]
    assert b.id == a.id
    with pytest.raises(LeaseLost):
        r.finish_job(a.id, lease=a, status="completed")
    assert _row(svc, job.id) == ("leased", "worker-B")
    done = r.finish_job(b.id, lease=b, status="completed")
    assert done.status == "completed"
    # a finished job cannot be resurrected, by anyone
    with pytest.raises(LeaseLost):
        r.finish_job(b.id, lease=b, status="retryable", error_code="x")
    with pytest.raises(LeaseLost):
        r.finish_job(a.id, lease=a, status="retryable", error_code="x")
    assert _row(svc, job.id)[0] == "completed"
