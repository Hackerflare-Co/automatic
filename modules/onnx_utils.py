import os
import importlib
from typing import Type, Tuple, Union, List, Dict, Any
import torch
import diffusers
import onnxruntime as ort


def get_sess_options(hidden_batch_size: int, height: int, width: int, is_sdxl: bool) -> ort.SessionOptions:
    sess_options = ort.SessionOptions()
    sess_options.enable_mem_pattern = False
    sess_options.add_free_dimension_override_by_name("unet_sample_batch", hidden_batch_size)
    sess_options.add_free_dimension_override_by_name("unet_sample_channels", 4)
    sess_options.add_free_dimension_override_by_name("unet_sample_height", height // 8)
    sess_options.add_free_dimension_override_by_name("unet_sample_width", width // 8)
    sess_options.add_free_dimension_override_by_name("unet_time_batch", 1)
    sess_options.add_free_dimension_override_by_name("unet_hidden_batch", hidden_batch_size)
    sess_options.add_free_dimension_override_by_name("unet_hidden_sequence", 77)
    if is_sdxl:
        sess_options.add_free_dimension_override_by_name("unet_text_embeds_batch", hidden_batch_size)
        sess_options.add_free_dimension_override_by_name("unet_text_embeds_size", 1280)
        sess_options.add_free_dimension_override_by_name("unet_time_ids_batch", hidden_batch_size)
        sess_options.add_free_dimension_override_by_name("unet_time_ids_size", 6)
    return sess_options


def extract_device(args: List, kwargs: Dict):
    device = kwargs.get("device", None)
    if device is None:
        for arg in args:
            if isinstance(arg, torch.device):
                device = arg
    return device


def load_init_dict(cls: Type[diffusers.DiffusionPipeline], path: os.PathLike):
    merged: Dict[str, Any] = {}
    extracted = cls.extract_init_dict(diffusers.DiffusionPipeline.load_config(path))
    for dict in extracted:
        merged.update(dict)
    merged = merged.items()
    R: Dict[str, Tuple[str]] = {}
    for k, v in merged:
        if isinstance(v, list):
            if k not in cls.__init__.__annotations__:
                continue
            R[k] = v
    return R


def check_pipeline_sdxl(cls: Type[diffusers.DiffusionPipeline]) -> bool:
    return 'XL' in cls.__name__


def check_cache_onnx(path: os.PathLike) -> bool:
    if not os.path.isdir(path):
        return False
    init_dict_path = os.path.join(path, "model_index.json")
    if not os.path.isfile(init_dict_path):
        return False
    init_dict = None
    with open(init_dict_path, "r") as file:
        init_dict = file.read()
    if "OnnxRuntimeModel" not in init_dict:
        return False
    return True


def load_submodel(path: os.PathLike, is_sdxl: bool, submodel_name: str, item: List[Union[str, None]], **kwargs_ort):
    lib, atr = item
    if lib is None or atr is None:
        return None
    library = importlib.import_module(lib)
    attribute = getattr(library, atr)
    path = os.path.join(path, submodel_name)
    if issubclass(attribute, diffusers.OnnxRuntimeModel):
        return diffusers.OnnxRuntimeModel.load_model(
            os.path.join(path, "model.onnx"),
            **kwargs_ort,
        ) if is_sdxl else diffusers.OnnxRuntimeModel.from_pretrained(
            path,
            **kwargs_ort,
        )
    return attribute.from_pretrained(path)


def load_submodels(path: os.PathLike, is_sdxl: bool, init_dict: Dict[str, Type], **kwargs_ort):
    loaded = {}
    for k, v in init_dict.items():
        if not isinstance(v, list):
            loaded[k] = v
            continue
        try:
            loaded[k] = load_submodel(path, is_sdxl, k, v, **kwargs_ort)
        except Exception:
            pass
    return loaded


def patch_kwargs(cls: Type[diffusers.DiffusionPipeline], kwargs: Dict) -> Dict:
    if cls == diffusers.OnnxStableDiffusionPipeline or cls == diffusers.OnnxStableDiffusionImg2ImgPipeline or cls == diffusers.OnnxStableDiffusionInpaintPipeline:
        kwargs["safety_checker"] = None
        kwargs["requires_safety_checker"] = False
    if cls == diffusers.OnnxStableDiffusionXLPipeline or cls == diffusers.OnnxStableDiffusionXLImg2ImgPipeline:
        kwargs["config"] = {}

    return kwargs


def load_pipeline(cls: Type[diffusers.DiffusionPipeline], path: os.PathLike, **kwargs_ort) -> diffusers.DiffusionPipeline:
    if os.path.isdir(path):
        return cls(**patch_kwargs(cls, load_submodels(path, check_pipeline_sdxl(cls), load_init_dict(cls, path), **kwargs_ort)))
    else:
        return cls.from_single_file(path)
