# SPDX-FileCopyrightText: Copyright (c) 2022-2023 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import re
import time
from pathlib import Path
from typing import Dict, Optional, Union

import numpy as np
import torch

import tensorrt_llm
import tensorrt_llm.logger as logger


def split(weight: np.ndarray, tp_size: int, rank: int = 0, dim: int = 0):
    if tp_size == 1:
        return weight
    elif weight.ndim == 1:
        return np.ascontiguousarray(np.split(weight, tp_size)[rank])
    return np.ascontiguousarray(np.split(weight, tp_size, axis=dim)[rank])


def reorder_qkv_weight_or_bias(weight: np.ndarray,
                               head_dim: int,
                               num_heads: int,
                               num_kv_heads: Optional[int] = None,
                               tp_size: int = 1,
                               is_bias: bool = False):
    """ Reorder the qkv weight for TRT-LLM use.

    The shape of the fused QKV weights in HF is different from the shape that
    TRT-LLM requires. In particular, the weight of HF consists of interleaved
    q, k, v head weights, while that of TRT-LLM is contigous.
        HF     : [q1, k1, v1, ..., qh, kh, vh]
        TRT-LLM: [q1, ..., qh, k1, ..., kh, v1, vh]
    where qi, vi, ki are weight vectors corresponding to attention head i.
    It's similar to multi/grouped query attention cases.

    We reorder and split the weight of an attention layer to fit into TRT-LLM.
    The reordered weight and bias will be
        weight: (T, Qh * D + 2 * KVh * D, H)
        bias  : (T, Qh * D + 2 * KVh * D)
    where T=tp_size, Qh=local_num_q_heads, KVh=local_num_kv_heads, D=head_dim,
    H=hidden_dim. In the multi/grouped query attention, the number of K/V
    attention heads are less than that of Q attention, so that K/V attention
    heads may be shared across different ranks if necessary.

    For tensor parallelism, we use the first dimension to select the
    corresponding weights.
    """

    # Query types and expected kv heads.
    #  - Conventional MHA: num_heads = num_kv_heads
    #  - Multi-Query Attention: num_kv_heads = 1
    #  - Grouped-Query Attention: num_heads % num_kv_heads = 0
    num_kv_heads = num_kv_heads if num_kv_heads is not None else num_heads
    assert num_heads % num_kv_heads == 0, \
        f'num_heads({num_heads}) must be divisible by '\
        f'num_kv_heads({num_kv_heads})).'

    # The number of attention heads per group: N q head + 1 k head + 1 v head.
    num_group_heads = num_heads // num_kv_heads + 2
    assert weight.shape[0] == num_kv_heads * num_group_heads * head_dim, \
        f'{weight.shape[0]} != {num_kv_heads} * {num_group_heads} * {head_dim}'

    qkv_in = num_heads * head_dim if not is_bias else 1

    # Split Q/K/V weights
    weight = weight.reshape(num_kv_heads, num_heads // num_kv_heads + 2,
                            head_dim, qkv_in)
    q_w = weight[:, :-2, ...]  # (nKV, num_heads // nKV, head_dim, qkv_in)
    k_w = weight[:, -2:-1, ...]  # (nKV, 1, head_dim, qkv_in)
    v_w = weight[:, -1:, ...]  # (nKV, 1, head_dim, qkv_in)

    if num_kv_heads < num_heads and num_kv_heads < tp_size:
        # Duplicate K/V heads to make sure that each rank has at least one
        # K/V heads. For instance, num_heads=8, num_kv_heads=2, tp_size=4,
        # we will make the qkv weight as below.
        #   Orig: [q0 q1 q2 q3 k0 v0 q4 q5 q6 q7 k1 v0 v1]
        #   >>>>  [[q0 q1 k0 v0], [q2 q3 k0 v0], [q4 q5 k1 v1], [q6 q7 k1 v1]]
        assert tp_size % num_kv_heads == 0
        num_dups = tp_size // num_kv_heads

        # k_w and v_w have the same shape.
        new_shape = (num_kv_heads, num_dups) + k_w.shape[2:]
        k_w = np.broadcast_to(k_w, shape=new_shape)
        v_w = np.broadcast_to(v_w, shape=new_shape)

        # Update the number of kv heads.
        num_kv_heads = tp_size

    reordered = np.concatenate(
        [
            q_w.reshape(tp_size, num_heads // tp_size, head_dim, qkv_in),
            k_w.reshape(tp_size, num_kv_heads // tp_size, head_dim, qkv_in),
            v_w.reshape(tp_size, num_kv_heads // tp_size, head_dim, qkv_in),
        ],
        axis=1,
    )

    qkv_out = (num_heads + 2 * num_kv_heads) // tp_size * head_dim
    return reordered.reshape((tp_size, qkv_out, -1))


def split_qkv_weight(trtllm_falcon: tensorrt_llm.models.FalconModel,
                     weight: np.ndarray,
                     tp_size: int,
                     rank: int,
                     is_bias: bool,
                     num_kv_heads: Optional[int] = None):
    """ Splits the QKV matrix according to tensor parallelism """
    n_heads = trtllm_falcon.num_heads
    hidden_size = trtllm_falcon.hidden_size
    head_dim = hidden_size // n_heads
    weight = reorder_qkv_weight_or_bias(weight,
                                        head_dim=head_dim,
                                        num_heads=n_heads,
                                        num_kv_heads=num_kv_heads,
                                        tp_size=tp_size,
                                        is_bias=is_bias)
    if not is_bias:
        return np.ascontiguousarray(weight[rank, ...])
    else:
        return weight[rank, ...].ravel()


def split_matrix(weight: np.ndarray, tp_size: int, rank: int, dim: int):
    return np.ascontiguousarray(split(weight, tp_size, rank, dim=dim))


def get_weight(params: Dict, prefix: str, dtype: torch.dtype):
    if f'{prefix}.weight' not in params:
        return None
    param = params[f'{prefix}.weight'].to(dtype).detach().cpu()
    return tensorrt_llm._utils.torch_to_numpy(param)


def get_bias(params: Dict, prefix: str, dtype: torch.dtype):
    if f'{prefix}.bias' not in params:
        return None
    param = params[f'{prefix}.bias'].to(dtype).detach().cpu()
    return tensorrt_llm._utils.torch_to_numpy(param)


def get_weight_and_bias(params: Dict, prefix: str, dtype: torch.dtype):
    return get_weight(params, prefix, dtype), get_bias(params, prefix, dtype)


def load_from_hf_falcon(trtllm_falcon: tensorrt_llm.models.FalconForCausalLM,
                        hf_falcon,
                        rank: int = 0,
                        tensor_parallel: int = 1,
                        dtype: Union[str, torch.dtype] = torch.float32):
    logger.info('Loading weights from HF Falcon...')
    tik = time.time()

    model_params = dict(hf_falcon.named_parameters())
    if isinstance(dtype, str):
        dtype = tensorrt_llm._utils.str_dtype_to_torch(dtype)
    num_kv_heads = trtllm_falcon.num_kv_heads

    for i in range(trtllm_falcon.num_layers):
        prefix = f'transformer.h.{i}'
        layer = trtllm_falcon.layers[i]
        qkv_weight, qkv_bias = get_weight_and_bias(
            model_params, f'{prefix}.self_attention.query_key_value', dtype)
        qkv_w = split_qkv_weight(trtllm_falcon,
                                 qkv_weight,
                                 tensor_parallel,
                                 rank,
                                 is_bias=False,
                                 num_kv_heads=num_kv_heads)
        layer.attention.qkv.weight.value = qkv_w
        if qkv_bias is not None:
            layer.attention.qkv.bias.value = split_qkv_weight(
                trtllm_falcon,
                qkv_bias,
                tensor_parallel,
                rank,
                is_bias=True,
                num_kv_heads=num_kv_heads)

        logger.debug(f'Layer {i}: Loading attention Dense weights...')
        attn_dense_weight, attn_dense_bias = get_weight_and_bias(
            model_params, f'{prefix}.self_attention.dense', dtype)
        layer.attention.dense.weight.value = split_matrix(attn_dense_weight,
                                                          tensor_parallel,
                                                          rank,
                                                          dim=1)
        if attn_dense_bias is not None:
            layer.attention.dense.bias.value = attn_dense_bias

        logger.debug(f'Layer {i}: Loading MLP FC weights...')
        mlp_fc_weight, mlp_fc_bias = get_weight_and_bias(
            model_params, f'{prefix}.mlp.dense_h_to_4h', dtype)
        layer.mlp.fc.weight.value = split_matrix(mlp_fc_weight,
                                                 tensor_parallel,
                                                 rank,
                                                 dim=0)
        if mlp_fc_bias is not None:
            layer.mlp.fc.bias.value = split_matrix(mlp_fc_bias,
                                                   tensor_parallel,
                                                   rank,
                                                   dim=0)

        logger.debug(f'Layer {i}: Loading MLP Proj weights...')
        mlp_proj_weight, mlp_proj_bias = get_weight_and_bias(
            model_params, f'{prefix}.mlp.dense_4h_to_h', dtype)
        layer.mlp.proj.weight.value = split_matrix(mlp_proj_weight,
                                                   tensor_parallel,
                                                   rank,
                                                   dim=1)
        if mlp_proj_bias is not None:
            layer.mlp.proj.bias.value = mlp_proj_bias

        if trtllm_falcon.new_decoder_architecture:
            input_ln_weight, input_ln_bias = get_weight_and_bias(
                model_params, f'{prefix}.ln_attn', dtype)
            layer.input_layernorm.weight.value = input_ln_weight
            if input_ln_bias is not None:
                layer.input_layernorm.bias.value = input_ln_bias

            mlp_ln_weight, mlp_ln_bias = get_weight_and_bias(
                model_params, f'{prefix}.ln_mlp', dtype)
            layer.mlp_layernorm.weight.value = mlp_ln_weight
            if mlp_ln_bias is not None:
                layer.mlp_layernorm.bias.value = mlp_ln_bias
        else:
            # Layer norms do not use tensor parallelism
            logger.debug(f'Layer {i}: Loading normalization weights...')
            input_ln_weight, input_ln_bias = get_weight_and_bias(
                model_params, f'{prefix}.input_layernorm', dtype)
            layer.input_layernorm.weight.value = input_ln_weight
            if input_ln_bias is not None:
                layer.input_layernorm.bias.value = input_ln_bias

            if not trtllm_falcon.parallel_attention:
                post_ln_weight, post_ln_bias = get_weight_and_bias(
                    model_params, f'{prefix}.post_attention_layernorm', dtype)
                if post_ln_weight is not None:
                    layer.post_layernorm.weight.value = post_ln_weight
                if post_ln_bias is not None:
                    layer.post_layernorm.bias.value = post_ln_bias

    embed_w = get_weight(model_params, 'transformer.word_embeddings', dtype)
    trtllm_falcon.embedding.weight.value = embed_w.copy()
    trtllm_falcon.lm_head.weight.value = split_matrix(embed_w,
                                                      tensor_parallel,
                                                      rank,
                                                      dim=0)

    ln_f_w, ln_f_b = get_weight_and_bias(model_params, 'transformer.ln_f',
                                         dtype)
    trtllm_falcon.ln_f.weight.value = ln_f_w
    if ln_f_b is not None:
        trtllm_falcon.ln_f.bias.value = ln_f_b

    tok = time.time()
    t = time.strftime('%H:%M:%S', time.gmtime(tok - tik))
    logger.info(f'Weights loaded. Total time: {t}')


def load_state_dict(file_path: Path,
                    dtype: torch.dtype) -> Dict[str, np.ndarray]:
    """ Load weights from model file

    `safetensors` or `pytorch binary` is supported

    # Args.
        file_path: model file path, ends with .bin or .safetensors.
        dtype: torch.dtype, data type.
    # Returns.
        Dict[str, torch.Tensor]
    """

    state_dict = {}
    if file_path.suffix == '.safetensors':
        # load from safetensors file
        from safetensors import safe_open
        with safe_open(file_path, framework='pt', device='cpu') as f:
            for name in f.keys():
                param = f.get_tensor(name).to(dtype)
                state_dict[name] = tensorrt_llm._utils.torch_to_numpy(param)
    elif file_path.suffix == '.bin':
        # load from pytorch bin file
        state_dict = torch.load(file_path, map_location='cpu')
        for name in state_dict:
            param = state_dict[name].to(dtype)
            state_dict[name] = tensorrt_llm._utils.torch_to_numpy(param)
    else:
        raise NotImplementedError(
            f'Support .safetensors or .bin files, but got {str(file_path)}')
    return state_dict


def retrieved_layer_index_from_name(name: str) -> Optional[int]:
    res = re.search(r'\d+', name)
    return int(res.group()) if res is not None else res


def iterate_shard_files(model_dir: Path):
    for file in model_dir.glob('*.bin'):
        yield file
    for file in model_dir.glob('*.safetensors'):
        yield file


def load_from_hf_checkpoint(
    trtllm_falcon: tensorrt_llm.models.FalconForCausalLM,
    model_dir: Union[str, Path],
    rank: int = 0,
    tensor_parallel: int = 1,
    dtype: Union[str, torch.dtype] = torch.float32,
):
    logger.info('Loading weights from HF Falcon...')
    tik = time.time()

    model_dir = Path(model_dir)
    if isinstance(dtype, str):
        dtype = tensorrt_llm._utils.str_dtype_to_torch(dtype)

    def is_bias(_name):
        return 'bias' in _name

    for model_file in iterate_shard_files(model_dir):
        logger.debug(f'Loading file {str(model_file)}...')
        state_dict = load_state_dict(model_file, dtype)
        for name, param in state_dict.items():
            logger.debug(f'Converting weight {name}...')
            i = retrieved_layer_index_from_name(name)
            layer = trtllm_falcon.layers[i] if i is not None else None

            if 'self_attention.query_key_value' in name:
                if not is_bias(name):
                    layer.attention.qkv.weight.value = split_qkv_weight(
                        trtllm_falcon,
                        param,
                        tensor_parallel,
                        rank,
                        is_bias=False,
                        num_kv_heads=trtllm_falcon.num_kv_heads)
                else:
                    layer.attention.qkv.bias.value = split_qkv_weight(
                        trtllm_falcon,
                        param,
                        tensor_parallel,
                        rank,
                        is_bias=True,
                        num_kv_heads=trtllm_falcon.num_kv_heads)
            elif 'self_attention.dense' in name:
                if not is_bias(name):
                    layer.attention.dense.weight.value = split_matrix(
                        param, tensor_parallel, rank, dim=1)
                else:
                    layer.attention.dense.bias.value = param
            elif 'mlp.dense_h_to_4h' in name:
                if not is_bias(name):
                    layer.mlp.fc.weight.value = split_matrix(param,
                                                             tensor_parallel,
                                                             rank,
                                                             dim=0)
                else:
                    layer.mlp.fc.bias.value = split_matrix(param,
                                                           tensor_parallel,
                                                           rank,
                                                           dim=0)
            elif 'mlp.dense_4h_to_h' in name:
                if not is_bias(name):
                    layer.mlp.proj.weight.value = split_matrix(param,
                                                               tensor_parallel,
                                                               rank,
                                                               dim=1)
                else:
                    layer.mlp.proj.bias.value = param
            elif 'ln_attn' in name or 'input_layernorm' in name:
                if not is_bias(name):
                    layer.input_layernorm.weight.value = param
                else:
                    layer.input_layernorm.bias.value = param
            elif 'ln_mlp' in name:
                assert layer.mlp_layernorm is not None
                if not is_bias(name):
                    layer.mlp_layernorm.weight.value = param
                else:
                    layer.mlp_layernorm.bias.value = param
            elif 'post_attention_layernorm' in name:
                assert layer.post_layernorm is not None
                if not is_bias(name):
                    layer.post_layernorm.weight.value = param
                else:
                    layer.post_layernorm.bias.value = param
            elif 'word_embeddings' in name:
                trtllm_falcon.embedding.weight.value = param.copy()
                trtllm_falcon.lm_head.weight.value = split_matrix(
                    param, tensor_parallel, rank, dim=0)
            elif 'ln_f' in name:
                if not is_bias(name):
                    trtllm_falcon.ln_f.weight.value = param
                else:
                    trtllm_falcon.ln_f.bias.value = param
        del state_dict

    tok = time.time()
    t = time.strftime('%H:%M:%S', time.gmtime(tok - tik))
    logger.info(f'Weights loaded. Total time: {t}')
