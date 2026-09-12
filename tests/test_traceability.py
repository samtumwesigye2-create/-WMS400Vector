from datetime import date
import pytest

from traceability import validate_trace_unit, recall_scope_matches


def test_batch_managed_material_requires_lot_code():
    with pytest.raises(ValueError, match='lot_code_required'):
        validate_trace_unit(True, False, None, [], 5, None, None)


def test_serial_managed_quantity_requires_unique_serial_per_unit():
    with pytest.raises(ValueError, match='serial_count_must_equal_quantity'):
        validate_trace_unit(False, True, None, ['SN-1'], 2, None, None)
    with pytest.raises(ValueError, match='duplicate_serial_number'):
        validate_trace_unit(False, True, None, ['SN-1','SN-1'], 2, None, None)


def test_expiration_cannot_precede_manufacture_date():
    with pytest.raises(ValueError, match='expiration_before_manufacture'):
        validate_trace_unit(False, False, None, [], 1, date(2026,9,12), date(2026,9,11))


def test_recall_scope_matches_lot_or_serial():
    row={'sku':'SKU-1','lot_code':'LOT-A','serial_number':'SN-9'}
    assert recall_scope_matches(row, sku='SKU-1', lot_code='LOT-A')
    assert recall_scope_matches(row, serial_number='SN-9')
    assert not recall_scope_matches(row, lot_code='LOT-B')
