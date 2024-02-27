from ..layers import MLP, ColumnLinear, GatedMLP, LayerNorm, RmsNorm, RowLinear
from .layers import (Int8SmoothQuantLinear, Int8SmoothQuantRowLinear,
                     SmoothQuantAttention, SmoothQuantGatedMLP,
                     SmoothQuantLayerNorm, SmoothQuantMLP, SmoothQuantRmsNorm,
                     WeightOnlyGroupwiseQuantColumnLinear,
                     WeightOnlyGroupwiseQuantRowLinear,
                     WeightOnlyQuantColumnLinear, WeightOnlyQuantRowLinear)
from .mode import W4A8_AWQ, W4A16_AWQ


def weight_only_quantize(model,
                         quant_mode,
                         exclude_modules=None,
                         current_key_name=None):
    assert quant_mode.is_weight_only()

    exclude_modules = ['lm_head'
                       ] if exclude_modules is None else exclude_modules

    for name, module in model.named_children():
        if current_key_name is None:
            current_key_name = []
        current_key_name.append(name)

        if len(list(module.children())) > 0:
            weight_only_quantize(module, quant_mode, exclude_modules,
                                 current_key_name)

        if isinstance(module, ColumnLinear) and name not in exclude_modules:
            if not any(key in '.'.join(current_key_name)
                       for key in exclude_modules):
                model._modules[name] = WeightOnlyQuantColumnLinear(
                    in_features=module.in_features,
                    out_features=module.out_features * module.tp_size,
                    bias=module.bias is not None,
                    dtype=module.dtype,
                    tp_group=module.tp_group,
                    tp_size=module.tp_size,
                    gather_output=module.gather_output,
                    quant_mode=quant_mode)
        elif isinstance(module, RowLinear) and name not in exclude_modules:
            if not any(key in '.'.join(current_key_name)
                       for key in exclude_modules):
                model._modules[name] = WeightOnlyQuantRowLinear(
                    in_features=module.in_features * module.tp_size,
                    out_features=module.out_features,
                    bias=module.bias is not None,
                    dtype=module.dtype,
                    tp_group=module.tp_group,
                    tp_size=module.tp_size,
                    quant_mode=quant_mode)

        current_key_name.pop(-1)

    setattr(model, 'quant_mode', quant_mode)

    return model


def weight_only_groupwise_quantize(model,
                                   quant_mode,
                                   quant_algo=W4A16_AWQ,
                                   group_size=128,
                                   pre_quant_scale=False,
                                   zero=False,
                                   exclude_modules=None,
                                   current_key_name=None):
    assert quant_mode.is_weight_only()

    exclude_modules = ['lm_head'
                       ] if exclude_modules is None else exclude_modules

    for name, module in model.named_children():
        if current_key_name is None:
            current_key_name = []
        current_key_name.append(name)

        if len(list(module.children())) > 0:
            weight_only_groupwise_quantize(module, quant_mode, quant_algo,
                                           group_size, pre_quant_scale, zero,
                                           exclude_modules, current_key_name)

        if isinstance(module, ColumnLinear) and name not in exclude_modules:
            if not any(key in '.'.join(current_key_name)
                       for key in exclude_modules):
                model._modules[name] = WeightOnlyGroupwiseQuantColumnLinear(
                    in_features=module.in_features,
                    out_features=module.out_features * module.tp_size,
                    group_size=group_size,
                    pre_quant_scale=pre_quant_scale,
                    zero=zero,
                    bias=module.bias is not None,
                    use_w4a8_awq=quant_algo == W4A8_AWQ,
                    dtype=module.dtype,
                    tp_group=module.tp_group,
                    tp_size=module.tp_size,
                    gather_output=module.gather_output)
        elif isinstance(module, RowLinear) and name not in exclude_modules:
            if not any(key in '.'.join(current_key_name)
                       for key in exclude_modules):
                model._modules[name] = WeightOnlyGroupwiseQuantRowLinear(
                    in_features=module.in_features * module.tp_size,
                    out_features=module.out_features,
                    group_size=group_size,
                    pre_quant_scale=pre_quant_scale,
                    zero=zero,
                    bias=module.bias is not None,
                    use_w4a8_awq=quant_algo == W4A8_AWQ,
                    dtype=module.dtype,
                    tp_group=module.tp_group,
                    tp_size=module.tp_size)

        current_key_name.pop(-1)

    return model


