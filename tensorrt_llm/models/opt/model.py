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
import tensorrt as trt

from ..._common import default_net
from ..._utils import pad_vocab_size, str_dtype_to_trt
from ...functional import Tensor, gather_last_token_logits
from ...layers import (MLP, Attention, AttentionMaskType, ColumnLinear,
                       LayerNorm, PositionEmbeddingType)
from ...mapping import Mapping
from ...module import Module, ModuleList
from ..generation_mixin import GenerationMixin
from ..gpt.model import GPTEmbedding


class OPTDecoderLayer(Module):

    def __init__(self,
                 hidden_size,
                 num_attention_heads,
                 max_position_embeddings,
                 dtype=None,
                 hidden_act='relu',
                 pre_norm=False,
                 tp_group=None,
                 tp_size=1):
        super().__init__()
        self.input_layernorm = LayerNorm(normalized_shape=hidden_size,
                                         dtype=dtype)

        self.attention = Attention(
            hidden_size,
            num_attention_heads,
            max_position_embeddings=max_position_embeddings,
            attention_mask_type=AttentionMaskType.causal,
            dtype=dtype,
            tp_group=tp_group,
            tp_size=tp_size)

        self.mlp = MLP(hidden_size=hidden_size,
                       ffn_hidden_size=hidden_size * 4,
                       hidden_act=hidden_act,
                       dtype=dtype,
                       tp_group=tp_group,
                       tp_size=tp_size)
        self.post_layernorm = LayerNorm(normalized_shape=hidden_size,
                                        dtype=dtype)

        self.pre_norm = pre_norm

    def forward(self,
                hidden_states: Tensor,
                attention_mask=None,
                past_key_value=None,
                sequence_length=None,
                host_past_key_value_lengths=None,
                use_cache=False,
                cache_indirection=None,
                context_lengths=None,
                host_context_lengths=None,
                host_request_types=None,
                max_context_length=None):
        residual = hidden_states

        attention_input = hidden_states
        if self.pre_norm:
            attention_input = self.input_layernorm(hidden_states)

        # At this point the hidden_states object must be a Tensor.
        assert isinstance(attention_input, Tensor)

        attention_output = self.attention(
            attention_input,
            attention_mask=attention_mask,
            past_key_value=past_key_value,
            sequence_length=sequence_length,
            host_past_key_value_lengths=host_past_key_value_lengths,
            use_cache=use_cache,
            cache_indirection=cache_indirection,
            context_lengths=context_lengths,
            host_context_lengths=host_context_lengths,
            host_request_types=host_request_types,
            max_context_length=max_context_length)

        if use_cache:
            attention_output, presents = attention_output

        hidden_states = residual + attention_output
        if not self.pre_norm:
            hidden_states = self.input_layernorm(hidden_states)

        residual = hidden_states
        if self.pre_norm:
            hidden_states = self.post_layernorm(hidden_states)

        hidden_states = self.mlp(hidden_states)

        hidden_states = residual + hidden_states

        if not self.pre_norm:
            hidden_states = self.post_layernorm(hidden_states)

        if use_cache:
            return (hidden_states, presents)
        return hidden_states


class OPTModel(Module):

    def __init__(self,
                 num_layers,
                 num_heads,
                 hidden_size,
                 vocab_size,
                 hidden_act,
                 max_position_embeddings,
                 dtype=None,
                 mapping=Mapping(),
                 pre_norm=True,
                 do_layer_norm_before=True,
                 use_parallel_embedding=False,
                 embedding_sharding_dim=0):
        super().__init__()
        self.do_layer_norm_before = do_layer_norm_before

        self.embedding = GPTEmbedding(
            vocab_size,
            hidden_size,
            max_position_embeddings,
            position_embedding_type=PositionEmbeddingType.learned_absolute,
            dtype=dtype,
            tensor_parallel=mapping.tp_size if use_parallel_embedding else 1,
            tensor_parallel_group=mapping.tp_group
            if use_parallel_embedding else None,
            sharding_dim=embedding_sharding_dim,
            tensor_parallel_rank=mapping.tp_rank)

        self.layers = ModuleList([
            OPTDecoderLayer(hidden_size=hidden_size,
                            num_attention_heads=num_heads,
                            max_position_embeddings=max_position_embeddings,
                            dtype=dtype,
                            hidden_act=hidden_act,
                            pre_norm=pre_norm,
                            tp_group=mapping.tp_group,
                            tp_size=mapping.tp_size) for _ in range(num_layers)
        ])

        if self.do_layer_norm_before:
            self.ln_f = LayerNorm(normalized_shape=hidden_size, dtype=dtype)

    def forward(self,
                input_ids=None,
                position_ids=None,
                past_key_value=None,
                sequence_length=None,
                host_past_key_value_lengths=None,
                use_cache=False,
                attention_mask=None,
                cache_indirection=None,
                context_lengths=None,
                host_context_lengths=None,
                host_request_types=None,
                max_context_length=None):

        hidden_states = self.embedding(input_ids, position_ids)

        if past_key_value is None:
            past_key_value = tuple([None] * len(self.layers))

        if use_cache:
            presents = []

        for layer, past in zip(self.layers, past_key_value):
            hidden_states = layer(
                hidden_states,
                past_key_value=past,
                sequence_length=sequence_length,
                host_past_key_value_lengths=host_past_key_value_lengths,
                use_cache=use_cache,
                attention_mask=attention_mask,
                cache_indirection=cache_indirection,
                context_lengths=context_lengths,
                host_context_lengths=host_context_lengths,
                host_request_types=host_request_types,
                max_context_length=max_context_length)

            if use_cache:
                presents.append(hidden_states[1])
                hidden_states = hidden_states[0]

        if self.do_layer_norm_before:
            hidden_states = self.ln_f(hidden_states)

        if use_cache:
            return (hidden_states, tuple(presents))
        return hidden_states


