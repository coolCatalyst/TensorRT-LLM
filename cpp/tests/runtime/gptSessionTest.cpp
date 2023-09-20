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
#ifndef TOP_LEVEL_DIR
#error "Define TOP_LEVEL_DIR"
#endif

#include <gtest/gtest.h>

#include "tensorrt_llm/common/memoryUtils.h"
#include "tensorrt_llm/common/tensor.h"
#include "tensorrt_llm/runtime/gptJsonConfig.h"
#include "tensorrt_llm/runtime/gptSession.h"
#include "tensorrt_llm/runtime/tllmLogger.h"

#include <algorithm>
#include <filesystem>

#include <NvInferPlugin.h>

using namespace tensorrt_llm::runtime;

namespace tc = tensorrt_llm::common;
namespace fs = std::filesystem;

namespace
{
auto const TEST_RESOURCE_PATH = fs::path{TOP_LEVEL_DIR} / "cpp/tests/resources";
auto const ENGINGE_PATH = TEST_RESOURCE_PATH / "models/rt_engine";
auto const DATA_PATH = TEST_RESOURCE_PATH / "data";

auto const GPT_MODEL_DIR = "gpt2";
auto const GPTJ_MODEL_DIR = "gpt-j-6b";

// Engines need to be generated using cpp/tests/resources/scripts/build_gpt_engines.py.
auto const FP32_GPT_DIR = "fp32-default";
auto const FP32_GPT_ATTENTION_DIR = "fp32-plugin";
auto const FP16_GPT_DIR = "fp16-default";
auto const FP16_GPT_ATTENTION_DIR = "fp16-plugin";
auto const FP16_GPT_ATTENTION_PACKED_DIR = FP16_GPT_ATTENTION_DIR + std::string("-packed");
auto const FP16_GPT_ATTENTION_PACKED_PAGED_DIR = FP16_GPT_ATTENTION_PACKED_DIR + std::string("-paged");
auto const FP16_GPT_ATTENTION_INFLIGHT_BATCHING_DIR = "fp16-inflight-batching-plugin";
auto const FP16_GPT_ATTENTION_INFLIGHT_BATCHING_PAGED_DIR
    = FP16_GPT_ATTENTION_INFLIGHT_BATCHING_DIR + std::string("-paged");

// Expected outputs need to be generated using cpp/tests/resources/scripts/generate_expected_gpt_output.py.
auto const FP32_RESULT_FILE = "output_tokens_fp32.npy";
auto const FP32_PLUGIN_RESULT_FILE = "output_tokens_fp32_plugin.npy";
auto const FP16_RESULT_FILE = "output_tokens_fp16.npy";
auto const FP16_PLUGIN_RESULT_FILE = "output_tokens_fp16_plugin.npy";
auto const FP16_PLUGIN_PACKED_RESULT_FILE = "output_tokens_fp16_plugin_packed.npy";

class ModelSpec
{
public:
    ModelSpec(std::string modelPath, std::string resultsFile, nvinfer1::DataType dtype)
        : mModelPath{std::move(modelPath)}
        , mResultsFile{std::move(resultsFile)}
        , mDataType{dtype}
        , mUseGptAttentionPlugin{false}
        , mUseInflightBatching{false}
        , mUsePackedInput{false}
        , mUsePagedKvCache{false}
        , mDecoderPerRequest{false}
    {
    }

    ModelSpec& useGptAttentionPlugin()
    {
        mUseGptAttentionPlugin = true;
        return *this;
    }

    ModelSpec& useInflightBatching()
    {
        mUseInflightBatching = true;
        return *this;
    }

    ModelSpec& usePackedInput()
    {
        mUsePackedInput = true;
        return *this;
    }

    ModelSpec& usePagedKvCache()
    {
        mUsePagedKvCache = true;
        return *this;
    }

    ModelSpec& useDecoderPerRequest()
    {
        mDecoderPerRequest = true;
        return *this;
    }

