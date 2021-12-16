__version__ = "3.0.0"

from dataclasses import dataclass
import enum
import math
import os.path
import typing

import vapoursynth as vs
from vapoursynth import core


class Backend:
    @dataclass(frozen=True)
    class ORT_CPU:
        num_streams: int = 1
        verbosity: int = 2

    @dataclass(frozen=True)
    class ORT_CUDA:
        device_id: int = 0
        cudnn_benchmark: bool = True
        num_streams: int = 1
        verbosity: int = 2

    @dataclass(frozen=True)
    class OV_CPU:
        pass


def calcSize(width: int, tiles: int, overlap: int) -> int:
    return math.ceil((width + 2 * overlap * (tiles - 1)) / tiles)


@enum.unique
class Waifu2xModel(enum.IntEnum):
    anime_style_art = 0
    anime_style_art_rgb = 1
    photo = 2
    upconv_7_anime_style_art_rgb = 3
    upconv_7_photo = 4
    upresnet10 = 5
    cunet = 6


def Waifu2x(
    clip: vs.VideoNode,
    noise: typing.Literal[-1, 0, 1, 2, 3] = -1,
    scale: typing.Literal[1, 2] = 2,
    tiles: typing.Optional[typing.Union[int, typing.Tuple[int, int]]] = None,
    tilesize: typing.Optional[typing.Union[int, typing.Tuple[int, int]]] = None,
    overlap: typing.Optional[typing.Union[int, typing.Tuple[int, int]]] = None,
    model: typing.Literal[0, 1, 2, 3, 4, 5, 6] = 6,
    backend: typing.Union[Backend.OV_CPU, Backend.ORT_CPU, Backend.ORT_CUDA] = Backend.OV_CPU()
) -> vs.VideoNode:

    funcName = "vsmlrt.Waifu2x"

    if not isinstance(clip, vs.VideoNode):
        raise TypeError(f'{funcName}: "clip" must be a clip!')

    if clip.format.sample_type != vs.FLOAT or clip.format.bits_per_sample != 32:
        raise ValueError(f"{funcName}: only constant format 32 bit float input supported")

    if not isinstance(noise, int) or noise not in range(-1, 4):
        raise ValueError(f'{funcName}: "noise" must be -1, 0, 1, 2, or 3')

    if not isinstance(scale, int) or scale not in (1, 2):
        raise ValueError(f'{funcName}: "scale" must be 1 or 2')

    if not isinstance(model, int) or model not in Waifu2xModel.__members__.values():
        raise ValueError(f'{funcName}: "model" must be 0, 1, 2, 3, 4, 5, or 6')

    if model == 0 and noise == 0:
        raise ValueError(
            f'{funcName}: "anime_style_art" model'
            ' does not support noise reduction level 0'
        )

    if overlap is None:
        overlap_w = overlap_h = [8, 8, 8, 8, 8, 4, 4][model]
    elif isinstance(overlap, int):
        overlap_w = overlap_h = overlap
    else:
        overlap_w, overlap_h = overlap

    if tilesize is None:
        if tiles is None:
            overlap = 0
            tile_w = clip.width
            tile_h = clip.height
        elif isinstance(tiles, int):
            tile_w = calcSize(clip.width, tiles, overlap_w)
            tile_h = calcSize(clip.height, tiles, overlap_h)
        else:
            tile_w = calcSize(clip.width, tiles[0], overlap_w)
            tile_h = calcSize(clip.height, tiles[1], overlap_h)
    elif isinstance(tilesize, int):
        tile_w = tilesize
        tile_h = tilesize
    else:
        tile_w, tile_h = tilesize

    if model == 6 and (tile_w % 4 != 0 or tile_h % 4 != 0): # type: ignore
        raise ValueError(f'{funcName}: tile size of cunet model must be divisible by 4')

    if model == 0:
        if clip.format.id != vs.GRAYS:
            raise ValueError(f'{funcName}: input should be of GRAYS format')
    elif clip.format.id != vs.RGBS:
        raise ValueError(f'{funcName}: input should be of RGBS format')

    if backend is Backend.ORT_CPU:
        backend = Backend.ORT_CPU()
    elif backend is Backend.ORT_CUDA:
        backend = Backend.ORT_CUDA()
    elif backend is Backend.OV_CPU:
        backend = Backend.OV_CPU()

    folder_path = os.path.join("waifu2x", tuple(Waifu2xModel.__members__)[model])

    if model in (0, 1, 2):
        if noise == -1:
            model_name = "scale2.0x_model.onnx"
        else:
             model_name = f"noise{noise}_model.onnx"
    elif model in (3, 4, 5):
        if noise == -1:
            model_name = "scale2.0x_model.onnx"
        else:
            model_name = f"noise{noise}_scale2.0x_model.onnx"
    else:
        if scale == 1:
            scale_name = ""
        else:
            scale_name = "scale2.0x_"

        if noise == -1:
            model_name = "scale2.0x_model.onnx"
        else:
            model_name = f"noise{noise}_{scale_name}model.onnx"

    network_path = os.path.join(folder_path, model_name)

    width, height = clip.width, clip.height
    if model in (0, 1, 2):
        # emulating cv2.resize(interpolation=cv2.INTER_CUBIC)
        clip = core.resize.Bicubic(
            clip,
            width * 2, height * 2,
            filter_param_a=0, filter_param_b=0.75
        )

    if isinstance(backend, Backend.ORT_CPU):
        clip = core.ort.Model(
            clip, network_path,
            overlap=(overlap_w, overlap_h), tilesize=(tile_w, tile_h),
            provider="CPU", builtin=1,
            num_streams=backend.num_streams,
            verbosity=backend.verbosity
        )
    elif isinstance(backend, Backend.ORT_CUDA):
        clip = core.ort.Model(
            clip, network_path,
            overlap=(overlap_w, overlap_h), tilesize=(tile_w, tile_h),
            provider="CUDA", builtin=1,
            device_id=backend.device_id,
            num_streams=backend.num_streams,
            verbosity=backend.verbosity,
            cudnn_benchmark=backend.cudnn_benchmark
        )
    elif isinstance(backend, Backend.OV_CPU):
        clip = core.ov.Model(
            clip, network_path,
            overlap=(overlap_w, overlap_h), tilesize=(tile_w, tile_h),
            device="CPU", builtin=1
        )
    else:
        raise ValueError(f'{funcName}: unknown backend {backend}: ["ort-cpu", "ort-cuda"]')

    if scale == 1 and clip.width // width == 2:
        # emulating cv2.resize(interpolation=cv2.INTER_CUBIC)
        # cr: @AkarinVS
        clip = core.fmtc.resample(
            clip, scale=0.5,
            kernel="impulse", impulse=[-0.1875, 1.375, -0.1875],
            kovrspl=2
        )

    return clip
