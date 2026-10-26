"""The ``call`` channel executor + its concurrency check (A6). Importing this module
registers both with the A1 dispatcher and gate. The gate has passed when it runs."""
from __future__ import annotations

from api.agents import executors
from api.call_agent.service import has_capacity, place_call

executors.register("call", place_call)
executors.register_capacity("call", has_capacity)
