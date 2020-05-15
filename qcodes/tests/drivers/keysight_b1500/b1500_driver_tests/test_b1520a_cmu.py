from unittest.mock import MagicMock, call
import numpy as np
import pytest

from qcodes.instrument_drivers.Keysight.keysightb1500 import constants
from qcodes.instrument_drivers.Keysight.keysightb1500.KeysightB1520A import \
    B1520A


# pylint: disable=redefined-outer-name

@pytest.fixture
def mainframe():
    yield MagicMock()


@pytest.fixture
def cmu(mainframe):
    slot_nr = 3
    cmu = B1520A(parent=mainframe, name='B1520A', slot_nr=slot_nr)
    # GroupParameter with initial values write at the init so reset the mock
    # to not count those write
    mainframe.reset_mock()

    yield cmu


def test_force_dc_voltage(cmu):
    mainframe = cmu.parent

    cmu.voltage_dc(10)

    mainframe.write.assert_called_once_with('DCV 3,10')


def test_force_ac_voltage(cmu):
    mainframe = cmu.parent

    cmu.voltage_ac(0.1)

    mainframe.write.assert_called_once_with('ACV 3,0.1')


def test_set_ac_frequency(cmu):
    mainframe = cmu.parent

    cmu.frequency(100e3)

    mainframe.write.assert_called_once_with('FC 3,100000.0')


def test_get_capacitance(cmu):
    mainframe = cmu.parent

    mainframe.ask.return_value = "NCC-1.45713E-06,NCY-3.05845E-03"

    assert pytest.approx((-1.45713E-06, -3.05845E-03)) == cmu.capacitance()


def test_raise_error_on_unsupported_result_format(cmu):
    mainframe = cmu.parent

    mainframe.ask.return_value = "NCR-1.1234E-03,NCX-4.5677E-03"

    with pytest.raises(ValueError):
        cmu.capacitance()


def test_ranging_mode(cmu):
    mainframe = cmu.parent

    cmu.ranging_mode(constants.RangingMode.AUTO)

    mainframe.write.assert_called_once_with('RC 3,0')


def test_sweep_auto_abort(cmu):
    mainframe = cmu.parent

    cmu.cv_sweep.sweep_auto_abort(constants.Abort.ENABLED)

    mainframe.write.assert_called_once_with("WMDCV 2")


def test_post_sweep_voltage_cond(cmu):
    mainframe = cmu.parent

    cmu.cv_sweep.post_sweep_voltage_condition(constants.WMDCV.Post.STOP)

    mainframe.write.assert_called_once_with("WMDCV 2,2")


def test_post_sweep_voltage_val_raise_warning_if_abort_not_set(cmu):
    pass


def test_cv_sweep_delay(cmu):
    mainframe = cmu.root_instrument

    mainframe.ask.return_value = "WTDCV0.0,0.0,0.0,0.0,0.0"

    cmu.cv_sweep.hold(1.0)
    cmu.cv_sweep.delay(1.0)

    mainframe.write.assert_has_calls([call("WTDCV 1.0,0.0,0.0,0.0,0.0"),
                                      call("WTDCV 1.0,1.0,0.0,0.0,0.0")])


def test_cmu_sweep_steps(cmu):
    mainframe = cmu.root_instrument
    mainframe.ask.return_value = "WDCV3,1,0.0,0.0,1"
    cmu.sweep_start(2.0)
    cmu.sweep_end(4.0)

    mainframe.write.assert_has_calls([call("WDCV 3,1,2.0,0.0,1"),
                                      call("WDCV 3,1,2.0,4.0,1")])


def test_cv_sweep_voltages(cmu):

    mainframe = cmu.root_instrument

    start = -1.0
    end = 1.0
    steps = 5
    return_string = f'WDCV3,1,{start},{end},{steps}'
    mainframe.ask.return_value = return_string

    cmu.sweep_start(start)
    cmu.sweep_end(end)
    cmu.sweep_steps(steps)
    voltages = cmu.cv_sweep_voltages()

    assert all([a == b for a, b in zip(np.linspace(start, end, steps),
                                       voltages)])


def test_sweep_modes(cmu):

    mainframe = cmu.root_instrument

    start = -1.0
    end = 1.0
    steps = 5
    mode = constants.SweepMode.LINEAR_TWO_WAY
    return_string = f'WDCV3,{mode},{start},{end},{steps}'
    mainframe.ask.return_value = return_string

    cmu.sweep_start(start)
    cmu.sweep_end(end)
    cmu.sweep_steps(steps)
    cmu.sweep_mode(mode)
    voltages = cmu.cv_sweep_voltages()

    assert all([a == b for a, b in zip((-1.0, 0.0, 1.0, 0.0, -1.0), voltages)])



def test_run_sweep(cmu):
    mainframe = cmu.root_instrument

    start = -1.0
    end = 1.0
    steps = 5

    return_string = f'WMDCV2,2;WTDCV0.00,0.0000,0.2250,0.0000,0.0000;WDCV3,' \
                    f'1,{start},{end},{steps}'
    mainframe.ask.return_value = return_string
    cmu.setup_fnc_already_run = True
    cmu.sweep_start(start)
    cmu.sweep_end(end)
    cmu.sweep_steps(steps)
    cmu.run_sweep()


