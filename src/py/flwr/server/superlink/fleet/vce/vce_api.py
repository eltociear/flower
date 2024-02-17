# Copyright 2023 Flower Labs GmbH. All Rights Reserved.
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
"""Fleet VirtualClientEngine API."""

import asyncio
import traceback
from logging import ERROR, INFO
from typing import Callable, Dict, Union

from flwr.client.clientapp import ClientApp, load_client_app
from flwr.client.message_handler.task_handler import configure_task_res
from flwr.client.node_state import NodeState
from flwr.common.logger import log
from flwr.common.message import Message, Metadata
from flwr.common.serde import message_to_taskres, recordset_from_proto
from flwr.proto.node_pb2 import Node
from flwr.proto.task_pb2 import TaskIns
from flwr.server.superlink.state import StateFactory
from flwr.simulation.ray_transport.ray_actor import (
    BasicActorPool,
    ClientAppActor,
    init_ray,
)

TaskInsQueue = asyncio.Queue[TaskIns]
NodeToPartitionMapping = Dict[int, int]


def _construct_ray_actor_pool(
    client_resources: Dict[str, Union[float, int]],
    wdir: str,
) -> BasicActorPool:
    """Prepare ActorPool."""
    # Init ray and append working dir if needed
    # Ref: https://docs.ray.io/en/latest/ray-core/handling-dependencies.html#api-reference
    runtime_env = {"working_dir": wdir} if wdir else None
    init_ray(
        include_dashboard=True, runtime_env=runtime_env
    )  # TODO: recursiviely search dir, we don't want that. use `excludes` arg
    # Create actor pool
    pool = BasicActorPool(
        actor_type=ClientAppActor,
        client_resources=client_resources,
    )
    return pool


def taskins_to_message(taskins: TaskIns, datapartition_id: int) -> Message:
    """Convert TaskIns to Messsage."""
    recordset = recordset_from_proto(taskins.task.recordset)

    return Message(
        content=recordset,
        metadata=Metadata(
            run_id=taskins.run_id,
            message_id=taskins.task_id,
            group_id=taskins.group_id,
            node_id=datapartition_id,
            ttl=taskins.task.ttl,
            message_type=taskins.task.task_type,
        ),
    )


def _register_nodes(
    num_nodes: int, state_factory: StateFactory
) -> NodeToPartitionMapping:
    nodes_mapping: NodeToPartitionMapping = {}
    for i in range(num_nodes):
        node_id = state_factory.state().create_node()
        nodes_mapping[node_id] = i
    return nodes_mapping


async def worker(
    app: Callable[[], ClientApp],
    queue: TaskInsQueue,
    node_states: Dict[int, NodeState],
    state_factory: StateFactory,
    nodes_mapping: NodeToPartitionMapping,
    pool: BasicActorPool,
) -> None:
    """Get TaskIns from queue and pass it to an actor in the pool to execute it."""
    while True:
        try:
            task_ins = await queue.get()

            # TODO: check if another request for the same node is being running atm
            # TODO: Else potential problesm with run_state ?

            assert pool.is_actor_available(), "This should never happen."

            node_id = task_ins.task.consumer.node_id

            # Register and retrive runstate
            node_states[node_id].register_context(run_id=task_ins.run_id)
            run_state = node_states[node_id].retrieve_context(run_id=task_ins.run_id)

            # Convert TaskIns to Message
            message = taskins_to_message(
                task_ins, datapartition_id=nodes_mapping[node_id]
            )

            # Submite a task to the pool
            future = await pool.submit_if_actor_is_free(
                lambda a, a_fn, mssg, cid, state: a.run.remote(a_fn, mssg, cid, state),
                (app, message, str(node_id), run_state),
            )

            assert (
                future is not None
            ), "this shouldn't happen given the check above, right?"
            # print(f"wait for {future = }")
            await asyncio.wait([future])
            # print(f"got: {future = }")

            # Fetch result
            out_mssg, updated_context = await pool.fetch_result_and_return_actor(future)

            # Update Context
            node_states[node_id].update_context(
                task_ins.run_id, context=updated_context
            )

            # TODO: can we avoid going to proto ? maybe with a new StateFactory + In-Memory Driver-SuperLink conn.
            # Convert to TaskRes
            task_res = message_to_taskres(out_mssg)
            # Configuring task
            task_res = configure_task_res(task_res, task_ins, Node(node_id=node_id))
            # Store TaskRes in state
            state_factory.state().store_task_res(task_res)

        except Exception as ex:
            # TODO: gen TaskRes with relevant error, add it to state_factory.state()
            log(ERROR, ex)
            log(ERROR, traceback.format_exc())
            break


async def generate_pull_requests(
    queue: TaskInsQueue,
    state_factory: StateFactory,
    nodes_mapping: NodeToPartitionMapping,
) -> None:
    """Generate TaskIns and add it to the queue."""
    while True:
        for node_id in nodes_mapping.keys():
            task_ins = state_factory.state().get_task_ins(node_id=node_id, limit=1)
            if task_ins:
                await queue.put(task_ins[0])
        log(INFO, f"TaskIns in queue: {queue.qsize()}")
        await asyncio.sleep(1.0)  # TODO: what's the right value here ?


async def run(
    app: Callable[[], ClientApp],
    working_dir: str,
    client_resources: Dict[str, Union[float, int]],
    nodes_mapping: NodeToPartitionMapping,
    state_factory: StateFactory,
    node_states: Dict[int, NodeState],
) -> None:
    """Run the VCE async."""
    queue: TaskInsQueue = asyncio.Queue(64)  # TODO: how to set?

    # Create actor pool
    log(INFO, f"{client_resources = }")
    pool = _construct_ray_actor_pool(client_resources, wdir=working_dir)
    # Adding actors to pool
    await pool.add_actors_to_pool(pool.actors_capacity)
    log(INFO, f"Constructed ActorPool with: {pool.num_actors} actors")

    worker_tasks = [
        asyncio.create_task(
            worker(app, queue, node_states, state_factory, nodes_mapping, pool)
        )
        for _ in range(pool.num_actors)
    ]
    asyncio.create_task(generate_pull_requests(queue, state_factory, nodes_mapping))
    await queue.join()
    await asyncio.gather(*worker_tasks)


def run_vce(
    num_supernodes: int,
    client_resources: Dict[str, Union[float, int]],
    client_app_str: str,
    working_dir: str,
    state_factory: StateFactory,
) -> None:
    """Run VirtualClientEnginge."""
    # Register nodes (as many as number of possible clients)
    # Each node has its own state
    node_states: Dict[int, NodeState] = {}
    nodes_mapping = _register_nodes(
        num_nodes=num_supernodes, state_factory=state_factory
    )
    for node_id in nodes_mapping.keys():
        node_states[node_id] = NodeState()

    log(INFO, f"Registered {len(nodes_mapping)} nodes")

    log(INFO, f"{client_app_str = }")

    def _load() -> ClientApp:
        app: ClientApp = load_client_app(client_app_str)
        return app

    app = _load

    asyncio.run(
        run(
            app,
            working_dir,
            client_resources,
            nodes_mapping,
            state_factory,
            node_states,
        )
    )