    std::string mModelPath;
    std::string mResultsFile;
    nvinfer1::DataType mDataType;
    bool mUseGptAttentionPlugin;
    bool mUseInflightBatching;
    bool mUsePackedInput;
    bool mUsePagedKvCache;
    bool mDecoderPerRequest;
};
} // namespace

class SessionTest : public ::testing::Test // NOLINT(cppcoreguidelines-pro-type-member-init)
{
protected:
    void SetUp() override
    {
        mDeviceCount = tc::getDeviceCount();

        if (mDeviceCount == 0)
            GTEST_SKIP() << "No GPUs found";

        mLogger = std::make_shared<TllmLogger>();

        initLibNvInferPlugins(mLogger.get(), "tensorrt_llm");
    }

    void TearDown() override {}

    int mDeviceCount;
    std::shared_ptr<nvinfer1::ILogger> mLogger{};
};

namespace
{
void verifyModelConfig(GptModelConfig const& modelConfig, ModelSpec const& modelSpec)
{
    ASSERT_EQ(modelSpec.mUseGptAttentionPlugin, modelConfig.useGptAttentionPlugin());
    ASSERT_EQ(modelSpec.mUsePackedInput, modelConfig.usePackedInput());
    ASSERT_EQ(modelSpec.mUsePagedKvCache, modelConfig.usePagedKvCache());
    ASSERT_EQ(modelSpec.mDataType, modelConfig.getDataType());
}

template <int endId = 50256, int padId = 50256>
void testGptSession(fs::path const& modelPath, ModelSpec const& modelSpec, SizeType beamWidth,
    std::initializer_list<int> const& batchSizes, std::string const& resultsFile,
    std::shared_ptr<nvinfer1::ILogger> const& logger, bool replicateFirstInput = false, bool cudaGraphMode = false)
{
    ASSERT_TRUE(fs::exists(DATA_PATH));
    auto givenInput = tc::Tensor::loadNpy(DATA_PATH / "input_tokens.npy", tc::MEMORY_CPU);
    ASSERT_EQ(givenInput.shape.size(), 2);
    ASSERT_GT(givenInput.shape[0], 0);
    auto const nbGivenInputs = static_cast<SizeType>(givenInput.shape[0]);
    auto expectedOutput = tc::Tensor::loadNpy(resultsFile, tc::MEMORY_CPU);
    ASSERT_EQ(expectedOutput.shape.size(), 2);
    ASSERT_EQ(givenInput.shape[0] * beamWidth, expectedOutput.shape[0]);
    auto const givenInputData = givenInput.getPtr<int>();
    auto const expectedOutputData = expectedOutput.getPtr<int>();

    ASSERT_TRUE(fs::exists(modelPath));
    auto const json = GptJsonConfig::parse(modelPath / "config.json");
    auto const modelConfig = json.getModelConfig();
    verifyModelConfig(modelConfig, modelSpec);
    auto const decoderPerRequest = modelSpec.mDecoderPerRequest;

    auto const worldConfig = WorldConfig::mpi(*logger);
    auto const enginePath = modelPath / json.engineFilename(worldConfig);

    auto const maxInputLength = static_cast<SizeType>(givenInput.shape[1]);
    auto const maxSeqLength = static_cast<SizeType>(expectedOutput.shape[1]);
    ASSERT_LT(maxInputLength, maxSeqLength);
    auto const maxNewTokens = maxSeqLength - maxInputLength;
    SamplingConfig samplingConfig{beamWidth};
    samplingConfig.temperature = std::vector{1.0f};
    samplingConfig.minLength = std::vector{1};
    samplingConfig.randomSeed = std::vector{42ull};
    samplingConfig.topK = std::vector{0};
    samplingConfig.topP = std::vector{0.0f};

    std::vector<SizeType> givenInputLengths(nbGivenInputs);
    for (SizeType i = 0; i < nbGivenInputs; ++i)
    {
        auto const seqBegin = givenInputData + i * maxInputLength;
        auto const it = std::find(seqBegin, seqBegin + maxInputLength, padId);
        givenInputLengths[i] = std::distance(seqBegin, it);
    }

    GptSession session{modelConfig, worldConfig, enginePath, logger};
    session.setCudaGraphMode(cudaGraphMode);
    EXPECT_EQ(session.getDevice(), worldConfig.getDevice());
    // Use bufferManager for copying data to and from the GPU
    auto& bufferManager = session.getBufferManager();

    auto maxBatchSize = *std::max_element(batchSizes.begin(), batchSizes.end());
    session.setup(maxBatchSize, beamWidth, maxSeqLength, decoderPerRequest);

    for (auto const batchSize : batchSizes)
    {
        std::cout << "=== batchSize:" << batchSize << " ===\n";

        // use 5 to 12 tokens from input
        std::vector<SizeType> inputLenghtsHost(batchSize);
        for (SizeType i = 0; i < batchSize; ++i)
        {
            const int inputIdx = replicateFirstInput ? 0 : i % nbGivenInputs;
            inputLenghtsHost[i] = givenInputLengths[inputIdx];
        }
        auto inputLenghts = bufferManager.copyFrom(inputLenghtsHost, ITensor::makeShape({batchSize}), MemoryType::kGPU);

        // copy inputs and wrap into shared_ptr
        GenerationInput::TensorPtr inputIds;
        if (modelConfig.usePackedInput())
        {
            std::vector<SizeType> inputOffsetsHost(batchSize + 1);
            std::inclusive_scan(inputLenghtsHost.begin(), inputLenghtsHost.end(), inputOffsetsHost.begin() + 1);
            auto const totalInputSize = inputOffsetsHost.back();

            std::vector<std::int32_t> inputsHost(totalInputSize);
            for (SizeType i = 0; i < batchSize; ++i)
            {
                auto const seqBegin = givenInputData + (replicateFirstInput ? 0 : (i % nbGivenInputs) * maxInputLength);
                std::copy(seqBegin, seqBegin + inputLenghtsHost[i], inputsHost.begin() + inputOffsetsHost[i]);
            }
            inputIds = bufferManager.copyFrom(inputsHost, ITensor::makeShape({1, totalInputSize}), MemoryType::kGPU);
        }
        else
        {
            std::vector<std::int32_t> inputsHost(batchSize * maxInputLength, padId);
            for (SizeType i = 0; i < batchSize; ++i)
            {
                auto const seqBegin = givenInputData + (replicateFirstInput ? 0 : (i % nbGivenInputs) * maxInputLength);
                std::copy(seqBegin, seqBegin + inputLenghtsHost[i], inputsHost.begin() + i * maxInputLength);
            }
            inputIds
                = bufferManager.copyFrom(inputsHost, ITensor::makeShape({batchSize, maxInputLength}), MemoryType::kGPU);
        }

        GenerationInput generationInput{
            endId, padId, std::move(inputIds), std::move(inputLenghts), modelConfig.usePackedInput()};

        // runtime will allocate memory for output if this tensor is empty
        GenerationOutput generationOutput{bufferManager.emptyTensor(MemoryType::kGPU, nvinfer1::DataType::kINT32)};

        // repeat the same inputs multiple times for testing idempotency of `generate()`
        auto constexpr repetitions = 10;
        for (auto r = 0; r < repetitions; ++r)
        {
            SizeType numSteps = 0;
            generationOutput.onTokenGenerated
                = [&numSteps, maxNewTokens]([[maybe_unused]] GenerationOutput::TensorPtr const& outputIds,
                      [[maybe_unused]] SizeType step, bool finished)
            {
                ++numSteps;
                EXPECT_TRUE(!finished || numSteps == maxNewTokens);
            };
            session.generate(generationOutput, generationInput, samplingConfig);
            EXPECT_EQ(numSteps, maxNewTokens);

            // compare outputs
            auto const& outputIds = generationOutput.ids;
            auto const& outputDims = outputIds->getShape();
            EXPECT_EQ(outputDims.nbDims, 3);
            EXPECT_EQ(outputDims.d[0], batchSize) << "r: " << r;
            EXPECT_EQ(outputDims.d[1], beamWidth) << "r: " << r;
            EXPECT_EQ(outputDims.d[2], maxSeqLength) << "r: " << r;
            auto outputHost = bufferManager.copyFrom(*outputIds, MemoryType::kCPU);
            auto output = bufferCast<std::int32_t>(*outputHost);
            bufferManager.getStream().synchronize();
            for (auto b = 0; b < batchSize; ++b)
            {
                for (auto beam = 0; beam < beamWidth; ++beam)
                {
                    bool anyMismatch = false;
                    for (auto i = 0; i < maxSeqLength; ++i)
                    {
                        auto const outputIndex = tc::flat_index3(b, beam, i, beamWidth, maxSeqLength);
                        const int expectedBatch = replicateFirstInput ? 0 : b;
                        auto const expectIndex
                            = tc::flat_index2((expectedBatch % nbGivenInputs * beamWidth + beam), i, maxSeqLength);
                        EXPECT_EQ(output[outputIndex], expectedOutputData[expectIndex])
                            << " b: " << b << " beam: " << beam << " i: " << i;
                        anyMismatch |= (output[outputIndex] != expectedOutputData[expectIndex]);
                    }
                    ASSERT_FALSE(anyMismatch) << "batchSize: " << batchSize << ", r: " << r << ", b: " << b;
                }
            }

            // make sure to recreate the outputs in the next repetition
            outputIds->release();
        }
    }

    free(givenInputData);
    free(expectedOutputData);
}

auto constexpr kBatchSizes = {1, 8};

using ParamType = std::tuple<char const*, ModelSpec, SizeType, bool>;

std::string generateTestName(const testing::TestParamInfo<ParamType>& info)
{
    auto const modelSpec = std::get<1>(info.param);
    std::string name{modelSpec.mDataType == nvinfer1::DataType::kFLOAT ? "Float" : "Half"};
    auto const beamWidth = std::get<2>(info.param);
    name.append(beamWidth == 1 ? "Sampling" : "BeamWidth" + std::to_string(beamWidth));
    if (modelSpec.mUseGptAttentionPlugin)
        name.append("GptAttentionPlugin");
    if (modelSpec.mUseInflightBatching)
        name.append("WithInflightBatching");
    if (modelSpec.mUsePackedInput)
        name.append("Packed");
    if (modelSpec.mUsePagedKvCache)
        name.append("PagedKvCache");
    if (modelSpec.mDecoderPerRequest)
        name.append("DecoderBatch");
    if (std::get<3>(info.param))
        name.append("CudaGraph");
    return name;
}
} // namespace