def test_phase_compensation_mode(cmu):
    mainframe = cmu.parent

    cmu.phase_compensation_mode(constants.ADJ.Mode.MANUAL)

    mainframe.write.assert_called_once_with('ADJ 3,1')

    assert constants.ADJ.Mode.MANUAL == cmu.phase_compensation_mode()


def test_phase_compensation(cmu):
    mainframe = cmu.parent

    mainframe.ask.return_value = 0

    response = cmu.phase_compensation()

    mainframe.ask.assert_called_once_with('ADJ? 3')
    assert isinstance(response, constants.ADJQuery.Response)
    assert response == constants.ADJQuery.Response.PASSED


def test_phase_compensation_with_mode(cmu):
    mainframe = cmu.parent

    mainframe.ask.return_value = 0

    response = cmu.phase_compensation(constants.ADJQuery.Mode.USE_LAST)

    mainframe.ask.assert_called_once_with('ADJ? 3,0')
    assert isinstance(response, constants.ADJQuery.Response)
    assert response == constants.ADJQuery.Response.PASSED


def test_enable_correction(cmu):
    mainframe = cmu.parent

    cmu.correction.enable(constants.CalibrationType.OPEN)
    mainframe.write.assert_called_once_with('CORRST 3,1,1')

    mainframe.reset_mock()

    cmu.correction.enable(constants.CalibrationType.SHORT)
    mainframe.write.assert_called_once_with('CORRST 3,2,1')

    mainframe.reset_mock()

    cmu.correction.enable(constants.CalibrationType.LOAD)
    mainframe.write.assert_called_once_with('CORRST 3,3,1')


def test_disable_correction(cmu):
    mainframe = cmu.parent

    cmu.correction.disable(constants.CalibrationType.OPEN)
    mainframe.write.assert_called_once_with('CORRST 3,1,0')

    mainframe.reset_mock()

    cmu.correction.disable(constants.CalibrationType.SHORT)
    mainframe.write.assert_called_once_with('CORRST 3,2,0')

    mainframe.reset_mock()

    cmu.correction.disable(constants.CalibrationType.LOAD)
    mainframe.write.assert_called_once_with('CORRST 3,3,0')


def test_correction_is_enabled(cmu):
    mainframe = cmu.parent

    mainframe.ask.return_value = '1'

    response = cmu.correction.is_enabled(constants.CalibrationType.SHORT)
    assert response == constants.CORRST.Response.ON


def test_correction_set_reference_values(cmu):
    mainframe = cmu.parent

    cmu.correction.set_reference_values(
        constants.CalibrationType.OPEN,
        constants.DCORR.Mode.Cp_G,
        1,
        2)
    mainframe.write.assert_called_once_with('DCORR 3,1,100,1,2')


def test_correction_get_reference_values(cmu):
    mainframe = cmu.parent

    mainframe.ask.return_value = '100,0.001,2'
    response = 'Mode: Cp_G, Primary Cp: 0.001 F, Secondary G: 2.0 S'
    assert response == cmu.correction.get_reference_values(
        constants.CalibrationType.OPEN)


def test_clear_and_set_default_frequency_list_for_correction(cmu):
    mainframe = cmu.parent

    cmu.correction.frequency_list.clear_and_set_default()

    mainframe.write.assert_called_once_with('CLCORR 3,2')


def test_clear_frequency_list_for_correction(cmu):
    mainframe = cmu.parent

    cmu.correction.frequency_list.clear()

    mainframe.write.assert_called_once_with('CLCORR 3,1')


def test_add_frequency_for_correction(cmu):
    mainframe = cmu.parent

    cmu.correction.frequency_list.add(1000)

    mainframe.write.assert_called_once_with('CORRL 3,1000')


def test_query_from_frequency_list_for_correction(cmu):
    mainframe = cmu.parent

    mainframe.ask.return_value = '25'

    assert pytest.approx(25) == cmu.correction.frequency_list.query()
    mainframe.ask.assert_called_once_with('CORRL? 3')


def test_query_at_index_from_frequency_list_for_correction(cmu):
    mainframe = cmu.parent

    mainframe.ask.return_value = '1234.567'

    assert pytest.approx(1234.567) == cmu.correction.frequency_list.query(
        index=0)
    mainframe.ask.assert_called_once_with('CORRL? 3,0')


def test_perform_correction(cmu):
    mainframe = cmu.parent

    mainframe.ask.return_value = 0

    response = cmu.correction.perform(
        constants.CalibrationType.OPEN)
    assert constants.CORR.Response.SUCCESSFUL == response


def test_perform_and_enable_correction(cmu):
    mainframe = cmu.parent

    mainframe.ask.side_effect = [
        '0',  # for correction status
        '1'   # for correction state (enabled/disabled)
    ]

    response = cmu.correction.perform_and_enable(
        constants.CalibrationType.OPEN)

    expected_response = f'Correction status ' \
                        f'{constants.CORR.Response.SUCCESSFUL.name} and ' \
                        f'Enable ' \
                        f'{constants.CORRST.Response.ON.name}'
    assert response == expected_response


def test_abort(cmu):
    mainframe = cmu.parent

    cmu.abort()

    mainframe.write.assert_called_once_with('AB')
