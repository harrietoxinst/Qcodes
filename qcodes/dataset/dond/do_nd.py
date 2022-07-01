from __future__ import annotations

import logging
import time
from collections import defaultdict
from contextlib import ExitStack
from typing import Mapping, Sequence

import numpy as np
from tqdm.auto import tqdm
from typing_extensions import TypedDict

from qcodes import config
from qcodes.dataset.data_set_protocol import res_type
from qcodes.dataset.descriptions.detect_shapes import detect_shape_of_measurement
from qcodes.dataset.descriptions.versioning.rundescribertypes import Shapes
from qcodes.dataset.dond.do_nd_utils import (
    ActionsT,
    AxesTupleListWithDataSet,
    BreakConditionInterrupt,
    BreakConditionT,
    MultiAxesTupleListWithDataSet,
    ParamMeasT,
    _catch_interrupts,
    _handle_plotting,
    _register_actions,
    _register_parameters,
    _set_write_period,
)
from qcodes.dataset.experiment_container import Experiment
from qcodes.dataset.measurements import Measurement
from qcodes.dataset.threading import (
    SequentialParamsCaller,
    ThreadPoolParamsCaller,
    process_params_meas,
)
from qcodes.parameters import ParameterBase

from .sweeps import AbstractSweep

LOG = logging.getLogger(__name__)


class ParameterGroup(TypedDict):
    params: tuple[ParamMeasT, ...]
    meas_name: str
    measured_params: list[res_type]


class MultiSweep:
    def __init__(self, sweeps: Sequence[AbstractSweep]):
        # todo check that all sweeps are the same
        self._sweeps = tuple(sweeps)

    @property
    def sweeps(self) -> tuple[AbstractSweep, ...]:
        return self._sweeps

    def get_setpoints(self) -> np.ndarray:
        setpoints = np.zeros_like(self.sweeps[0].get_setpoints(), dtype=object)
        for i in range(len(setpoints)):
            setpoints[i] = tuple(sweep.get_setpoints()[i] for sweep in self._sweeps)
        return setpoints

    @property
    def num_points(self) -> int:
        return self.sweeps[0].num_points


class _Sweeper:
    def __init__(
        self,
        sweeps: Sequence[AbstractSweep | MultiSweep],
        additional_setpoints: Sequence[ParameterBase],
    ):
        _sweeps = []

        self._additional_setpoints = additional_setpoints
        self._nested_setpoints = self._make_nested_setpoints(sweeps)
        self._parameter_groups = self._make_parameter_groups(
            sweeps, additional_setpoints
        )
        for sweep in sweeps:
            if isinstance(sweep, AbstractSweep):
                _sweeps.append(sweep)
            else:
                _sweeps.extend(sweep.sweeps)
        self._sweeps = _sweeps
        self._shapes = self._make_shape(sweeps, additional_setpoints)

        self._post_delays = tuple(sweep.delay for sweep in sweeps)
        self._params_set = tuple(sweep.param for sweep in sweeps)
        self._post_actions = tuple(sweep.post_actions for sweep in sweeps)

    def _make_parameter_groups(
        self,
        sweeps: Sequence[AbstractSweep | MultiSweep],
        additional_setpoints: Sequence[ParameterBase],
    ) -> dict[str, list[ParameterBase]]:
        # todo this only supports one Multisweep

        ungrouped_sweeps = []
        grouped_sweeps: list[AbstractSweep] = []

        for sweep in sweeps:
            if isinstance(sweep, AbstractSweep):
                ungrouped_sweeps.append(sweep)
            else:
                grouped_sweeps.extend(sweep.sweeps)
        n_groups = len(grouped_sweeps) or 1

        groups: dict[str, list[ParameterBase]] = {
            f"group_{i}": [] for i in range(n_groups)
        }

        for sweep_or_multi_sweep in sweeps:
            if isinstance(sweep_or_multi_sweep, AbstractSweep):
                for group in groups.values():
                    group.append(sweep_or_multi_sweep.param)
            else:
                for i, sweep in enumerate(sweep_or_multi_sweep.sweeps):
                    groups[f"group_{i}"].append(sweep.param)

        for group in groups.values():
            group.extend(additional_setpoints)

        return groups

    def _make_nested_setpoints(
        self, sweeps: Sequence[AbstractSweep | MultiSweep]
    ) -> np.ndarray:
        """Create the cartesian product of all the setpoint values."""
        if len(sweeps) == 0:
            return np.array([[]])  # 0d sweep (do0d)
        setpoint_values = [sweep.get_setpoints() for sweep in sweeps]
        return self._flatten_setpoint_values(setpoint_values)

    @staticmethod
    def _flatten_setpoint_values(setpoint_values: Sequence[np.ndarray]) -> np.ndarray:
        setpoint_grids = np.meshgrid(*setpoint_values, indexing="ij")
        flat_setpoint_grids = [np.ravel(grid, order="C") for grid in setpoint_grids]
        new_flat_grids = []
        # todo this is not the greatest. The idea is that when setpoint elements
        # are tuples they will not be expanded by meshgrid so we can use them
        # directly as being sweept in parallel.
        for grid in flat_setpoint_grids:
            if grid.dtype == np.dtype("O"):
                n_params = len(grid[0])
                for i in range(n_params):
                    vals = np.array([grid[j][i] for j in range(len(grid))])
                    new_flat_grids.append(vals)
            else:
                new_flat_grids.append(grid)

        return np.vstack(new_flat_grids).T

    @property
    def nested_setpoints(self) -> np.ndarray:
        return self._nested_setpoints

    @property
    def all_setpoint_params(self) -> tuple[ParameterBase, ...]:
        return tuple(sweep.param for sweep in self._sweeps) + tuple(
            s for s in self._additional_setpoints
        )

    @property
    def sweep_groupes(self) -> dict[str, list[ParameterBase]]:
        return self._parameter_groups

    @staticmethod
    def _make_shape(
        sweeps: Sequence[AbstractSweep | MultiSweep],
        addtional_setpoints: Sequence[ParameterBase],
    ) -> tuple[int, ...]:
        loop_shape = tuple(sweep.num_points for sweep in sweeps) + tuple(
            1 for _ in addtional_setpoints
        )
        return loop_shape

    @property
    def shape(self) -> tuple[int, ...]:
        return self._shapes

    @property
    def post_delays(self) -> tuple[float, ...]:
        return self._post_delays

    @property
    def params_set(self) -> tuple[ParameterBase, ...]:
        return self._params_set

    @property
    def post_actions(self) -> tuple[ActionsT, ...]:
        return self._post_actions


