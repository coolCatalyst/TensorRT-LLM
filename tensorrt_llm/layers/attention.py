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
import math

import numpy as np
import tensorrt as trt

from .._common import default_net, precision
from ..functional import (AttentionMaskType, PositionEmbeddingType, Tensor,
                          cast, clip, concat, constant, expand_mask,
                          generate_alibi_biases, generate_alibi_slopes,
                          gpt_attention, matmul, round, shape, slice, softmax,
                          split)
from ..module import Module
from ..parameter import Parameter
from ..quantization import QuantMode
from ..quantization.layers import FP8Linear, FP8RowLinear
from .linear import ColumnLinear, RowLinear


class Attention(Module):

    def __init__(self,
                 hidden_size,
                 num_attention_heads,
                 num_kv_heads=None,
                 max_position_embeddings=1024,
                 num_layers=1,
                 apply_query_key_layer_scaling=False,
                 attention_mask_type=AttentionMaskType.padding,
                 bias=True,
                 dtype=None,
                 position_embedding_type=PositionEmbeddingType.learned_absolute,
                 use_int8_kv_cache=False,
                 rotary_embedding_percentage=1.0,
                 tp_group=None,
                 tp_size=1,
                 tp_rank=0,
                 multi_block_mode=False,
                 scale_alibi_bias=False,
                 quant_mode: QuantMode = QuantMode(0)):
        super().__init__()

        self.attention_mask_type = attention_mask_type
        self.attention_head_size = hidden_size // num_attention_heads
        self.num_attention_heads = num_attention_heads // tp_size
        self.num_attention_kv_heads = (
            num_kv_heads + tp_size - 1
        ) // tp_size if num_kv_heads is not None else self.num_attention_heads
        self.hidden_size = hidden_size // tp_size
        self.max_position_embeddings = max_position_embeddings
        self.tp_size = tp_size
        self.tp_rank = tp_rank

        self.num_layers = num_layers
        self.apply_query_key_layer_scaling = apply_query_key_layer_scaling
        self.norm_factor = math.sqrt(self.attention_head_size)
        self.q_scaling = 1
        if self.apply_query_key_layer_scaling:
            self.norm_factor *= self.num_layers
            self.q_scaling *= self.num_layers
        # Whether to scale ALiBi bias. Mathematically, it's equivalent to
        # normalizing QK after adding bias.
        #   - False, inv_sqrt_Dh * Q*K^T + alibi_bias
        #   - True,  inv_sqrt_Dh * Q*K^T + inv_sqrt_Dh * alibi_bias
        self.scale_alibi_bias = scale_alibi_bias

        self.position_embedding_type = position_embedding_type
        self.multi_block_mode = multi_block_mode

        self.rotary_embedding_dim = 0
        if self.position_embedding_type.is_rope():
            self.rotary_embedding_dim = int(self.attention_head_size *
                                            rotary_embedding_percentage)
            # TODO: Once we add RotaryEmbedding outside GPTAttention plugin,
            #       we need to set it up here

        self.dtype = dtype
        self.quant_mode = quant_mode
        if use_int8_kv_cache:
            # TODO: remove use_int8_kv_cache as can be replaced by quant_mode.has_kv_cache_quant()
            # Merge int8 setting into quant_mode
            self.quant_mode = self.quant_mode.set_int8_kv_cache()
        self.use_int8_kv_cache = use_int8_kv_cache
        if self.quant_mode.has_kv_cache_quant():
            self.kv_orig_quant_scale = Parameter(shape=(1, ), dtype='float32')
            self.kv_quant_orig_scale = Parameter(shape=(1, ), dtype='float32')
        else:
            self.register_parameter('kv_orig_quant_scale', None)
            self.register_parameter('kv_quant_orig_scale', None)

        # The output feature size is therefore (h/tp + 2*kvh/tp) * d, where h is num_heads,
        # d is head_size, kvh is the num_kv_heads and tp is tensor_parallel_size.
        # In ColumnLinear op, the output dim is calculated by (h + 2*kvh) * d / tp,
        # which matches the desired output size (h/tp + 2*kvh/tp) * d after splitting
        self.use_fp8_qdq = self.quant_mode.has_fp8_qdq()
        if self.use_fp8_qdq:
            self.qkv = FP8Linear(hidden_size,
                                 hidden_size +
                                 (2 * tp_size * self.num_attention_kv_heads *
                                  self.attention_head_size),
                                 bias=bias,
                                 dtype=dtype,
                                 tp_group=tp_group,
                                 tp_size=tp_size,
                                 gather_output=False)
            self.dense = FP8RowLinear(hidden_size,
                                      hidden_size,
                                      bias=bias,
                                      dtype=dtype,
                                      tp_group=tp_group,
                                      tp_size=tp_size)
        else:
            self.qkv = ColumnLinear(hidden_size,
                                    hidden_size +
                                    (2 * tp_size * self.num_attention_kv_heads *
                                     self.attention_head_size),
                                    bias=bias,
                                    dtype=dtype,
                                    tp_group=tp_group,
                                    tp_size=tp_size,
                                    gather_output=False)
            self.dense = RowLinear(hidden_size,
                                   hidden_size,
                                   bias=bias,
                                   dtype=dtype,
                                   tp_group=tp_group,
                                   tp_size=tp_size)

    def forward(
        self,
        hidden_states: Tensor,
        attention_mask=None,
        past_key_value=None,
        sequence_length=None,
        host_past_key_value_lengths: Tensor = None,
        use_cache=False,
        cache_indirection=None,
        kv_cache_block_pointers=None,
        context_lengths: Tensor = None,
        host_context_lengths: Tensor = None,
        host_request_types=None,
        # max allowed context length. Required to
        # compute scratch memory size.
        max_context_length: int = None,
    ):

        assert isinstance(hidden_states, Tensor)

        alibi_slopes = None
        if self.position_embedding_type.is_rope():
            if not default_net().plugin_config.gpt_attention_plugin:
                raise ValueError(
                    'RoPE is only supported with GPTAttention plugin')
        elif self.position_embedding_type == PositionEmbeddingType.alibi:
            dtype = trt.float32
            if default_net().plugin_config.gpt_attention_plugin:
                dtype = hidden_states.dtype
            alibi_scale = 1. / self.norm_factor if self.scale_alibi_bias else 1.
            alibi_slopes = alibi_scale * generate_alibi_slopes(
                self.num_attention_heads * self.tp_size,
                dtype=dtype,
                tp_size=self.tp_size,
                tp_rank=self.tp_rank)

        qkv = self.qkv(hidden_states)

        if default_net().plugin_config.gpt_attention_plugin:
            assert sequence_length is not None
            assert host_past_key_value_lengths is not None
            assert cache_indirection is not None
            assert context_lengths is not None
            assert host_request_types is not None
            assert self.attention_mask_type in [
                AttentionMaskType.causal, AttentionMaskType.bidirectional
            ], 'Plugin only support masked MHA.'
            kv_orig_quant_scale = self.kv_orig_quant_scale.value if self.quant_mode.has_kv_cache_quant(
            ) else None
            kv_quant_orig_scale = self.kv_quant_orig_scale.value if self.quant_mode.has_kv_cache_quant(
            ) else None
            if default_net().plugin_config.remove_input_padding:
                assert host_context_lengths is not None
            context, past_key_value = gpt_attention(
                qkv,
                past_key_value,
                sequence_length,
                host_past_key_value_lengths,
                context_lengths,
                cache_indirection,
                host_request_types,
                self.num_attention_heads,
                self.num_attention_kv_heads,
                self.q_scaling,
                self.rotary_embedding_dim,
                self.position_embedding_type,
                self.multi_block_mode,
                kv_orig_quant_scale,
                kv_quant_orig_scale,
                self.quant_mode,
                max_context_length,
                self.attention_mask_type,
                alibi_slopes=alibi_slopes,
                tp_size=self.tp_size,
                tp_rank=self.tp_rank,
                kv_cache_block_pointers=kv_cache_block_pointers,
                host_context_lengths=host_context_lengths)

        else:
            assert default_net().plugin_config.paged_kv_cache == False

            def transpose_for_scores(x, is_kv: bool = False):
                _num_attention_heads = self.num_attention_kv_heads if is_kv else self.num_attention_heads
                new_x_shape = concat([
                    shape(x, 0),
                    shape(x, 1), _num_attention_heads, self.attention_head_size
                ])
                return x.view(new_x_shape).permute([0, 2, 1, 3])

            # qkv after projection is of shape
            #   [bs, seqlen, (num_attention_heads + 2 * num_attention_kv_heads), attention_head_size].
            # The projected and split qkv after transpose_for_scores():
            #   Q[bs, num_attention_heads, seqlen, attention_head_size]
            #   K[bs, num_attention_kv_heads, seqlen, attention_head_size]
            #   V[bs, num_attention_kv_heads, seqlen, attention_head_size]
            kv_size = self.attention_head_size * self.num_attention_kv_heads
            query, key, value = split(qkv, [self.hidden_size, kv_size, kv_size],
                                      dim=2)
            query = transpose_for_scores(query)
            key = transpose_for_scores(key, is_kv=True)
            value = transpose_for_scores(value, is_kv=True)

            if past_key_value is not None:

                def dequantize_tensor(x, scale):
                    # Cast from int8 to dtype
                    casted_x = cast(x, self.dtype)
                    return casted_x * scale

                if self.use_int8_kv_cache:
                    past_key_value = dequantize_tensor(
                        past_key_value, self.kv_quant_orig_scale.value)

                # past_key_value [bs, 2, num_heads, max_seq_len, head_dim]
                past_key, past_value = split(past_key_value, 1, dim=1)

                key_shape = concat([
                    shape(past_key, 0),
                    shape(past_key, 2),
                    shape(past_key, 3),
                    shape(past_key, 4)
                ])
                past_key = past_key.view(key_shape, zero_is_placeholder=False)
                past_value = past_value.view(key_shape,
                                             zero_is_placeholder=False)
                # FIXME(kaiyu): Remove cast after https://nvbugs/4211574 is fixed
                key = concat([past_key, key], dim=2).cast(self.dtype)
                value = concat([past_value, value], dim=2).cast(self.dtype)

            if use_cache:
                key_inflated_shape = concat([
                    shape(key, 0), 1,
                    shape(key, 1),
                    shape(key, 2),
                    shape(key, 3)
                ])
                inflated_key = key.view(key_inflated_shape,
                                        zero_is_placeholder=False)
                inflated_value = value.view(key_inflated_shape,
                                            zero_is_placeholder=False)
                past_key_value = concat([inflated_key, inflated_value], dim=1)

                if self.use_int8_kv_cache:

                    def quantize_tensor(x, scale):
                        scaled = x * scale
                        rounded = round(scaled)
                        clipped = clip(rounded, -128, 127)
                        quantized = cast(clipped, 'int8')
                        return quantized

                    past_key_value = quantize_tensor(
                        past_key_value, self.kv_orig_quant_scale.value)

            key_length = shape(key, 2)

            # The following code creates a 2D tensor with 0s in the lower triangular (including the diagonal) and
            # +INF in the upper triangular parts. This bias tensor will be added to the output of the Q*K^T matrix
            # multiplication (BMM1). The +INF elements will be transformed to 0s by the Softmax operator that
            # follows. The elements that corresponds to 0s in the bias are unaffected by the bias tensor.
            #
            # Note that when we added to another bias tensor B (for example, with AliBi), the values in the lower-
            # triangular part of the B tensor are not affected and the upper-triangular ones are set to +INF.
            if self.attention_mask_type == AttentionMaskType.causal:
                query_length = shape(query, 2)
                starts = concat([0, 0, key_length - query_length, 0])
                sizes = concat([1, 1, query_length, key_length])
                select_buf = np.expand_dims(
                    np.tril(
                        np.ones((self.max_position_embeddings,
                                 self.max_position_embeddings))).astype(bool),
                    (0, 1))

                select_buf = np.logical_not(select_buf)
                mask_buf = np.zeros_like(select_buf, np.float32)
                mask_buf[select_buf] = float('-inf')
                buffer = constant(mask_buf)
                causal_mask = slice(buffer, starts, sizes)

            if attention_mask is not None:
                attention_mask = expand_mask(attention_mask, shape(query, 2))
            bias = attention_mask
            if self.position_embedding_type == PositionEmbeddingType.alibi:
                alibi_biases = generate_alibi_biases(alibi_slopes, key_length)
                bias = alibi_biases if bias is None else bias + alibi_biases

            key = key.permute([0, 1, 3, 2])
            with precision('float32'):
                attention_scores = matmul(cast(query, 'float32'),
                                          cast(key, 'float32'))

                attention_scores = attention_scores / self.norm_factor

                if self.attention_mask_type == AttentionMaskType.causal:
                    bias = causal_mask if bias is None else bias + causal_mask

                if bias is not None:
                    attention_scores = attention_scores + bias

            attention_probs = softmax(attention_scores, dim=-1)

            context = matmul(attention_probs, value).permute([0, 2, 1, 3])
            context = context.view(
                concat([shape(context, 0),
                        shape(context, 1), self.hidden_size]))

        context = self.dense(context)

        if use_cache:
            return (context, past_key_value)
        else:
            return context
