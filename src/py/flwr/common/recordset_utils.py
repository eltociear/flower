# Copyright 2024 Flower Labs GmbH. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""RecordSet utilities."""


from typing import Dict, OrderedDict, Tuple, Union, cast, get_args

from .configsrecord import ConfigsRecord
from .metricsrecord import MetricsRecord
from .parametersrecord import Array, ParametersRecord
from .recordset import RecordSet
from .typing import (
    ConfigsRecordValues,
    EvaluateIns,
    EvaluateRes,
    FitIns,
    FitRes,
    GetParametersIns,
    GetParametersRes,
    GetPropertiesIns,
    GetPropertiesRes,
    MetricsRecordValues,
    Parameters,
    Scalar,
    Status,
)


def parametersrecord_to_parameters(
    record: ParametersRecord, keep_input: bool = False
) -> Parameters:
    """Convert ParameterRecord to legacy Parameters.

    Warning: Because `Arrays` in `ParametersRecord` encode more information of the
    array-like or tensor-like data (e.g their datatype, shape) than `Parameters` it
    might not be possible to reconstruct such data structures from `Parameters` objects
    alone. Additional information or metadta must be provided from elsewhere.

    Parameters
    ----------
    record : ParametersRecord
        The record to be conveted into Parameters.
    keep_input : bool (default: False)
        A boolean indicating whether entries in the record should be deleted from the
        input dictionary immediately after adding them to the record.
    """
    parameters = Parameters(tensors=[], tensor_type="")

    for key in list(record.data.keys()):
        parameters.tensors.append(record.data[key].data)

        if not keep_input:
            del record.data[key]

    return parameters


def parameters_to_parametersrecord(
    parameters: Parameters, keep_input: bool = False
) -> ParametersRecord:
    """Convert legacy Parameters into a single ParametersRecord.

    Because there is no concept of names in the legacy Parameters, arbitrary keys will
    be used when constructing the ParametersRecord. Similarly, the shape and data type
    won't be recorded in the Array objects.

    Parameters
    ----------
    parameters : Parameters
        Parameters object to be represented as a ParametersRecord.
    keep_input : bool (default: False)
        A boolean indicating whether parameters should be deleted from the input
        Parameters object (i.e. a list of serialized NumPy arrays) immediately after
        adding them to the record.
    """
    tensor_type = parameters.tensor_type

    p_record = ParametersRecord()

    num_arrays = len(parameters.tensors)
    for idx in range(num_arrays):
        if keep_input:
            tensor = parameters.tensors[idx]
        else:
            tensor = parameters.tensors.pop(0)
        p_record.set_parameters(
            OrderedDict(
                {str(idx): Array(data=tensor, dtype="", stype=tensor_type, shape=[])}
            )
        )

    return p_record


def _check_mapping_from_scalar_to_metricsrecordstypes(
    scalar_config: Dict[str, Scalar],
) -> Dict[str, MetricsRecordValues]:
    """."""
    for value in scalar_config.values():
        if not isinstance(value, get_args(MetricsRecordValues)):
            raise TypeError(
                f"Supported types are {MetricsRecordValues}. "
                f"But you used type: {type(value)}"
            )
    return cast(Dict[str, MetricsRecordValues], scalar_config)


def _check_mapping_from_scalar_to_configsrecordstypes(
    scalar_config: Dict[str, Scalar],
) -> Dict[str, ConfigsRecordValues]:
    """."""
    for value in scalar_config.values():
        if not isinstance(value, get_args(ConfigsRecordValues)):
            raise TypeError(
                f"Supported types are {ConfigsRecordValues}. "
                f"But you used type: {type(value)}"
            )
    return cast(Dict[str, ConfigsRecordValues], scalar_config)


def _check_mapping_from_recordscalartype_to_scalar(
    record_data: Dict[str, Union[ConfigsRecordValues, MetricsRecordValues]]
) -> Dict[str, Scalar]:
    """Check mapping `common.*RecordValues` into `common.Scalar` is possible."""
    for value in record_data.values():
        if not isinstance(value, get_args(Scalar)):
            raise TypeError(
                "There is not a 1:1 mapping between `common.Scalar` types and those "
                "supported in `common.ConfigsRecordValues` or "
                "`common.ConfigsRecordValues`. Consider casting your values to a type "
                "supported by the `common.RecordSet` infrastructure. "
                f"You used type: {type(value)}"
            )
    return cast(Dict[str, Scalar], record_data)