class ParamTest : public SessionTest, public ::testing::WithParamInterface<ParamType>
{
};

TEST_P(ParamTest, Test)
{
    auto const modelDir = std::get<0>(GetParam());
    auto const modelSpec = std::get<1>(GetParam());
    auto const modelPath{ENGINGE_PATH / modelDir / modelSpec.mModelPath / "1-gpu"};
    SizeType const beamWidth{std::get<2>(GetParam())};
    auto const resultsPath
        = DATA_PATH / modelDir / ((beamWidth == 1) ? "sampling" : "beam_search_" + std::to_string(beamWidth));
    std::string const resultsFile{resultsPath / modelSpec.mResultsFile};

    if (!modelSpec.mUseGptAttentionPlugin && beamWidth > 1)
        GTEST_SKIP();

    auto const replicateFirstInput = false;
    auto const cudaGraphMode = std::get<3>(GetParam());

    testGptSession(
        modelPath, modelSpec, beamWidth, kBatchSizes, resultsFile, mLogger, replicateFirstInput, cudaGraphMode);
}

INSTANTIATE_TEST_SUITE_P(GptSessionTest, ParamTest,
    testing::Combine(testing::Values(GPT_MODEL_DIR),
        testing::Values(
            // single decoder
            ModelSpec{FP32_GPT_DIR, FP32_RESULT_FILE, nvinfer1::DataType::kFLOAT},
            ModelSpec{FP32_GPT_ATTENTION_DIR, FP32_PLUGIN_RESULT_FILE, nvinfer1::DataType::kFLOAT}
                .useGptAttentionPlugin(),
            ModelSpec{FP16_GPT_DIR, FP16_RESULT_FILE, nvinfer1::DataType::kHALF},
            ModelSpec{FP16_GPT_ATTENTION_DIR, FP16_PLUGIN_RESULT_FILE, nvinfer1::DataType::kHALF}
                .useGptAttentionPlugin(),
            ModelSpec{FP16_GPT_ATTENTION_PACKED_DIR, FP16_PLUGIN_PACKED_RESULT_FILE, nvinfer1::DataType::kHALF}
                .useGptAttentionPlugin()
                .usePackedInput(),
            ModelSpec{FP16_GPT_ATTENTION_PACKED_PAGED_DIR, FP16_PLUGIN_PACKED_RESULT_FILE, nvinfer1::DataType::kHALF}
                .useGptAttentionPlugin()
                .usePackedInput()
                .usePagedKvCache(),
            // ModelSpec{
            //     FP16_GPT_ATTENTION_INFLIGHT_BATCHING_DIR, FP16_PLUGIN_PACKED_RESULT_FILE, nvinfer1::DataType::kHALF}
            //     .useGptAttentionPlugin()
            //     .useInflightBatching()
            //     .usePackedInput(),
            ModelSpec{FP16_GPT_ATTENTION_INFLIGHT_BATCHING_PAGED_DIR, FP16_PLUGIN_PACKED_RESULT_FILE,
                nvinfer1::DataType::kHALF}
                .useGptAttentionPlugin()
                .useInflightBatching()
                .usePackedInput()
                .usePagedKvCache(),
            // decoderBatch
            ModelSpec{FP32_GPT_DIR, FP32_RESULT_FILE, nvinfer1::DataType::kFLOAT}.useDecoderPerRequest(),
            ModelSpec{FP32_GPT_ATTENTION_DIR, FP32_PLUGIN_RESULT_FILE, nvinfer1::DataType::kFLOAT}
                .useGptAttentionPlugin()
                .useDecoderPerRequest(),
            ModelSpec{FP16_GPT_DIR, FP16_RESULT_FILE, nvinfer1::DataType::kHALF}.useDecoderPerRequest(),
            ModelSpec{FP16_GPT_ATTENTION_DIR, FP16_PLUGIN_RESULT_FILE, nvinfer1::DataType::kHALF}
                .useGptAttentionPlugin()
                .useDecoderPerRequest(),
            ModelSpec{FP16_GPT_ATTENTION_PACKED_DIR, FP16_PLUGIN_PACKED_RESULT_FILE, nvinfer1::DataType::kHALF}
                .useGptAttentionPlugin()
                .usePackedInput()
                .useDecoderPerRequest(),
            ModelSpec{FP16_GPT_ATTENTION_PACKED_PAGED_DIR, FP16_PLUGIN_PACKED_RESULT_FILE, nvinfer1::DataType::kHALF}
                .useGptAttentionPlugin()
                .usePackedInput()
                .usePagedKvCache()
                .useDecoderPerRequest(),
            // ModelSpec{
            //     FP16_GPT_ATTENTION_INFLIGHT_BATCHING_DIR, FP16_PLUGIN_PACKED_RESULT_FILE, nvinfer1::DataType::kHALF}
            //     .useGptAttentionPlugin()
            //     .useInflightBatching()
            //     .usePackedInput()
            //     .useDecoderPerRequest(),
            ModelSpec{FP16_GPT_ATTENTION_INFLIGHT_BATCHING_PAGED_DIR, FP16_PLUGIN_PACKED_RESULT_FILE,
                nvinfer1::DataType::kHALF}
                .useGptAttentionPlugin()
                .useInflightBatching()
                .usePackedInput()
                .usePagedKvCache()
                .useDecoderPerRequest()

                ),
        testing::Values(1, 2), testing::Values(false, true)),
    generateTestName);

