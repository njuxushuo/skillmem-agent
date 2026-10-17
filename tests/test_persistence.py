"""SQLite atomicity, crash boundaries and real subprocess restart checks (no network)."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from conftest import ScriptedToolModel, call
from langchain_core.messages import AIMessage

from support_agent.service import SupportService, default_data_dir
from support_agent.store import Store


def child(db, body):
    script = (
        """
import runpy
runpy.run_path('scripts/run_offline.py')['configure_offline']()
import json,sys
from support_agent.service import SupportService
s=SupportService(db_path=sys.argv[1], variant='B1')
"""
        + body
        + "\ns.close()\n"
    )
    return subprocess.run(
        [sys.executable, "-c", script, str(db)], text=True, capture_output=True, timeout=20, check=True
    )


def test_real_process_restart_preserves_refund_cancel_and_ticket(tmp_path):
    db = tmp_path / "business.sqlite"
    first = child(
        db,
        """
s.chat('C100','Refund order 1005','refund')
s.chat('C100','Cancel order 1004','cancel')
s.chat('C100','I want a person','ticket')
print(json.dumps({'refunds':len(s.store.refunds),'cancelled':s.store.orders['1004'].status,'tickets':len(s.store.tickets)}))
""",
    )
    second = child(
        db,
        """
s.chat('C100','Refund order 1005','new-thread')
print(json.dumps({'refunds':len(s.store.refunds),'cancelled':s.store.orders['1004'].status,'tickets':len(s.store.tickets)}))
""",
    )
    assert json.loads(first.stdout) == json.loads(second.stdout) == dict(refunds=1, cancelled="cancelled", tickets=1)


def test_real_process_restart_resumes_pending_approval_and_rejects_duplicate_payment(tmp_path):
    db = tmp_path / "approval.sqlite"
    child(
        db,
        "assert s.chat('C100','Refund order 1001, damaged','t').status == 'awaiting_approval'\n"
        "assert not s.store.refunds",
    )
    done = child(
        db,
        """
assert s.pending_approvals()[0]['state']=='pending'
a=s.resolve_approval('t',True,'reviewer')
b=s.resolve_approval('t',True,'reviewer')
assert a==b
print(json.dumps({'status':a.status,'refunds':len(s.store.refunds),'pending':len(s.pending_approvals())}))
""",
    )
    assert json.loads(done.stdout) == dict(status="answered", refunds=1, pending=0)


def test_process_crash_after_refund_commit_before_graph_completion(tmp_path):
    db = tmp_path / "crash.sqlite"
    child(db, "assert s.chat('C100','Refund order 1001, damaged','t').status == 'awaiting_approval'")
    script = """
import os,runpy,sys
runpy.run_path('scripts/run_offline.py')['configure_offline']()
from contextlib import contextmanager
from support_agent.service import SupportService
s=SupportService(db_path=sys.argv[1],variant='B1')
original=s.store.transaction
@contextmanager
def crash(*args,**kwargs):
    with original(*args,**kwargs):
        yield
    if s.store.refunds:
        os._exit(73)
s.store.transaction=crash
s.resolve_approval('t',True,'reviewer')
"""
    killed = subprocess.run([sys.executable, "-c", script, str(db)], capture_output=True, timeout=20)
    assert killed.returncode == 73
    done = child(
        db,
        """
