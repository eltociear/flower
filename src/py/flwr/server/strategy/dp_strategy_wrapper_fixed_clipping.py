# Copyright 2020 Flower Labs GmbH. All Rights Reserved.
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
"""Central differential privacy with fixed clipping.

Papers: https://arxiv.org/pdf/1712.07557.pdf, https://arxiv.org/pdf/1710.06963.pdf
"""
from typing import Dict, List, Optional, Tuple, Union

import numpy as np

from flwr.common import (
    EvaluateIns,
    EvaluateRes,
    FitIns,
    FitRes,
    NDArrays,
    Parameters,
    Scalar,
    ndarrays_to_parameters,
    parameters_to_ndarrays,
)
from flwr.common.differential_privacy import (
    add_noise_to_params,
    clip_inputs,
    compute_stdv,
)
from flwr.server.client_manager import ClientManager
from flwr.server.client_proxy import ClientProxy
from flwr.server.strategy.strategy import Strategy


class DPStrategyWrapperServerSideFixedClipping(Strategy):
    """Wrapper for Configuring a Strategy for Central DP with Server Side Fixed
    Clipping.

    Parameters
    ----------
    strategy: Strategy
        The strategy to which DP functionalities will be added by this wrapper.
    noise_multiplier: float
        The noise multiplier for the Gaussian mechanism for model updates.
        A value of 1.0 or higher is recommended for strong privacy.
    clipping_norm: float
        The value of the clipping norm.
    num_sampled_clients: int
        The number of clients that are sampled on each round.
    """

    # pylint: disable=too-many-arguments,too-many-instance-attributes
    def __init__(
        self,
        strategy: Strategy,
        noise_multiplier: float,
        clipping_norm: float,
        num_sampled_clients: int,
    ) -> None:
        super().__init__()

        self.strategy = strategy

        if noise_multiplier < 0:
            raise ValueError("The noise multiplier should be a non-negative value.")

        if clipping_norm <= 0:
            raise ValueError("The clipping threshold should be a positive value.")

        if num_sampled_clients <= 0:
            raise ValueError(
                "The number of sampled clients should be a positive value."
            )

        self.noise_multiplier = noise_multiplier
        self.clipping_norm = clipping_norm
        self.num_sampled_clients = num_sampled_clients

        self.current_round_params: NDArrays = []

    def __repr__(self) -> str:
        """Compute a string representation of the strategy."""
        rep = "DP Strategy Wrapper with Fixed Clipping"
        return rep

    def initialize_parameters(
        self, client_manager: ClientManager
    ) -> Optional[Parameters]:
        """Initialize global model parameters using given strategy."""
        return self.strategy.initialize_parameters(client_manager)

    def configure_fit(
        self, server_round: int, parameters: Parameters, client_manager: ClientManager
    ) -> List[Tuple[ClientProxy, FitIns]]:
        """Configure the next round of training."""
        self.current_round_params = parameters_to_ndarrays(parameters)
        return self.strategy.configure_fit(server_round, parameters, client_manager)

    def configure_evaluate(
        self, server_round: int, parameters: Parameters, client_manager: ClientManager
    ) -> List[Tuple[ClientProxy, EvaluateIns]]:
        """Configure the next round of evaluation."""
        return self.strategy.configure_evaluate(
            server_round, parameters, client_manager
        )

    def aggregate_fit(
        self,
        server_round: int,
        results: List[Tuple[ClientProxy, FitRes]],
        failures: List[Union[Tuple[ClientProxy, FitRes], BaseException]],
    ) -> Tuple[Optional[Parameters], Dict[str, Scalar]]:
        """Compute the updates, clip them, and pass them to the child strategy for
        aggregation.

        Afterward, add noise to the aggregated parameters.
        """
        if failures:
            return None, {}

        # Extract all clients' model params
        clients_params = [
            parameters_to_ndarrays(fit_res.parameters) for _, fit_res in results
        ]

        # Compute the updates
        all_clients_updates = self._compute_model_updates(clients_params)

        # Clip updates
        for client_update in all_clients_updates:
            client_update = clip_inputs(client_update, self.clipping_norm)

        # Compute the new parameters with the clipped updates
        for client_param, client_update in zip(clients_params, all_clients_updates):
            self._update_clients_params(client_param, client_update)

        # Update the results with the new params
        for res, params in zip(results, clients_params):
            res[1].parameters = ndarrays_to_parameters(params)

        # Pass the new parameters for aggregation
        aggregated_params, metrics = self.strategy.aggregate_fit(
            server_round, results, failures
        )

        # Add Gaussian noise to the aggregated parameters
        if aggregated_params:
            aggregated_params = add_noise_to_params(
                aggregated_params,
                compute_stdv(
                    self.noise_multiplier, self.clipping_norm, self.num_sampled_clients
                ),
            )

        return aggregated_params, metrics

    def aggregate_evaluate(
        self,
        server_round: int,
        results: List[Tuple[ClientProxy, EvaluateRes]],
        failures: List[Union[Tuple[ClientProxy, EvaluateRes], BaseException]],
    ) -> Tuple[Optional[float], Dict[str, Scalar]]:
        """Aggregate evaluation losses using the given strategy."""
        return self.strategy.aggregate_evaluate(server_round, results, failures)

    def evaluate(
        self, server_round: int, parameters: Parameters
    ) -> Optional[Tuple[float, Dict[str, Scalar]]]:
        """Evaluate model parameters using an evaluation function from the strategy."""
        return self.strategy.evaluate(server_round, parameters)

    def _compute_model_updates(
        self, all_clients_params: List[NDArrays]
    ) -> List[NDArrays]:
        """Compute model updates for each client based on the current round
        parameters."""
        all_client_updates = []
        for client_param in all_clients_params:
            client_update = [
                np.subtract(x, y)
                for (x, y) in zip(client_param, self.current_round_params)
            ]
            all_client_updates.append(client_update)
        return all_client_updates

    def _update_clients_params(
        self, client_param: NDArrays, client_update: NDArrays
    ) -> None:
        """Update the client parameters based on the model updates."""
        for i, _ in enumerate(self.current_round_params):
            client_param[i] = self.current_round_params[i] + client_update[i]

class DPStrategyWrapperClientSideFixedClipping(Strategy):
    """Wrapper for Configuring a Strategy for Central DP.

        The clipping is at the client side.

        Parameters
        ----------
        strategy: Strategy
            The strategy to which DP functionalities will be added by this wrapper.
        noise_multiplier: float
            The noise multiplier for the Gaussian mechanism for model updates.
            A value of 1.0 or higher is recommended for strong privacy.
        clipping_threshold: float
            The value of the clipping threshold.
        num_sampled_clients: int
            The number of clients that are sampled on each round.
        """

    # pylint: disable=too-many-arguments,too-many-instance-attributes
    def __init__(
            self,
            strategy: Strategy,
            noise_multiplier: float,
            clipping_threshold: float,
            num_sampled_clients: int,
    ) -> None:
        super().__init__()

        self.strategy = strategy

        if noise_multiplier < 0:
            raise Exception("The noise multiplier should be a non-negative value.")

        if clipping_threshold <= 0:
            raise Exception("The clipping threshold should be a positive value.")

        if num_sampled_clients <= 0:
            raise Exception("The number of sampled clients should be a positive value.")

        self.noise_multiplier = noise_multiplier
        self.clipping_threshold = clipping_threshold
        self.num_sampled_clients = num_sampled_clients

        self.current_round_params: NDArrays = []