INSTANTIATE_TEST_SUITE_P(GptjSessionTest, ParamTest,
    testing::Combine(testing::Values(GPTJ_MODEL_DIR),
        testing::Values(
            // single decoder
            ModelSpec{FP16_GPT_ATTENTION_DIR, FP16_PLUGIN_RESULT_FILE, nvinfer1::DataType::kHALF}
                .useGptAttentionPlugin(),
            ModelSpec{FP16_GPT_ATTENTION_PACKED_DIR, FP16_PLUGIN_PACKED_RESULT_FILE, nvinfer1::DataType::kHALF}
                .useGptAttentionPlugin()
                .usePackedInput(),
            ModelSpec{FP16_GPT_ATTENTION_INFLIGHT_BATCHING_PAGED_DIR, FP16_PLUGIN_PACKED_RESULT_FILE,
                nvinfer1::DataType::kHALF}
                .useGptAttentionPlugin()
                .usePackedInput()
                .usePagedKvCache(),
            // decoderBatch
            ModelSpec{FP16_GPT_ATTENTION_DIR, FP16_PLUGIN_RESULT_FILE, nvinfer1::DataType::kHALF}
                .useGptAttentionPlugin()
                .useDecoderPerRequest(),
            ModelSpec{FP16_GPT_ATTENTION_PACKED_DIR, FP16_PLUGIN_PACKED_RESULT_FILE, nvinfer1::DataType::kHALF}
                .useGptAttentionPlugin()
                .usePackedInput()
                .useDecoderPerRequest(),
            ModelSpec{FP16_GPT_ATTENTION_INFLIGHT_BATCHING_PAGED_DIR, FP16_PLUGIN_PACKED_RESULT_FILE,
                nvinfer1::DataType::kHALF}
                .useGptAttentionPlugin()
                .usePackedInput()
                .usePagedKvCache()
                .useDecoderPerRequest()

                ),
        testing::Values(1, 2), testing::Values(false)),
    generateTestName);