class _Measurements:
    def __init__(
        self,
        measurement_name: str,
        params_meas: Sequence[ParamMeasT | Sequence[ParamMeasT]],
    ):
        (
            self._measured_all,
            self._grouped_parameters,
            self._measured_parameters,
        ) = _extract_paramters_by_type_and_group(measurement_name, params_meas)

    @property
    def measured_all(self) -> tuple[ParamMeasT, ...]:
        return self._measured_all

    @property
    def grouped_parameters(self) -> dict[str, ParameterGroup]:
        return self._grouped_parameters

    @property
    def measured_parameters(self) -> tuple[ParameterBase, ...]:
        return self._measured_parameters


def dond(
    *params: AbstractSweep | ParamMeasT | Sequence[ParamMeasT],
    write_period: float | None = None,
    measurement_name: str = "",
    exp: Experiment | Sequence[Experiment] | None = None,
    enter_actions: ActionsT = (),
    exit_actions: ActionsT = (),
    do_plot: bool | None = None,
    show_progress: bool | None = None,
    use_threads: bool | None = None,
    additional_setpoints: Sequence[ParameterBase] = tuple(),
    log_info: str | None = None,
    break_condition: BreakConditionT | None = None,
) -> AxesTupleListWithDataSet | MultiAxesTupleListWithDataSet:
    """
    Perform n-dimentional scan from slowest (first) to the fastest (last), to
    measure m measurement parameters. The dimensions should be specified
    as sweep objects, and after them the parameters to measure should be passed.

    Args:
        params: Instances of n sweep classes and m measurement parameters,
            e.g. if linear sweep is considered:

            .. code-block::

                LinSweep(param_set_1, start_1, stop_1, num_points_1, delay_1), ...,
                LinSweep(param_set_n, start_n, stop_n, num_points_n, delay_n),
                param_meas_1, param_meas_2, ..., param_meas_m

            If multiple DataSets creation is needed, measurement parameters should
            be grouped, so one dataset will be created for each group. e.g.:

            .. code-block::

                LinSweep(param_set_1, start_1, stop_1, num_points_1, delay_1), ...,
                LinSweep(param_set_n, start_n, stop_n, num_points_n, delay_n),
                [param_meas_1, param_meas_2], ..., [param_meas_m]

        write_period: The time after which the data is actually written to the
            database.
        measurement_name: Name of the measurement. This will be passed down to
            the dataset produced by the measurement. If not given, a default
            value of 'results' is used for the dataset.
        exp: The experiment to use for this measurement. If you create multiple
            measurements using groups you may also supply multiple experiments.
        enter_actions: A list of functions taking no arguments that will be
            called before the measurements start.
        exit_actions: A list of functions taking no arguments that will be
            called after the measurements ends.
        do_plot: should png and pdf versions of the images be saved and plots
            are shown after the run. If None the setting will be read from
            ``qcodesrc.json``
        show_progress: should a progress bar be displayed during the
            measurement. If None the setting will be read from ``qcodesrc.json``
        use_threads: If True, measurements from each instrument will be done on
            separate threads. If you are measuring from several instruments
            this may give a significant speedup.
        additional_setpoints: A list of setpoint parameters to be registered in
            the measurement but not scanned/swept-over.
        log_info: Message that is logged during the measurement. If None a default
            message is used.
        break_condition: Callable that takes no arguments. If returned True,
            measurement is interrupted.

    Returns:
        A tuple of QCoDeS DataSet, Matplotlib axis, Matplotlib colorbar. If
        more than one group of measurement parameters is supplied, the output
        will be a tuple of tuple(QCoDeS DataSet), tuple(Matplotlib axis),
        tuple(Matplotlib colorbar), in which each element of each sub-tuple
        belongs to one group, and the order of elements is the order of
        the supplied groups.
    """
    if do_plot is None:
        do_plot = config.dataset.dond_plot
    if show_progress is None:
        show_progress = config.dataset.dond_show_progress

    sweep_instances, params_meas = _parse_dond_arguments(*params)

    sweeper = _Sweeper(sweep_instances, additional_setpoints)

    measurements = _Measurements(measurement_name, params_meas)

    LOG.info(
        "Starting a doNd with scan with\n setpoints: %s,\n measuring: %s",
        sweeper.all_setpoint_params,
        measurements.measured_all,
    )
    LOG.debug(
        "Measured parameters have been grouped into:\n " "%s",
        {
            name: group["params"]
            for name, group in measurements.grouped_parameters.items()
        },
    )
    try:
        shapes: Shapes = detect_shape_of_measurement(
            measurements.measured_parameters, sweeper.shape
        )
        LOG.debug("Detected shapes to be %s", shapes)
    except TypeError:
        LOG.exception(
            f"Could not detect shape of {measurements.measured_parameters} "
            f"falling back to unknown shape."
        )
        shapes = None
    meas_list = _create_measurements_grouped(
        sweeper.sweep_groupes,
        enter_actions,
        exit_actions,
        exp,
        measurements.grouped_parameters,
        shapes,
        write_period,
        log_info,
    )

    datasets = []
    plots_axes = []
    plots_colorbar = []
    if use_threads is None:
        use_threads = config.dataset.use_threads

    params_meas_caller = (
        ThreadPoolParamsCaller(*measurements.measured_all)
        if use_threads
        else SequentialParamsCaller(*measurements.measured_all)
    )

    try:
        with _catch_interrupts() as interrupted, ExitStack() as stack, params_meas_caller as call_params_meas:
            datasavers = [stack.enter_context(measure.run()) for measure in meas_list]
            additional_setpoints_data = process_params_meas(additional_setpoints)
            previous_setpoints = np.empty(len(sweep_instances))
            for setpoints in tqdm(sweeper.nested_setpoints, disable=not show_progress):

                active_actions, delays = _select_active_actions_delays(
                    sweeper.post_actions,
                    sweeper.post_delays,
                    setpoints,
                    previous_setpoints,
                )
                previous_setpoints = setpoints

                param_set_list = []
                param_set_dict = defaultdict(list)
                for setpoint_param, setpoint, action, delay in zip(
                    sweeper.params_set,
                    setpoints,
                    active_actions,
                    delays,
                ):
                    _conditional_parameter_set(setpoint_param, setpoint)
                    for sweep_group_name, sweep_group in sweeper.sweep_groupes.items():
                        if setpoint_param in sweep_group:
                            param_set_dict[sweep_group_name].append(
                                (setpoint_param, setpoint)
                            )
                    param_set_list.append((setpoint_param, setpoint))
                    for act in action:
                        act()
                    time.sleep(delay)

                meas_value_pair = call_params_meas()
                for group in measurements.grouped_parameters.values():
                    group["measured_params"] = []
                    for measured in meas_value_pair:
                        if measured[0] in group["params"]:
                            group["measured_params"].append(measured)

                for ind, datasaver in enumerate(datasavers):
                    datasaver.add_result(
                        *param_set_dict[f"group_{ind}"],
                        *measurements.grouped_parameters[f"group_{ind}"][
                            "measured_params"
                        ],
                        *additional_setpoints_data,
                    )

                if callable(break_condition):
                    if break_condition():
                        raise BreakConditionInterrupt("Break condition was met.")
    finally:

        for datasaver in datasavers:
            ds, plot_axis, plot_color = _handle_plotting(
                datasaver.dataset, do_plot, interrupted()
            )
            datasets.append(ds)
            plots_axes.append(plot_axis)
            plots_colorbar.append(plot_color)

    if len(measurements.grouped_parameters) == 1:
        return datasets[0], plots_axes[0], plots_colorbar[0]
    else:
        return tuple(datasets), tuple(plots_axes), tuple(plots_colorbar)


