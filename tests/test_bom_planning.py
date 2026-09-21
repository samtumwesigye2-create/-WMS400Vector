import pytest

from manufacturing_planning import explode_bom, validate_bom_graph


def test_recursive_bom_explosion_preserves_genealogy_and_totals():
    rows=[
        {'parent_sku':'FP-1000','component_sku':'A','qty_per':1},
        {'parent_sku':'FP-1000','component_sku':'B','qty_per':2},
        {'parent_sku':'A','component_sku':'D','qty_per':2},
        {'parent_sku':'A','component_sku':'E','qty_per':1},
    ]
    result=explode_bom(rows,'FP-1000',95)
    req={x['component_sku']:x['gross_requirement'] for x in result['requirements']}
    assert req['A']==95
    assert req['B']==190
    assert req['D']==190
    assert req['E']==95
    assert any(x['path']==['FP-1000','A','D'] and x['level']==2 for x in result['genealogy'])


def test_bom_cycle_is_rejected():
    rows=[
        {'parent_sku':'A','component_sku':'B','qty_per':1},
        {'parent_sku':'B','component_sku':'A','qty_per':1},
    ]
    with pytest.raises(ValueError,match='circular_bom'):
        validate_bom_graph(rows)


def test_bom_self_reference_is_rejected():
    with pytest.raises(ValueError,match='bom_self_reference'):
        validate_bom_graph([{'parent_sku':'A','component_sku':'A','qty_per':1}])


def test_explosion_accumulates_shared_components_across_branches():
    rows=[
        {'parent_sku':'P','component_sku':'A','qty_per':2},
        {'parent_sku':'P','component_sku':'B','qty_per':3},
        {'parent_sku':'A','component_sku':'X','qty_per':4},
        {'parent_sku':'B','component_sku':'X','qty_per':5},
    ]
    result=explode_bom(rows,'P',10)
    req={x['component_sku']:x['gross_requirement'] for x in result['requirements']}
    assert req['X']==230
