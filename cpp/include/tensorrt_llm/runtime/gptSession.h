/*
 * Copyright (c) 2022-2023, NVIDIA CORPORATION.  All rights reserved.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

#pragma once

#include "tensorrt_llm/runtime/bufferManager.h"
#include "tensorrt_llm/runtime/common.h"
#include "tensorrt_llm/runtime/generationInput.h"
#include "tensorrt_llm/runtime/generationOutput.h"
#include "tensorrt_llm/runtime/gptModelConfig.h"
#include "tensorrt_llm/runtime/iTensor.h"
#include "tensorrt_llm/runtime/samplingConfig.h"
#include "tensorrt_llm/runtime/worldConfig.h"

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include <NvInferRuntime.h>

namespace tensorrt_llm::batch_manager::kv_cache_manager
{
class KVCacheManager;
}

namespace tensorrt_llm::runtime
{

namespace utils
{
std::vector<uint8_t> loadEngine(std::string const& enginePath);
}

class TllmRuntime;
class IStatefulGptDecoder;
class RuntimeBuffers;

class GptSession
{
public:
    using LoggerPtr = std::shared_ptr<nvinfer1::ILogger>;

    GptSession(GptModelConfig const& modelConfig, WorldConfig const& worldConfig, void const* engineBuffer,
        std::size_t engineSize, LoggerPtr logger = nullptr);

    GptSession(GptModelConfig const& modelConfig, WorldConfig const& worldConfig,
        std::vector<uint8_t> const& engineBuffer, LoggerPtr logger = nullptr)
        : GptSession(modelConfig, worldConfig, engineBuffer.data(), engineBuffer.size(), logger)
    {
    }

    GptSession(GptModelConfig const& modelConfig, WorldConfig const& worldConfig, std::string const& engineFile,
        LoggerPtr logger = nullptr)
        : GptSession(modelConfig, worldConfig, utils::loadEngine(engineFile), logger)
    {
    }

    [[nodiscard]] nvinfer1::ILogger& getLogger() const;

    [[nodiscard]] BufferManager& getBufferManager() const;

    [[nodiscard]] GptModelConfig const& getModelConfig() const
    {
        return mModelConfig;
    }

    [[nodiscard]] WorldConfig const& getWorldConfig() const
    {
        return mWorldConfig;
    }

    [[nodiscard]] int getDevice() const noexcept
    {
        return mDevice;
    }

    [[nodiscard]] bool isCudaGraphMode() const noexcept
    {
        return mCudaGraphMode;
    }

    void setCudaGraphMode(bool value)
    {
        mCudaGraphMode = value;
    }

    void setup(SizeType batchSize, SizeType beamWidth, SizeType maxSequenceLength, bool decoderPerRequest,
        std::optional<SizeType> maxTokensInPagedKvCache = std::nullopt);

    void generate(GenerationOutput& outputs, GenerationInput const& inputs, SamplingConfig const& samplingConfig);

private:
    using KvCacheManager = batch_manager::kv_cache_manager::KVCacheManager;

    void createContexts();
    void createDecoder(bool decoderPerRequest);

    class CudaGraphExecutor
    {
    public:
        CudaGraphExecutor() = default;

        ~CudaGraphExecutor()
        {
            try
            {
                clear();
            }
            catch (std::exception& e)
            {
                TLLM_LOG_EXCEPTION(e);
            }
        }

        bool hasInstance()
        {
            return mInstance != nullptr;
        }

        void create(cudaGraph_t const& graph);
        bool update(cudaGraph_t const& graph);
        void uploadToStream(CudaStream const& stream);
        void launch(CudaStream const& stream);
        void clear();

    private:
        using cudaGraphExecPtr = cudaGraphExec_t;
        cudaGraphExecPtr mInstance;
    };

private:
    GptModelConfig const mModelConfig;
    WorldConfig const mWorldConfig;
    int mDevice{-1};

    SizeType mDecoderMaxSequenceLength{};

    LoggerPtr mLogger;
    std::shared_ptr<TllmRuntime> mRuntime;
    std::shared_ptr<IStatefulGptDecoder> mDecoder;

    std::shared_ptr<RuntimeBuffers> mBuffers;
    std::shared_ptr<KvCacheManager> mKvCacheManager;

    bool mCudaGraphMode{false};
    // ping-pong instances
    std::array<CudaGraphExecutor, 2> mCudaGraphInstances;
};

} // namespace tensorrt_llm::runtime