def _parse_dond_arguments(
    *params: AbstractSweep | ParamMeasT | Sequence[ParamMeasT],
) -> tuple[list[AbstractSweep | MultiSweep], list[ParamMeasT | Sequence[ParamMeasT]]]:
    """
    Parse supplied arguments into sweep objects and measurement parameters
    and their callables.
    """
    sweep_instances: list[AbstractSweep | MultiSweep] = []
    params_meas: list[ParamMeasT | Sequence[ParamMeasT]] = []
    for par in params:
        if isinstance(par, AbstractSweep):
            sweep_instances.append(par)
        elif isinstance(par, MultiSweep):
            sweep_instances.append(par)
        else:
            params_meas.append(par)
    return sweep_instances, params_meas


def _conditional_parameter_set(
    parameter: ParameterBase,
    value: float | complex,
) -> None:
    """
    Reads the cache value of the given parameter and set the parameter to
    the given value if the value is different from the cache value.
    """
    if value != parameter.cache.get():
        parameter.set(value)


def _make_nested_setpoints(sweeps: Sequence[AbstractSweep]) -> np.ndarray:
    """Create the cartesian product of all the setpoint values."""
    if len(sweeps) == 0:
        return np.array([[]])  # 0d sweep (do0d)
    setpoint_values = [sweep.get_setpoints() for sweep in sweeps]
    setpoint_grids = np.meshgrid(*setpoint_values, indexing="ij")
    flat_setpoint_grids = [np.ravel(grid, order="C") for grid in setpoint_grids]
    return np.vstack(flat_setpoint_grids).T