assert len(s.store.refunds)==1
assert s.pending_approvals()[0]['state']=='resolving'
r=s.resolve_approval('t',True,'reviewer')
assert r.status=='answered'
assert len(s.store.refunds)==1 and not s.pending_approvals()
assert s.control.counts('t')['tool_calls'] <= 8
print('recovered-once')
""",
    )
    assert done.stdout.strip() == "recovered-once"


def test_refund_and_receipt_rollback_together(tmp_path, monkeypatch):
    db = str(tmp_path / "rollback.sqlite")
    model = ScriptedToolModel(replies=[call("request_refund", order_id="1005", amount=18, reason="broken")])
    service = SupportService(model=model, db_path=db, variant="B1")
    try:
        original = service.store.remember

        def fail(*args):
            original(*args)
            raise RuntimeError("injected before commit")

        monkeypatch.setattr(service.store, "remember", fail)
        assert service.chat("C100", "refund", "t").status == "failed"
        assert service.store.refunds == [] and service.store.orders["1005"].refunded == 0
        assert service.store._db.execute("SELECT COUNT(*) FROM business_operations").fetchone()[0] == 0
    finally:
        service.close()
    restarted = SupportService(db_path=db, variant="B1")
    try:
        assert not restarted.store.refunds
        assert restarted.chat("C100", "Refund order 1005", "fresh").status == "answered"
        assert len(restarted.store.refunds) == 1
    finally:
        restarted.close()


def test_checkpoint_save_failure_after_payment_replays_receipt(tmp_path, monkeypatch):
    service = SupportService(db_path=str(tmp_path / "checkpoint.sqlite"), variant="B1")
    saver = service.graph.checkpointer
    original = saver.put
    failed = False

    def fail(*args, **kwargs):
        nonlocal failed
        if service.store.refunds and not failed:
            failed = True
            raise OSError("checkpoint write fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(saver, "put", fail)
    try:
        with pytest.raises(OSError, match="checkpoint"):
            service.chat("C100", "Refund order 1005", "t")
        assert len(service.store.refunds) == 1
        monkeypatch.setattr(saver, "put", original)
        result = service.retry_turn("C100", "t")
        assert result.status == "answered" and len(service.store.refunds) == 1
        assert service.store.orders["1005"].refunded == Decimal("18")
    finally:
        service.close()


def test_concurrent_different_sessions_same_order_have_one_operation(tmp_path):
    db = str(tmp_path / "race.sqlite")

    def model():
        return ScriptedToolModel(
            replies=[call("request_refund", order_id="1005", amount=18, reason="broken"), AIMessage("done")]
        )

    services = [SupportService(model=model(), db_path=db, variant="B1") for _ in range(2)]
    try:
        with ThreadPoolExecutor(2) as pool:
            jobs = [pool.submit(s.chat, "C100", "refund", f"thread-{i}") for i, s in enumerate(services)]
            assert all(j.result().status == "answered" for j in jobs)
        for s in services:
            s.store.refresh()
            assert len(s.store.refunds) == 1 and s.store.orders["1005"].refunded == Decimal("18")
        assert services[0].store._db.execute("SELECT COUNT(*) FROM business_operations").fetchone()[0] == 1
    finally:
        for s in services:
            s.close()


def test_store_reloads_before_rejecting_stale_second_refund(tmp_path):
    db = str(tmp_path / "store.sqlite")
    stores = [Store.load(default_data_dir() / "store.json") for _ in range(2)]
    for s in stores:
        s.persist(db)
    try:
        old_order = stores[1].orders["1005"]
        stores[0].record_refund(stores[0].orders["1005"], Decimal("18"), "broken", "policy")
        with pytest.raises(ValueError, match="already"):
            stores[1].record_refund(old_order, Decimal("18"), "again", "policy")
        stores[1].refresh()
        assert len(stores[1].refunds) == 1
    finally:
        for s in stores:
            s.close()


def test_cancel_replay_is_persistent_and_has_one_receipt(tmp_path):
    db = str(tmp_path / "cancel.sqlite")

    def execute():
        s = SupportService(
            model=ScriptedToolModel(replies=[call("cancel_order", order_id="1004"), AIMessage("cancelled")]),
            db_path=db,
            variant="B1",
        )
        try:
            assert s.chat("C100", "cancel").status == "answered"
            assert s.store.orders["1004"].status == "cancelled"
            return s.store._db.execute("SELECT COUNT(*) FROM business_operations").fetchone()[0]
        finally:
            s.close()

    assert execute() == execute() == 1


@pytest.mark.parametrize("variant", ["B0-compatible", "B1"])
def test_databases_cannot_silently_mix_baselines(tmp_path, variant):
    db = str(tmp_path / "version.sqlite")
    service = SupportService(db_path=db, variant=variant)
    service.chat("C100", "Where is order 1002?", "t")
    service.close()
    other = "B1" if variant == "B0-compatible" else "B0-compatible"
    with pytest.raises(ValueError, match="B0"):
        SupportService(db_path=db, variant=other)


def test_receipt_primary_key_prevents_duplicate_sql_insert(tmp_path):
    import sqlite3

    s = Store.load(default_data_dir() / "store.json")
    s.persist(str(tmp_path / "unique.sqlite"))
    try:
        with s.transaction():
            s.remember("refund:C100:1005", {"amount": "18"}, {"status": "refunded"})
        with pytest.raises(sqlite3.IntegrityError), s.transaction():
            s.remember("refund:C100:1005", {"amount": "18"}, {"status": "refunded"})
        assert s._db.execute("SELECT COUNT(*) FROM business_operations").fetchone()[0] == 1
    finally:
        s.close()
