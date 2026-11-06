"""F4: Was die Firmware nicht über den Bus herausgibt oder nicht prüft, wird nicht schreibbar."""

import pytest

from gateway import safety
from gateway.devices.fw_numbers import NUMBERS


@pytest.mark.parametrize("name", ["P_WRSDBUS", "P_NACHLEG_KG", "P_NACHLEGEM"])
@pytest.mark.parametrize("agg", [0x0000, 0x0100, 0x0300])
@pytest.mark.parametrize("regler_typ", [0x0B00, 0x0A01, 0x0D00, None])
def test_f4_locked_all_aggregates(name, agg, regler_typ):
    assert safety.param_lock_reason("can", regler_typ, NUMBERS[name] | agg)
