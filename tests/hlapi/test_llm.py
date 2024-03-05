import asyncio
import os
import sys
import tempfile
from typing import List

import pytest
import torch
from transformers import AutoTokenizer

from tensorrt_llm.hlapi.llm import (LLM, ModelConfig, SamplingConfig,
                                    TokenizerBase, TransformersTokenizer)
from tensorrt_llm.hlapi.utils import get_total_gpu_memory


def get_model_path(model_name):
    sys.path.append(os.path.join(os.path.dirname(__file__), '..'))
    from utils.llm_data import llm_models_root
    return str(llm_models_root() / model_name)


default_model_name = "llama-models/llama-7b-hf"
mixtral_model_name = "Mixtral-8x7B-v0.1"

llama_model_path = get_model_path(default_model_name)
llm_engine_dir = os.environ.get('LLM_ENGINE_DIR', './tmp.engine')
prompts = ["Tell a story", "Who are you"]

cur_dir = os.path.dirname(os.path.abspath(__file__))
models_root = os.path.join(cur_dir, '../../models')


def test_tokenizer():
    tokenizer = TransformersTokenizer.from_pretrained(llama_model_path)

    res = tokenizer("hello world")
    assert res


def test_llm_loading_from_hf():
    config = ModelConfig(llama_model_path)
    llm = LLM(config, kvcache_free_gpu_memory_fraction=0.4)

    for output in llm.generate(prompts):
        print(output)


class MyTokenizer(TokenizerBase):
    ''' A wrapper for the Transformers' tokenizer.
    This is the default tokenizer for LLM. '''

    @classmethod
    def from_pretrained(cls, pretrained_model_dir: str, **kwargs):
        tokenizer = AutoTokenizer.from_pretrained(pretrained_model_dir,
                                                  **kwargs)
        return MyTokenizer(tokenizer)

    def __init__(self, tokenizer):
        self.tokenizer = tokenizer

    @property
    def eos_token_id(self) -> int:
        return self.tokenizer.eos_token_id

    @property
    def pad_token_id(self) -> int:
        return self.tokenizer.pad_token_id

    def encode(self, text: str, **kwargs) -> List[int]:
        return self.tokenizer.encode(text, **kwargs)

    def decode(self, token_ids: List[int], **kwargs) -> str:
        return self.tokenizer.decode(token_ids, **kwargs)

    def batch_encode_plus(self, texts: List[str], **kwargs) -> dict:
        return self.tokenizer.batch_encode_plus(texts, **kwargs)


def test_llm_with_customized_tokenizer():
    config = ModelConfig(llama_model_path)
    llm = LLM(
        config,
        # a customized tokenizer is passed to override the default one
        tokenizer=MyTokenizer.from_pretrained(config.model_dir),
        kvcache_free_gpu_memory_fraction=0.4,
    )

    for output in llm.generate(prompts):
        print(output)


def test_llm_without_tokenizer():
    config = ModelConfig(llama_model_path)
    llm = LLM(
        config,
        # this will turn off tokenizer for pre-processing and post-processing
        enable_tokenizer=False,
        kvcache_free_gpu_memory_fraction=0.4,
    )

    sampling_config = SamplingConfig(end_id=2,
                                     pad_id=2,
                                     output_sequence_lengths=True,
                                     return_dict=True)

    prompts = [[23, 14, 3]]

    for output in llm.generate(prompts, sampling_config=sampling_config):
        print(output)


@pytest.mark.skipif(torch.cuda.device_count() < 2,
                    reason="The test needs at least 2 GPUs, skipping")
def test_llm_build_engine_for_tp2(model_name=default_model_name):
    config = ModelConfig(get_model_path(model_name))
    config.parallel_config.tp_size = 2
    llm = LLM(
        config,
        kvcache_free_gpu_memory_fraction=0.4,
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        llm.save(tmpdir)


@pytest.mark.skipif(torch.cuda.device_count() < 2,
                    reason="The test needs at least 2 GPUs, skipping")
def test_llm_generate_for_tp2():
    config = ModelConfig(llama_model_path)
    config.parallel_config.tp_size = 2
    llm = LLM(config, kvcache_free_gpu_memory_fraction=0.4)
    for output in llm.generate(prompts):
        print(output)


# TODO[chunweiy]: Move mixtral test to the e2e test
def is_memory_enough_for_mixtral():
    if torch.cuda.device_count() < 2:
        return False
    try:
        total_memory = get_total_gpu_memory(0) + get_total_gpu_memory(1)
        if total_memory >= 160 * 1024**3:
            return True
    except:
        return False


@pytest.mark.skipif(torch.cuda.device_count() < 2,
                    reason="The test needs at least 2 GPUs, skipping")
@pytest.mark.skipif(not is_memory_enough_for_mixtral(),
                    reason="The test needs at least 160GB memory, skipping")
def test_llm_generate_mixtral_for_tp2():
    config = ModelConfig(get_model_path(mixtral_model_name))
    config.parallel_config.tp_size = 2
    llm = LLM(config, kvcache_free_gpu_memory_fraction=0.4)
    for output in llm.generate(prompts):
        print(output)


def test_llm_generate_async(model_name=default_model_name, tp_size: int = 1):
    config = ModelConfig(llama_model_path)
    config.parallel_config.tp_size = tp_size
    llm = LLM(
        config,
        # set to 40%, since by default, the executor will occupy all the free memory, making some other tests OOM in CI
        kvcache_free_gpu_memory_fraction=0.4)

    def test_async(streaming: bool):

        async def task(prompt: str):
            outputs = []
            async for output in llm.generate_async(prompt, streaming=streaming):
                print('output', output)
                outputs.append(output.text)
            print(' '.join(outputs))

        async def main():
            tasks = [task(prompt) for prompt in prompts]
            await asyncio.gather(*tasks)

        asyncio.run(main())

    def test_wait(streaming: bool):
        for prompt in prompts:
            future = llm.generate_async(prompt, streaming=streaming)
            for output in future:
                print('wait', output)

    def test_non_streaming_usage_wait():
        for prompt in prompts:
            output = llm.generate_async(prompt, streaming=False)
            print(output.text)

    def test_future(streaming: bool):
        for prompt in prompts:
            future = llm.generate_async(prompt, streaming=streaming)
            if streaming is True:
                for output in future:
                    # Do something else and then wait for the result if needed
                    output = output.wait_completion(timeout=10)
                    print('future', output.text)
            else:
                # Do something else and then wait for the result if needed
                output = future.wait_completion(timeout=10)
                print('future', output.text)

    test_async(streaming=True)
    test_async(streaming=False)
    test_wait(streaming=True)
    test_wait(streaming=False)
    test_future(streaming=True)
    test_future(streaming=False)
    test_non_streaming_usage_wait()


@pytest.mark.skipif(torch.cuda.device_count() < 2,
                    reason="The test needs at least 2 GPUs, skipping")
def test_llm_generate_async_tp2():
    test_llm_generate_async(default_model_name, tp_size=2)


# TODO[chunweiy]: Add test for loading inmemory model
