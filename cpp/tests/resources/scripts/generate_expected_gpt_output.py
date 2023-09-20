#!/usr/bin/env python3
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

from pathlib import Path

import run


def generate_output(engine: str,
                    num_beams: int,
                    output_name: str,
                    max_output_len: int = 8):

    model = 'gpt2'
    resources_dir = Path(__file__).parent.resolve().parent
    models_dir = resources_dir / 'models'
    engine_dir = models_dir / 'rt_engine' / model / engine / '1-gpu/'

    data_dir = resources_dir / 'data'
    input_file = data_dir / 'input_tokens.npy'
    model_data_dir = data_dir / model
    if num_beams <= 1:
        output_dir = model_data_dir / 'sampling'
    else:
        output_dir = model_data_dir / ('beam_search_' + str(num_beams))

    run.generate(engine_dir=str(engine_dir),
                 input_file=str(input_file),
                 tokenizer_path=str(models_dir / model),
                 output_npy=str(output_dir / (output_name + '.npy')),
                 output_csv=str(output_dir / (output_name + '.csv')),
                 max_output_len=max_output_len,
                 num_beams=num_beams)


def generate_outputs(num_beams):
    print('Generating GPT2 FP32 outputs')
    if num_beams == 1:
        generate_output(engine='fp32-default',
                        num_beams=num_beams,
                        output_name='output_tokens_fp32')
    generate_output(engine='fp32-plugin',
                    num_beams=num_beams,
                    output_name='output_tokens_fp32_plugin')

    print('Generating GPT2 FP16 outputs')
    if num_beams == 1:
        generate_output(engine='fp16-default',
                        num_beams=num_beams,
                        output_name='output_tokens_fp16')
    generate_output(engine='fp16-plugin',
                    num_beams=num_beams,
                    output_name='output_tokens_fp16_plugin')
    generate_output(engine='fp16-plugin-packed',
                    num_beams=num_beams,
                    output_name='output_tokens_fp16_plugin_packed')
    generate_output(engine='fp16-plugin-packed-paged',
                    num_beams=num_beams,
                    output_name='output_tokens_fp16_plugin_packed_paged')


if __name__ == '__main__':
    generate_outputs(num_beams=1)
    generate_outputs(num_beams=2)