class OPTLMHeadModel(OPTModel, GenerationMixin):

    def __init__(self,
                 num_layers,
                 num_heads,
                 hidden_size,
                 vocab_size,
                 hidden_act,
                 max_position_embeddings,
                 dtype,
                 mapping=Mapping(),
                 pre_norm=True,
                 do_layer_norm_before=True,
                 use_parallel_embedding=False,
                 embedding_sharding_dim=0,
                 share_embedding_table=False):
        if share_embedding_table and mapping.tp_size > 1:
            if (not use_parallel_embedding) or (use_parallel_embedding and
                                                embedding_sharding_dim == 1):
                raise NotImplementedError(
                    'For multiple-processes cases, sharing the embedding table must set use_parallel_embedding=True and embedding_sharding_dim = 0'
                )

        super().__init__(num_layers, num_heads, hidden_size, vocab_size,
                         hidden_act, max_position_embeddings, dtype, mapping,
                         pre_norm, do_layer_norm_before, use_parallel_embedding,
                         embedding_sharding_dim)
        vocab_size_padded = pad_vocab_size(vocab_size, mapping.tp_size)
        if isinstance(dtype, str):
            self._kv_dtype = str_dtype_to_trt(dtype)
        else:
            assert isinstance(dtype, trt.DataType)
            self._kv_dtype = dtype
        self._num_layers = num_layers
        self._num_heads = num_heads
        self._hidden_size = hidden_size
        self._vocab_size = vocab_size
        self._tp_size = mapping.tp_size

        share_weight = None
        if share_embedding_table:
            share_weight = self.embedding.vocab_embedding.weight

        self.lm_head = ColumnLinear(hidden_size,
                                    vocab_size_padded,
                                    bias=False,
                                    dtype=dtype,
                                    tp_group=mapping.tp_group,
                                    tp_size=mapping.tp_size,
                                    gather_output=True,
                                    share_weight=share_weight)

    def forward(self,
                input_ids: Tensor,
                position_ids=None,
                past_key_value=None,
                sequence_length=None,
                host_past_key_value_lengths=None,
                use_cache=False,
                last_token_ids=None,
                attention_mask=None,
                cache_indirection=None,
                context_lengths=None,
                host_context_lengths=None,
                host_request_types=None,
                max_context_length=None):
        hidden_states = super().forward(
            input_ids,
            position_ids,
            past_key_value,
            sequence_length,
            host_past_key_value_lengths,
            use_cache,
            attention_mask,
            cache_indirection,
            context_lengths=context_lengths,
            host_context_lengths=host_context_lengths,
            host_request_types=host_request_types,
            max_context_length=max_context_length)

        if use_cache:
            hidden_states, presents = hidden_states

        hidden_states = gather_last_token_logits(
            hidden_states, last_token_ids,
            default_net().plugin_config.remove_input_padding)

        # [batch_size, hidden_size] -> [batch_size, vocab_size]
        lm_logits = self.lm_head(hidden_states)
        lm_logits.mark_output('logits', self._kv_dtype)

        if use_cache:
            for i, present in enumerate(presents):
                present.mark_output(f'present_key_value_{i}', self._kv_dtype)
            return (lm_logits, presents)

        return lm_logits

    def prepare_inputs(self, max_batch_size, max_input_len, max_new_tokens,
                       use_cache, max_beam_width):
        '''@brief: Prepare inputs Tensors for the model, the given sizes are used to determine the
            ranges of the dimensions of when using TRT dynamic shapes.

            @return: a list contains values which can be fed into the self.forward()
        '''

        # Prepare inputs
        head_size = self._hidden_size // self._num_heads
        num_heads = self._num_heads // self._tp_size
        remove_input_padding = default_net().plugin_config.remove_input_padding
        use_gpt_attention_plugin = default_net(
        ).plugin_config.gpt_attention_plugin

        model_inputs = self.prepare_basic_inputs(
            max_batch_size, max_beam_width, max_input_len, max_new_tokens,
            num_heads, head_size, self._num_layers, self._kv_dtype,
            remove_input_padding, use_gpt_attention_plugin)

        return (model_inputs['input_ids'], model_inputs['position_ids'],
                model_inputs['past_key_value'], model_inputs['sequence_length'],
                model_inputs['host_past_key_value_lengths'], True,
                model_inputs['last_token_ids'], model_inputs['attention_mask'],
                model_inputs['cache_indirection'],
                model_inputs['context_lengths'],
                model_inputs['host_context_lengths'],
                model_inputs['host_request_types'], max_input_len)
