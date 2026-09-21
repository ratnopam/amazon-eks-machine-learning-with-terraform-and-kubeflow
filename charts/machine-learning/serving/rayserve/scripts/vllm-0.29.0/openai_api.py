import json
import os
from argparse import Namespace
from contextlib import asynccontextmanager
from dataclasses import is_dataclass

from fastapi import FastAPI
from ray import serve

from vllm.engine.arg_utils import AsyncEngineArgs
from vllm.entrypoints.launchers.api_server.app_state import init_app_state
from vllm.entrypoints.launchers.api_server.routers import register_api_routers
from vllm.entrypoints.launchers.cli_args import (make_arg_parser,
                                                 validate_parsed_serve_args)
from vllm.entrypoints.serve.exception_handling.register import init_exception_handler
from vllm.usage.usage_lib import UsageContext
from vllm.utils.argparse_utils import FlexibleArgumentParser
from vllm.v1.engine.async_llm import AsyncLLM

# Holds the engine client for the lifetime of the replica. It is populated by
# the lifespan handler below, which is the only code that runs in the replica
# process with access to a running event loop.
_replica = {}


def _load_engine_config() -> dict:
    config_path = os.getenv("ENGINE_CONFIG")
    if not config_path:
        raise ValueError("ENGINE_CONFIG env variable for engine config path is required")
    if not os.path.isfile(config_path):
        raise FileNotFoundError(f"ENGINE_CONFIG {config_path} does not exist")

    with open(config_path, "r") as f:
        return json.load(f)


def _build_args(config: dict) -> Namespace:
    """Turn the engine config into the argparse Namespace vLLM's entrypoints expect.

    Defaults come from vLLM's own server parser, so every attribute read by
    init_app_state and register_api_routers is present without enumerating them
    here. The engine config only has to name the settings it overrides.
    """
    parser = make_arg_parser(FlexibleArgumentParser(description="Ray Serve vLLM"))
    args = parser.parse_args([])

    # supported_tasks was an escape hatch for older vLLM releases. Since 0.29.0
    # the engine reports its own tasks and the pooling routers derive the score
    # and rerank endpoints from them, so an override is no longer meaningful.
    if config.pop("supported_tasks", None) is not None:
        print("Ignoring supported_tasks: tasks are reported by the engine", flush=True)

    for key, value in config.items():
        if not hasattr(args, key):
            raise ValueError(f"Unknown engine config setting: {key}")
        default = getattr(args, key)
        # Nested config groups (e.g. structured_outputs_config) parse as
        # dataclasses; JSON gives us plain dicts.
        if is_dataclass(default) and isinstance(value, dict):
            value = type(default)(**value)
        setattr(args, key, value)

    # --served-model-name is nargs='+' on the command line, and init_app_state
    # iterates it. A bare string in the engine config would iterate characters.
    if isinstance(args.served_model_name, str):
        args.served_model_name = [args.served_model_name]

    validate_parsed_serve_args(args)
    return args


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Build the engine and mount vLLM's API routers on this replica's app.

    Ray Serve awaits the ASGI lifespan startup as part of replica
    initialization, so the replica is not marked ready until the engine is up
    and every route is registered.
    """
    config = _load_engine_config()
    print(f"Engine config: {config}", flush=True)

    args = _build_args(config)
    engine_args = AsyncEngineArgs.from_cli_args(args)
    print(f"Engine args: {engine_args}", flush=True)

    print("Initializing engine", flush=True)
    engine_client = AsyncLLM.from_engine_args(
        engine_args,
        usage_context=UsageContext.OPENAI_API_SERVER,
    )
    try:
        supported_tasks = await engine_client.get_supported_tasks()
        print(f"Supported tasks: {supported_tasks}", flush=True)

        app.state.args = args
        # Starlette builds the middleware stack before it dispatches the
        # lifespan, and several of vLLM's routers (Prometheus instrumentation,
        # Cohere) call add_middleware, which Starlette rejects once that stack
        # exists. Drop the stack so registration is allowed, then rebuild it so
        # the added middleware is in place for subsequent requests. The lifespan
        # call already in flight is unaffected.
        app.middleware_stack = None
        register_api_routers(args, app, supported_tasks, engine_client.model_config)
        await init_app_state(engine_client, app.state, args, supported_tasks)
        app.middleware_stack = app.build_middleware_stack()

        _replica["engine_client"] = engine_client
        print("Engine initialized", flush=True)
        yield
    finally:
        _replica.pop("engine_client", None)
        engine_client.shutdown()


app = FastAPI(lifespan=lifespan)
init_exception_handler(app)


@serve.deployment
@serve.ingress(app)
class VLLMDeployment:
    """Serves vLLM's own OpenAI-compatible routers.

    The routes are not declared here: register_api_routers attaches vLLM's
    routers to the app during lifespan startup, and each router resolves its
    handler off app.state, which init_app_state populates.
    """

    async def check_health(self):
        engine_client = _replica.get("engine_client")
        if engine_client is None:
            # Still starting up. A failure to initialize surfaces as a lifespan
            # startup error, which fails replica initialization outright.
            return
        await engine_client.check_health()


deployment = VLLMDeployment.bind()
