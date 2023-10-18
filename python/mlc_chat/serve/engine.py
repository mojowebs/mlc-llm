"""The MLC LLM Serving Engine."""
from dataclasses import dataclass
from typing import List, Optional, Union

import tvm

from .config import KVCacheConfig
from .request import Request


@dataclass
class ModelInfo:
    """The model info dataclass.

    Parameters
    ----------
    model : str
        The identifier of the input model.
        It may be a compiled model's id (e.g., "Llama-2-7b-chat-hf-q4f16_1"),
        or a full path to a model directory
        (e.g., "dist/prebuilt/mlc-chat-Llama-2-7b-chat-hf-q4f16_1")

    device : str
        The device where to run the model.
        It can be "auto", "device_name" (e.g., "cuda") or
        "device_name:device_id" (e.g., "cuda:1").

    lib_path : Optional[str]
        The compiled library of the model.
        When specified, it is a path to the model library,
        e.g., "dist/prebuilt/lib/Llama-2-7b-chat-hf-q4f16_1-cuda.so"
    """

    model: str
    device: str = "auto"
    lib_path: Optional[str] = None


def _detect_local_device():
    """Automatically detect the local device.

    Returns
    ------
    device_name : str
        The name of the local device.
    """
    if tvm.metal().exist:
        return "metal"
    if tvm.rocm().exist:
        return "rocm"
    if tvm.cuda().exist:
        return "cuda"
    if tvm.vulkan().exist:
        return "vulkan"
    if tvm.opencl().exist:
        return "opencl"

    raise ValueError("No available local GPU is detected.")


class Engine:
    """The Python interface of request serving engine for MLC LLM.

    The engine can run one or multiple LLM models internally for
    text generation. Usually, when there are multiple models,
    speculative inference will be activated, where the first model
    (index 0) is the main "large model" that has better generation
    quality, and all other models are "small" models that used for
    speculation.

    The engine receives requests from the "add_request" method. For
    an given request, the engine will keep generating new tokens for
    the request until finish (under certain criterion). After finish,
    the engine will return the generation result through the callback
    function provided by the request.

    Parameters
    ----------
    models : Union[ModelInfo, List[ModelInfo]]
        One or a list of model info (specifying which models to load and
        which device to load to) to launch the engine.

    kv_cache_config : KVCacheConfig
        The configuration of the paged KV cache.
    """

    def __init__(
        self,
        models: Union[ModelInfo, List[ModelInfo]],
        kv_cache_config: KVCacheConfig,
    ):
        if not isinstance(models, list):
            models = [models]

        # - Create engine
        fcreate_engine = tvm.get_global_func("mlc.serve.create_engine")
        assert fcreate_engine is not False
        engine = fcreate_engine()

        # - Set the engine functions
        self._reload_func = engine["reload"]
        self._unload_func = engine["unload"]
        self._add_request_func = engine["add_request"]
        self._abort_func = engine["abort"]
        self._step_func = engine["step"]
        self._get_stats_func = engine["stats"]
        self._reset_engine_func = engine["reset"]

        # Load the engine with the input models and KV cache config.
        self._reload(models, kv_cache_config)

    def add_request(self, request: Request) -> None:
        """Add a new request to the engine.

        Parameters
        ----------
        request : Request
            The request to add.
        """
        self._add_request_func(request)

    def step(self) -> None:
        """The main function that the engine takes a step of action.

        At each step, the engine may decide to
        - run prefill for one (or more) requests,
        - run one-step decode for the all existing requests
        ...

        In the end of certain actions (e.g., decode), the engine will
        check if any request has finished, and will return the
        generation results for those finished requests.
        """
        self._step_func()

    def _reload(self, models: List[ModelInfo], kv_cache_config: KVCacheConfig):
        """Internal method for engine to load models and kv cache config."""
        from ..chat_module import (  # pylint: disable=import-outside-toplevel
            _get_chat_config,
            _get_lib_module_path,
            _get_model_path,
            _parse_device_str,
        )

        # - Collect the engine reload arguments.
        engine_reload_arg_list = []
        for model in models:
            # - Get the device type and id
            device, device_name = _parse_device_str(
                model.device if model.device != "auto" else _detect_local_device()
            )

            # - Get the model path and the library path
            model_path, config_file_path = _get_model_path(model.model)
            chat_config = _get_chat_config(config_file_path, user_chat_config=None)
            lib_path = _get_lib_module_path(
                model.model,
                model_path,
                chat_config,
                model.lib_path,
                device_name,
                config_file_path,
            )

            engine_reload_arg_list += [lib_path, model_path, device.device_type, device.device_id]

        # Invoke engine's reload function
        self._reload_func(*engine_reload_arg_list, kv_cache_config.asjson())