def _select_active_actions_delays(
    actions: Sequence[ActionsT],
    delays: Sequence[float],
    setpoints: np.ndarray,
    previous_setpoints: np.ndarray,
) -> tuple[list[ActionsT], list[float]]:
    """
    Select ActionT (Sequence[Callable]) and delays(Sequence[float]) from
    a Sequence of ActionsT and delays, respectively, if the corresponding
    setpoint has changed. Otherwise, select an empty Sequence for actions
    and zero for delays.
    """
    actions_list: list[ActionsT] = [()] * len(setpoints)
    setpoints_delay: list[float] = [0] * len(setpoints)
    for ind, (new_setpoint, old_setpoint) in enumerate(
        zip(setpoints, previous_setpoints)
    ):
        if new_setpoint != old_setpoint:
            actions_list[ind] = actions[ind]
            setpoints_delay[ind] = delays[ind]
    return (actions_list, setpoints_delay)


def _create_measurements(
    all_setpoint_params: Sequence[ParameterBase],
    enter_actions: ActionsT,
    exit_actions: ActionsT,
    experiments: Experiment | Sequence[Experiment] | None,
    grouped_parameters: Mapping[str, ParameterGroup],
    shapes: Shapes,
    write_period: float | None,
    log_info: str | None,
) -> tuple[Measurement, ...]:
    meas_list: list[Measurement] = []
    if log_info is not None:
        _extra_log_info = log_info
    else:
        _extra_log_info = "Using 'qcodes.dataset.dond'"

    if not isinstance(experiments, Sequence):
        experiments_internal: Sequence[Experiment | None] = [
            experiments for _ in grouped_parameters
        ]
    else:
        experiments_internal = experiments

    if len(experiments_internal) != len(grouped_parameters):
        raise ValueError(
            f"Inconsistent number of "
            f"parameter groups and experiments "
            f"got {len(grouped_parameters)} and {len(experiments_internal)}"
        )

    for group, exp in zip(grouped_parameters.values(), experiments_internal):
        meas_name = group["meas_name"]
        meas_params = group["params"]
        meas = Measurement(name=meas_name, exp=exp)
        meas._extra_log_info = _extra_log_info
        _register_parameters(meas, all_setpoint_params)
        _register_parameters(
            meas, meas_params, setpoints=all_setpoint_params, shapes=shapes
        )
        _set_write_period(meas, write_period)
        _register_actions(meas, enter_actions, exit_actions)
        meas_list.append(meas)
    return tuple(meas_list)