def _recordset_to_fit_or_evaluate_ins(
    recordset: RecordSet, ins_str: str
) -> Tuple[Parameters, Dict[str, Scalar]]:
    """Derive Fit/Evaluate Ins from a RecordSet."""
    # get Array and construct Parameters
    parameters_record = recordset.get_parameters(f"{ins_str}.parameters")

    parameters = parametersrecord_to_parameters(parameters_record)

    # get config dict
    config_record = recordset.get_configs(f"{ins_str}.config")

    config_dict = _check_mapping_from_recordscalartype_to_scalar(config_record.data)

    return parameters, config_dict


def _embed_status_into_recordset(
    res_str: str, status: Status, recordset: RecordSet
) -> RecordSet:
    status_dict: Dict[str, ConfigsRecordValues] = {
        "code": status.code.value,
        "message": status.message,
    }
    recordset.set_configs(f"{res_str}.status", record=ConfigsRecord(status_dict))
    return recordset


def recordset_to_fit_ins(recordset: RecordSet) -> FitIns:
    """Derive FitIns from a RecordSet object."""
    parameters, config = _recordset_to_fit_or_evaluate_ins(recordset, ins_str="fitins")

    return FitIns(parameters=parameters, config=config)


def fit_res_to_recordset(fitres: FitRes) -> RecordSet:
    """Construct a RecordSet from a FitRes object."""
    recordset = RecordSet()

    metrics = _check_mapping_from_scalar_to_metricsrecordstypes(fitres.metrics)
    recordset.set_metrics(name="fitres.metrics", record=MetricsRecord(metrics))
    recordset.set_metrics(
        name="fitres.num_examples",
        record=MetricsRecord({"num_examples": fitres.num_examples}),
    )
    recordset.set_parameters(
        name="fitres.parameters",
        record=parameters_to_parametersrecord(fitres.parameters),
    )

    # status
    recordset = _embed_status_into_recordset("fitres", fitres.status, recordset)

    return recordset


def recodset_to_evaluate_ins(recordset: RecordSet) -> EvaluateIns:
    """Derive EvaluateIns from a RecordSet object."""
    parameters, config = _recordset_to_fit_or_evaluate_ins(
        recordset, ins_str="evaluateins"
    )

    return EvaluateIns(parameters=parameters, config=config)


def evaluate_res_to_recordset(evaluateres: EvaluateRes) -> RecordSet:
    """Construct a RecordSet from a EvaluateRes object."""
    recordset = RecordSet()

    # loss
    recordset.set_metrics(
        name="evaluateres.loss",
        record=MetricsRecord({"loss": evaluateres.loss}),
    )

    # num_examples
    recordset.set_metrics(
        name="evaluateres.num_examples",
        record=MetricsRecord({"num_examples": evaluateres.num_examples}),
    )

    # metrics
    metrics = _check_mapping_from_scalar_to_metricsrecordstypes(evaluateres.metrics)
    recordset.set_metrics(name="evaluateres.metrics", record=MetricsRecord(metrics))

    # status
    recordset = _embed_status_into_recordset(
        "evaluateres", evaluateres.status, recordset
    )

    return recordset


def recordset_to_getparameters_ins(recordset: RecordSet) -> GetParametersIns:
    """Derive GetParametersIns from a RecordSet object."""
    config_record = recordset.get_configs("getparametersins.config")

    config_dict = _check_mapping_from_recordscalartype_to_scalar(config_record.data)

    return GetParametersIns(config=config_dict)


def getparameters_res_to_recordset(getparametersres: GetParametersRes) -> RecordSet:
    """Construct a RecordSet from a GetParametersRes object."""
    recordset = RecordSet()
    parameters_record = parameters_to_parametersrecord(getparametersres.parameters)
    recordset.set_parameters("getparametersres.parameters", parameters_record)

    # status
    recordset = _embed_status_into_recordset(
        "getparametersres", getparametersres.status, recordset
    )

    return recordset


def recordset_to_getproperties_ins(recordset: RecordSet) -> GetPropertiesIns:
    """Derive GetPropertiesIns from a RecordSet object."""
    config_record = recordset.get_configs("getpropertiesins.config")
    config_dict = _check_mapping_from_recordscalartype_to_scalar(config_record.data)

    return GetPropertiesIns(config=config_dict)


def getproperties_res_to_recorset(getpropertiesres: GetPropertiesRes) -> RecordSet:
    """Construct a RecordSet from a GetPropertiesRes object."""
    recordset = RecordSet()
    configs = _check_mapping_from_scalar_to_configsrecordstypes(
        getpropertiesres.properties
    )
    recordset.set_configs(
        name="gerpropertiesres.properties", record=ConfigsRecord(configs)
    )
    # status
    recordset = _embed_status_into_recordset(
        "getpropertiesres", getpropertiesres.status, recordset
    )

    return recordset
