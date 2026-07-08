"""Module Library: closed-world, typed activities. REGISTRY is the seam
(WizeMe or any host registers additional activities here; the Foundry
resolves signatures against it and can never hallucinate one)."""
from __future__ import annotations
from . import clinical

REGISTRY = {
    "extract_clinical_data": clinical.extract_clinical_data,
    "flag_for_review": clinical.flag_for_review,
}
