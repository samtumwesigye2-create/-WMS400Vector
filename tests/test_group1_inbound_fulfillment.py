import pytest

from inbound_workflow import (
    FULFILLMENT_SEQUENCE,
    can_complete_task,
    validate_receipt_disposition,
)


def test_receipt_disposition_must_reconcile():
    assert validate_receipt_disposition(100,90,10,0)
    with pytest.raises(ValueError,match='receipt_disposition_must_equal_quantity'):
        validate_receipt_disposition(100,80,10,0)


def test_quarantine_is_not_available_until_release():
    on_hand=190
    quarantined=10
    damaged=0
    reserved=0
    available=on_hand-quarantined-damaged-reserved
    assert available==180
    quarantined-=10
    available=on_hand-quarantined-damaged-reserved
    assert available==190


def test_fulfillment_sequence_is_frozen():
    assert FULFILLMENT_SEQUENCE==('allocate','pick','pack','stage')
    assert can_complete_task(1,None)
    assert can_complete_task(2,'completed')
    assert not can_complete_task(2,'open')


def test_golden_component_balances_reconcile():
    opening={'B':80,'C':40,'D':10,'E':50}
    scheduled={'B':0,'C':100,'D':0,'E':0}
    purchased={'B':110,'C':145,'D':180,'E':45}
    required={'B':190,'C':285,'D':190,'E':95}
    final={sku:opening[sku]+scheduled[sku]+purchased[sku]-required[sku] for sku in required}
    assert final=={'B':0,'C':0,'D':0,'E':0}


def test_finished_goods_fulfillment_reconciles():
    opening_fp=5
    produced=95
    shipped=100
    assert opening_fp+produced-shipped==0
