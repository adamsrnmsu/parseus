from unittest.mock import patch

from parseus.webapp.registers import utility


def test_get_reg_state_back_populates_by_keyword_only():
    with (
        patch.object(utility, "get_modbus_registers", return_value=[]),
        patch.object(utility, "one_to_many", return_value=[]),
        patch.object(utility, "load_regstate") as load,
    ):
        utility.get_reg_state(index="i", dshost=None)
    args, kwargs = load.call_args
    assert len(args) == 1
    assert kwargs == {"back_populate": True}