class LlamaSessionTest : public SessionTest
{
};

TEST_F(LlamaSessionTest, SamplingFP16WithAttentionPlugin)
{
    GTEST_SKIP() << "Run only on demand";
    auto const modelDir = "llama_7bf";
    auto const engineDir = "llama_7bf_outputs_tp1";
    auto const modelPath{ENGINGE_PATH / modelDir / engineDir};
    SizeType constexpr beamWidth{1};
    std::string resultsFile{DATA_PATH / modelDir / FP16_RESULT_FILE};
    auto const batchSizes = {8};

    auto constexpr dtype = nvinfer1::DataType::kHALF;
    auto const modelSpec = ModelSpec{"", "", dtype}.useGptAttentionPlugin();

    testGptSession<2, 2>(modelPath, modelSpec, beamWidth, batchSizes, resultsFile, mLogger);
}

TEST_F(LlamaSessionTest, SamplingFP16AttentionPluginDecoderBatch)
{
    GTEST_SKIP() << "Run only on demand";
    auto const modelDir = "llamav2";
    auto const modelPath{ENGINGE_PATH / modelDir};
    SizeType constexpr beamWidth{1};
    std::string resultsFile{DATA_PATH / modelDir / FP16_RESULT_FILE};
    auto const batchSizes = {8};

    auto constexpr dtype = nvinfer1::DataType::kHALF;
    auto const modelSpec = ModelSpec{"", "", dtype}.useGptAttentionPlugin().usePackedInput().useDecoderPerRequest();

    testGptSession<2, 2>(modelPath, modelSpec, beamWidth, batchSizes, resultsFile, mLogger);
}