def _create_measurements_grouped(
    setpoint_groups: Mapping[str, Sequence[ParameterBase]],
    enter_actions: ActionsT,
    exit_actions: ActionsT,
    experiments: Experiment | Sequence[Experiment] | None,
    grouped_parameters: Mapping[str, ParameterGroup],
    shapes: Shapes,
    write_period: float | None,
    log_info: str | None,
) -> tuple[Measurement, ...]:
    meas_list: list[Measurement] = []
    if log_info is not None:
        _extra_log_info = log_info
    else:
        _extra_log_info = "Using 'qcodes.dataset.dond'"

    if not isinstance(experiments, Sequence):
        experiments_internal: Sequence[Experiment | None] = [
            experiments for _ in grouped_parameters
        ]
    else:
        experiments_internal = experiments

    if len(experiments_internal) != len(grouped_parameters):
        raise ValueError(
            f"Inconsistent number of "
            f"parameter groups and experiments "
            f"got {len(grouped_parameters)} and {len(experiments_internal)}"
        )

    if len(setpoint_groups) == 1:
        setpoint_groups = {
            name: list(setpoint_groups.values())[0]
            for name in grouped_parameters.keys()
        }

    if len(setpoint_groups) != len(grouped_parameters):
        raise ValueError(
            f"Inconsistent number of "
            f"parameter groups and setpoint groups "
            f"got {len(grouped_parameters)} and {len(setpoint_groups)}"
        )

    for group_name, exp in zip(grouped_parameters.keys(), experiments_internal):
        meas_name = grouped_parameters[group_name]["meas_name"]
        meas_params = grouped_parameters[group_name]["params"]
        meas = Measurement(name=meas_name, exp=exp)
        meas._extra_log_info = _extra_log_info
        _register_parameters(meas, setpoint_groups[group_name])
        _register_parameters(
            meas, meas_params, setpoints=setpoint_groups[group_name], shapes=shapes
        )
        _set_write_period(meas, write_period)
        _register_actions(meas, enter_actions, exit_actions)
        meas_list.append(meas)
    return tuple(meas_list)


def _extract_paramters_by_type_and_group(
    measurement_name: str,
    params_meas: Sequence[ParamMeasT | Sequence[ParamMeasT]],
) -> tuple[
    tuple[ParamMeasT, ...], dict[str, ParameterGroup], tuple[ParameterBase, ...]
]:
    measured_parameters: list[ParameterBase] = []
    measured_all: list[ParamMeasT] = []
    single_group: list[ParamMeasT] = []
    multi_group: list[Sequence[ParamMeasT]] = []
    grouped_parameters: dict[str, ParameterGroup] = {}
    for param in params_meas:
        if not isinstance(param, Sequence):
            single_group.append(param)
            measured_all.append(param)
            if isinstance(param, ParameterBase):
                measured_parameters.append(param)
        elif not isinstance(param, str):
            multi_group.append(param)
            for nested_param in param:
                measured_all.append(nested_param)
                if isinstance(nested_param, ParameterBase):
                    measured_parameters.append(nested_param)
    if single_group:
        pg: ParameterGroup = {
            "params": tuple(single_group),
            "meas_name": measurement_name,
            "measured_params": [],
        }
        grouped_parameters["group_0"] = pg
    if multi_group:
        for index, par in enumerate(multi_group):
            pg = {
                "params": tuple(par),
                "meas_name": measurement_name,
                "measured_params": [],
            }
            grouped_parameters[f"group_{index}"] = pg
    return tuple(measured_all), grouped_parameters, tuple(measured_parameters)
