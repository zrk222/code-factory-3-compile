import time, pytest
from hsf.context.lsp_resolver import resolve_signatures, UnknownActivity
from hsf.context.assembler import assemble_context, doctrine_hash

def test_lsp_resolves_all_activity_signatures_under_100ms():
    t0 = time.perf_counter()
    sigs = resolve_signatures(["extract_clinical_data", "flag_for_review"])
    assert (time.perf_counter() - t0) < 0.1
    assert sigs["extract_clinical_data"].startswith("extract_clinical_data(text:")

def test_unknown_activity_blocks_compilation():
    with pytest.raises(UnknownActivity):
        resolve_signatures(["hallucinated_api_call"])

def test_context_assembles_in_fixed_layer_order():
    parts = [n for n, _ in assemble_context()["parts"]]
    kinds = [p.split("/")[0] for p in parts]
    assert kinds == sorted(kinds, key=["concepts", "contracts", "templates"].index)

def test_doctrine_hash_stable():
    assert doctrine_hash() == doctrine_hash()