def smooth_quantize_ootb(model,
                         quant_mode,
                         current_key_name=None,
                         exclude_modules=None):
    exclude_modules = ['lm_head'
                       ] if exclude_modules is None else exclude_modules

    for name, module in model.named_children():
        if current_key_name is None:
            current_key_name = []
        current_key_name.append(name)

        if len(list(module.children())) > 0:
            smooth_quantize_ootb(module, quant_mode, exclude_modules,
                                 current_key_name)

        if isinstance(module, ColumnLinear) and name not in exclude_modules:
            if not any(key in '.'.join(current_key_name)
                       for key in exclude_modules):
                model._modules[name] = Int8SmoothQuantLinear(
                    module.in_features, module.out_features * module.tp_size,
                    module.bias, module.dtype, module.tp_group, module.tp_size,
                    module.gather_output)
        elif isinstance(module, RowLinear) and name not in exclude_modules:
            if not any(key in '.'.join(current_key_name)
                       for key in exclude_modules):
                model._modules[name] = Int8SmoothQuantRowLinear(
                    module.in_features * module.tp_size, module.out_features,
                    module.bias, module.dtype, module.tp_group, module.tp_size)

        current_key_name.pop(-1)

    return model


def smooth_quantize_plugin(model, quant_mode):
    for layer_idx, layer in enumerate(model.transformer.layers):
        config = layer.config

        assert hasattr(layer,
                       "input_layernorm"), "The layer has no input_layernorm"
        quant_norm_cls = None
        if isinstance(layer.input_layernorm, RmsNorm):
            quant_norm_cls = SmoothQuantRmsNorm
        elif isinstance(layer.input_layernorm, LayerNorm):
            quant_norm_cls = SmoothQuantLayerNorm
        assert quant_norm_cls is not None
        layer.input_layernorm = quant_norm_cls(
            normalized_shape=config.hidden_size,
            dtype=config.dtype,
            quant_mode=quant_mode)

        assert hasattr(layer, "attention"), "The layer has no attention"
        qkv_bias = layer.attention.qkv.bias is not None
        dense_bias = layer.attention.dense.bias is not None
        head_size = config.head_size if hasattr(config, 'head_size') else None
        layer.attention = SmoothQuantAttention(
            layer_idx=layer_idx,
            hidden_size=config.hidden_size,
            num_attention_heads=config.num_attention_heads,
            num_kv_heads=config.num_key_value_heads,
            attention_head_size=head_size,
            max_position_embeddings=config.max_position_embeddings,
            num_layers=config.num_hidden_layers,
            dtype=config.dtype,
            attention_mask_type=layer.attention.attention_mask_type,
            position_embedding_type=layer.attention.position_embedding_type,
            tp_group=config.mapping.tp_group,
            tp_size=config.mapping.tp_size,
            tp_rank=config.mapping.tp_rank,
            quant_mode=quant_mode,
            bias=(qkv_bias and dense_bias),
            qkv_bias_only=(qkv_bias and not dense_bias))

        assert hasattr(layer, "mlp"), "The layer has no mlp"

        mlp_norm_cls = None
        if isinstance(layer.mlp, GatedMLP):
            mlp_norm_cls = SmoothQuantGatedMLP
        elif isinstance(layer.mlp, MLP):
            mlp_norm_cls = SmoothQuantMLP

        layer.mlp = mlp_norm_cls(hidden_size=config.hidden_size,
                                 ffn_hidden_size=config.intermediate_size,
                                 hidden_act=config.hidden_act,
                                 dtype=config.dtype,
                                 tp_group=config.mapping.tp_group,
                                 tp_size=config.mapping.tp_size,
                                 quant_mode=quant_mode,
                                 bias=layer.mlp.bias)
        assert hasattr(
            layer,
            "post_layernorm"), "The layer has no post_rmspost_layernormnorm"

        quant_norm_cls = None
        if isinstance(layer.post_layernorm, RmsNorm):
            quant_norm_cls = SmoothQuantRmsNorm
        elif isinstance(layer.post_layernorm, LayerNorm):
            quant_norm_cls = SmoothQuantLayerNorm
        assert quant_norm_cls is not None

        layer.post_layernorm = quant_norm_cls(
            normalized_shape=config.hidden_size,
            dtype=config.dtype,
            quant_mode=quant_mode)

    return model


def smooth_quantize(model, quant_mode, use_plugin=False):
    assert quant_mode.has_act_and_weight_quant()
    if use_plugin:
        return smooth_quantize_plugin(model, quant_mode)
    else:
        return smooth_quantize_ootb(model, quant_mode)


def quantize(model, quant_mode, **kwargs):
    if quant_mode.has_act_and_weight_quant():
        if 'sq_use_plugin' in kwargs and kwargs['sq_use_plugin']:
            smooth_quantize(model, quant_mode, use_plugin=True)
        else:
            smooth_quantize(model, quant_mode)
    elif quant_mode.is_weight_only():
        if quant_mode.has_per_group_scaling():
            kwargs = {
                k: kwargs[k]
                for k in [
                    'quant_algo', 'group_size', 'zero', 'pre_quant_scale',
                    'exclude_modules'
                ]
            }
            weight_only_groupwise_quantize(model, quant_mode, **kwargs)
        else:
            kwargs = {k: kwargs[k] for k in ['exclude_modules']}
            weight_only_quantize(model, quant_mode, **kwargs)
