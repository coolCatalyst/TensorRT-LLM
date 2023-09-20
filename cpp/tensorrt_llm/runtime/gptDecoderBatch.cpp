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
#include "tensorrt_llm/runtime/gptDecoderBatch.h"

#include "tensorrt_llm/common/assert.h"
#include "tensorrt_llm/runtime/bufferManager.h"
#include "tensorrt_llm/runtime/runtimeKernels.h"

#include <memory>

using namespace tensorrt_llm::runtime;

namespace tc = tensorrt_llm::common;

namespace
{
SamplingConfig extractSamplingConfig(SamplingConfig const& batchSamplingConfig, SizeType batchIdx)
{
    SamplingConfig samplingConfig{batchSamplingConfig.beamWidth};

    auto extractOptional = [&batchIdx](auto& single, auto const& batch)
    {
        using T = typename std::remove_reference_t<decltype(batch)>::value_type;
        if (batch)
        {
            if (batch->size() > 1)
                single.emplace(T{batch->at(batchIdx)});
            else
                single.emplace(T{batch->at(0)});
        }
    };

    extractOptional(samplingConfig.temperature, batchSamplingConfig.temperature);
    extractOptional(samplingConfig.minLength, batchSamplingConfig.minLength);
    extractOptional(samplingConfig.repetitionPenalty, batchSamplingConfig.repetitionPenalty);
    extractOptional(samplingConfig.presencePenalty, batchSamplingConfig.presencePenalty);
    // sampling layers
    extractOptional(samplingConfig.topK, batchSamplingConfig.topK);
    extractOptional(samplingConfig.topP, batchSamplingConfig.topP);
    extractOptional(samplingConfig.randomSeed, batchSamplingConfig.randomSeed);
    extractOptional(samplingConfig.topPDecay, batchSamplingConfig.topPDecay);
    extractOptional(samplingConfig.topPMin, batchSamplingConfig.topPMin);
    extractOptional(samplingConfig.topPResetIds, batchSamplingConfig.topPResetIds);

    // beam search layer
    samplingConfig.beamSearchDiversityRate = batchSamplingConfig.beamSearchDiversityRate;
    samplingConfig.lengthPenalty = batchSamplingConfig.lengthPenalty;

    return samplingConfig;
}

} // namespace

GptDecoderBatch::GptDecoderBatch(
    std::size_t vocabSize, std::size_t vocabSizePadded, GptDecoderBatch::CudaStreamPtr stream)
    : mVocabSize{vocabSize}
    , mVocabSizePadded{vocabSizePadded}
    , mStream{std::move(stream)}
    , mBufferManager{mStream}
    , mEventStart(tc::CreateEvent())
    , mEventStop(tc::CreateEvent())
{
    auto constexpr nvTokenIdType = TRTDataType<TokenIdType>::value;
    auto constexpr nvSizeType = TRTDataType<SizeType>::value;
    auto constexpr nvFloatType = TRTDataType<float>::value;

    auto& dInput = mJointDecodingInput;
    auto dummyLogits = mBufferManager.emptyTensor(MemoryType::kGPU, nvFloatType);
    auto endIds = mBufferManager.emptyTensor(MemoryType::kGPU, nvTokenIdType);
    dInput = std::make_unique<DecodingInput>(0, 0, std::move(dummyLogits), std::move(endIds));

    dInput->sequenceLimitLength = mBufferManager.emptyTensor(MemoryType::kGPU, nvSizeType);
    dInput->lengths = mBufferManager.emptyTensor(MemoryType::kGPU, nvSizeType);

    auto& dOutput = mJointDecodingOutput;
    auto outputIds = mBufferManager.emptyTensor(MemoryType::kGPU, nvTokenIdType);
    dOutput = std::make_unique<DecodingOutput>(std::move(outputIds));

    dOutput->newTokens = mBufferManager.emptyTensor(MemoryType::kGPU, nvTokenIdType);
    dOutput->parentIds = mBufferManager.emptyTensor(MemoryType::kGPU, nvTokenIdType);
    dOutput->finished = mBufferManager.emptyTensor(MemoryType::kGPU, TRTDataType<bool>::value);
    // use batchSize many entries instead of the usual 1
    dOutput->finishedSum = mBufferManager.emptyTensor(MemoryType::kPINNED, nvSizeType);
    dOutput->lengths = mBufferManager.emptyTensor(MemoryType::kGPU, nvSizeType);
    dOutput->cumLogProbs = mBufferManager.emptyTensor(MemoryType::kGPU, nvFloatType);
    dOutput->beamHypotheses.empty(mBufferManager);
}

void GptDecoderBatch::setup(
    SizeType maxBatchSize, SizeType maxBeamWidth, SizeType maxSequenceLength, nvinfer1::DataType dtype)
{
    TLLM_CHECK(maxBatchSize > 0);
    TLLM_CHECK(maxBeamWidth > 0);
    TLLM_CHECK(maxSequenceLength > 0);

    mActualBatchSize = maxBatchSize;
    mMaxSequenceLength = maxSequenceLength;

    auto const maxBatchSizeShape = ITensor::makeShape({maxBatchSize});
    auto const maxBatchSizeXmaxBeamWidth = ITensor::makeShape({maxBatchSize, maxBeamWidth});

    auto& dInput = *mJointDecodingInput;
    const_cast<ITensor&>(*dInput.endIds).reshape(maxBatchSizeXmaxBeamWidth);
    auto& sequenceLimitLength = const_cast<ITensor&>(*dInput.sequenceLimitLength);
    sequenceLimitLength.reshape(maxBatchSizeShape);
    kernels::invokeFill(sequenceLimitLength, mMaxSequenceLength, *mStream);
    auto& inputLengths = const_cast<ITensor&>(*dInput.lengths);
    inputLengths.reshape(maxBatchSizeXmaxBeamWidth);
    mBufferManager.setZero(inputLengths);

    auto const jointOutputIdsShape = ITensor::makeShape({maxBatchSize, maxBeamWidth, maxSequenceLength});

    auto& dOutput = *mJointDecodingOutput;
    dOutput.ids->reshape(jointOutputIdsShape);
    dOutput.newTokens->reshape(maxBatchSizeXmaxBeamWidth);
    mBufferManager.setZero(*dOutput.newTokens);
    dOutput.parentIds->reshape(jointOutputIdsShape);
    dOutput.finished->reshape(maxBatchSizeXmaxBeamWidth);
    mBufferManager.setZero(*dOutput.finished);
    mBufferManager.setZero(*dOutput.finishedSum);
    dOutput.lengths->reshape(maxBatchSizeXmaxBeamWidth);
    mBufferManager.setZero(*dOutput.lengths);
    dOutput.cumLogProbs->reshape(maxBatchSizeXmaxBeamWidth);
    mBufferManager.setZero(*dOutput.cumLogProbs);
    // use batchSize many entries instead of the usual 1
    dOutput.finishedSum->reshape(maxBatchSizeShape);
    mBufferManager.setZero(*dOutput.finishedSum);

    if (maxBeamWidth > 1)
    {
        dOutput.beamHypotheses.reshape(maxBatchSize, maxBeamWidth, mMaxSequenceLength);
    }
    else
    {
        dOutput.beamHypotheses.release();
    }

    mStreams.resize(maxBatchSize);
    mEvents.resize(maxBatchSize);
    mDecoders.resize(maxBatchSize);
    mDecodingInputs.resize(maxBatchSize);
    mDecodingOutputs.resize(maxBatchSize);
    mNbSteps.resize(maxBatchSize);
    mFinished.resize(maxBatchSize);
    mMaxNewTokens.resize(maxBatchSize);
    mBeamWidths.resize(maxBatchSize);
    auto const device = mStream->getDevice();
    for (SizeType i = 0; i < maxBatchSize; ++i)
    {
        mStreams[i] = std::make_shared<CudaStream>();
        TLLM_CHECK(mStreams[i]->getDevice() == device);
        mEvents[i] = tc::CreateEvent();
        mDecoders[i] = IGptDecoder::create(dtype, mVocabSize, mVocabSizePadded, mStreams[i]);
        mDecodingInputs[i].reset();
        mDecodingOutputs[i].reset();
        mNbSteps[i] = 0;
        mFinished[i] = true;
        mMaxNewTokens[i] = 0;
        mBeamWidths[i] = 0;
    }
}

void GptDecoderBatch::newRequest(
    SizeType batchIdx, decoder_batch::Request const& request, SamplingConfig const& samplingConfig)
{
    TLLM_LOG_DEBUG("%s start", __PRETTY_FUNCTION__);
    TLLM_CHECK(batchIdx >= 0);
    auto const& jointOutputIdsShape = mJointDecodingOutput->ids->getShape();
    auto const batchSize = jointOutputIdsShape.d[0];
    TLLM_CHECK(0 <= batchSize && batchIdx < batchSize);
    auto const maxBeamWidth = jointOutputIdsShape.d[1];
    auto const beamWidth = samplingConfig.beamWidth;
    TLLM_CHECK_WITH_INFO(beamWidth <= maxBeamWidth,
        tc::fmtstr("Beam width (%d) must be smaller than maxBeamWidth (%d) passed to decoder setup function.",
            beamWidth, maxBeamWidth));
    auto const& requestIds = request.ids;
    auto const inputLength = requestIds->getShape().d[0];
    auto const maxNewTokens = request.maxNewTokens.value_or(mMaxSequenceLength - inputLength);
    TLLM_CHECK_WITH_INFO(inputLength + maxNewTokens <= mMaxSequenceLength,
        tc::fmtstr("Input length (%d) + max new tokens (%d) must be less than max sequence length (%d).", inputLength,
            maxNewTokens, mMaxSequenceLength));
    TLLM_CHECK(requestIds->getDataType() == TRTDataType<TokenIdType>::value);
    auto const endId = request.endId.value_or(mVocabSize - 1);
    auto const padId = request.padId.value_or(mVocabSize - 1);

    auto constexpr localBatchSize = 1;

    auto& stream = mStreams[batchIdx];
    BufferManager manager{stream};

    // input
    auto& dJointInput = *mJointDecodingInput;
    auto& dInput = mDecodingInputs.at(batchIdx);

    TensorPtr endIdTensorPtr{ITensor::slice(constPointerCast(dJointInput.endIds), batchIdx, localBatchSize)};
    kernels::invokeFill(*endIdTensorPtr, endId, *stream);
    dInput = std::make_unique<DecodingInput>(inputLength, localBatchSize, dJointInput.logits, endIdTensorPtr);
    dInput->embeddingBias = request.embeddingBias;
    dInput->badWordsList = request.badWordsList;
    dInput->stopWordsList = request.stopWordsList;
    TensorPtr sequenceLimitLength{
        ITensor::slice(constPointerCast(dJointInput.sequenceLimitLength), batchIdx, localBatchSize)};
    kernels::invokeFill(*sequenceLimitLength, inputLength + maxNewTokens, *stream);
    dInput->sequenceLimitLength = std::move(sequenceLimitLength);
    TensorPtr inputLengths{ITensor::slice(constPointerCast(dJointInput.lengths), batchIdx, localBatchSize)};
    kernels::invokeFill(*inputLengths, inputLength, *stream);
    dInput->lengths = inputLengths;

    // output
    auto& dJointOutput = *mJointDecodingOutput;
    auto& dOutput = mDecodingOutputs.at(batchIdx);
    auto const outputIdsShape = ITensor::makeShape({localBatchSize, beamWidth, mMaxSequenceLength});

    TensorPtr outputIds = ITensor::slice(dJointOutput.ids, batchIdx, localBatchSize);
    outputIds->reshape(outputIdsShape);
    dOutput = std::make_unique<DecodingOutput>(outputIds);

    dOutput->finished = ITensor::slice(dJointOutput.finished, batchIdx, localBatchSize);
    manager.setZero(*dOutput->finished);
    dOutput->finishedSum = ITensor::slice(dJointOutput.finishedSum, batchIdx, localBatchSize);
    manager.setZero(*dOutput->finishedSum);
    dOutput->lengths = ITensor::slice(dJointOutput.lengths, batchIdx, localBatchSize);
    kernels::invokeFill(*dOutput->lengths, inputLength, *stream);
    dOutput->cumLogProbs = ITensor::slice(dJointOutput.cumLogProbs, batchIdx, localBatchSize);
    manager.setZero(*IBuffer::slice(dOutput->cumLogProbs, 0, 1));
    dOutput->newTokens = ITensor::slice(dJointOutput.newTokens, batchIdx, localBatchSize);
    manager.setZero(*dOutput->newTokens);

    if (beamWidth > 1)
    {
        kernels::invokeFill(
            *IBuffer::slice(dOutput->cumLogProbs, 1, beamWidth - 1), DecodingOutput::kNegativeInfinity, *stream);

        dOutput->parentIds = ITensor::slice(dJointOutput.parentIds, batchIdx, localBatchSize);
        dOutput->parentIds->reshape(outputIdsShape);
        manager.setZero(*dOutput->parentIds);
        dOutput->beamHypotheses = dJointOutput.beamHypotheses.slice(batchIdx, localBatchSize);
        dOutput->beamHypotheses.init(manager, endId);
    }

    // remaining
    mDecoders[batchIdx]->setup(samplingConfig, localBatchSize);
    mBeamWidths[batchIdx] = beamWidth;
    mNbSteps[batchIdx] = 0;
    mFinished[batchIdx] = false;
    mMaxNewTokens[batchIdx] = maxNewTokens;

    // copy the request ids into outputIds
    auto inputIdsView = ITensor::view(requestIds, ITensor::makeShape({localBatchSize, inputLength}));
    auto outputIdsView = ITensor::view(outputIds, ITensor::makeShape({beamWidth, mMaxSequenceLength}));
    kernels::invokeFill(*outputIdsView, endId, *stream);
    kernels::tileTensor(*outputIdsView, *inputIdsView, beamWidth, *stream);
}

void GptDecoderBatch::forward(decoder_batch::Output& output, decoder_batch::Input const& input)
{
    TLLM_LOG_DEBUG("%s start", __PRETTY_FUNCTION__);
    auto& logits = input.logits;
    auto const& logitsShape = logits->getShape();

    TLLM_CHECK(logitsShape.d[0] == mActualBatchSize);
    auto const& jointOutputIdsShape = mJointDecodingOutput->ids->getShape();
    auto const maxBeamWidth = jointOutputIdsShape.d[1];
    TLLM_CHECK(logitsShape.d[1] == maxBeamWidth);
    TLLM_CHECK(static_cast<std::size_t>(logitsShape.d[2]) == mVocabSizePadded);

    auto& srcCacheIndirection = input.cacheIndirection;
    auto& tgtCacheIndirection = output.cacheIndirection;
    TLLM_CHECK_WITH_INFO((srcCacheIndirection && tgtCacheIndirection) || (!srcCacheIndirection && !tgtCacheIndirection),
        "Specify both srcCacheIndirection and tgtCacheIndirection or neither.");
    TLLM_CHECK(!srcCacheIndirection || srcCacheIndirection->getDataType() == TRTDataType<SizeType>::value);
    TLLM_CHECK(!tgtCacheIndirection || tgtCacheIndirection->getDataType() == TRTDataType<SizeType>::value);

    auto constexpr singleRequest = 1;

    mStream->record(mEventStart.get());
    for (std::int32_t i = 0; i < mActualBatchSize; ++i)
    {
        if (mFinished[i] || !input.active.at(i))
            continue;

        auto& stream = mStreams[i];
        stream->wait(mEventStart.get());
        auto& dInput = *mDecodingInputs[i];
        auto& dOutput = *mDecodingOutputs[i];
        auto logitsView = std::shared_ptr(ITensor::slice(logits, i, singleRequest));
        dInput.logits
            = ITensor::view(logitsView, ITensor::makeShape({singleRequest, mBeamWidths[i], logitsShape.d[2]}));
        if (srcCacheIndirection && tgtCacheIndirection)
        {
            auto srcView = std::shared_ptr(ITensor::slice(srcCacheIndirection, i, singleRequest));
            auto tgtView = std::shared_ptr(ITensor::slice(tgtCacheIndirection, i, singleRequest));
            dInput.cacheIndirection
                = ITensor::view(srcView, ITensor::makeShape({singleRequest, mBeamWidths[i], srcView->getShape().d[2]}));
            dOutput.cacheIndirection
                = ITensor::view(tgtView, ITensor::makeShape({singleRequest, mBeamWidths[i], tgtView->getShape().d[2]}));
        }

        auto& decoder = *mDecoders[i];
        decoder.forwardAsync(dOutput, dInput);

        auto manager = BufferManager{stream};

        auto jointOutputIdsView = ITensor::slice(mJointDecodingOutput->ids, i, singleRequest);
        auto const& jointOutputShape = jointOutputIdsView->getShape();
        // squeeze dim 0 and set beamWidth
        jointOutputIdsView->reshape(ITensor::makeShape({mBeamWidths[i], jointOutputShape.d[2]}));

        manager.copy(*dOutput.ids, *jointOutputIdsView);

        if (mBeamWidths[i] > 1)
        {
            auto jointOutputParentIdsView = ITensor::slice(mJointDecodingOutput->parentIds, i, singleRequest);
            auto const& jointOutputParentIdsShape = jointOutputParentIdsView->getShape();
            // squeeze dim 0 and set beamWidth
            jointOutputParentIdsView->reshape(ITensor::makeShape({mBeamWidths[i], jointOutputParentIdsShape.d[2]}));

            manager.copy(*dOutput.parentIds, *jointOutputParentIdsView);
        }

        auto& event = mEvents[i];
        stream->record(event.get());
        mStream->wait(event.get());
        dInput.step += 1;
        mNbSteps[i] += 1;
    }
    mStream->record(mEventStop.get());
    TLLM_CUDA_CHECK(::cudaEventSynchronize(mEventStop.get()));

    for (std::int32_t i = 0; i < mActualBatchSize; ++i)
    {
        auto& dOutput = *mDecodingOutputs[i];
        mFinished[i] = mNbSteps[i] >= mMaxNewTokens[i]
            // This condition requires the synchronization above
            || *bufferCast<SizeType>(*dOutput.finishedSum) == static_cast<SizeType>(dOutput.finished->getSize());
    }
}

// TODO (rkobus) call this at the end of forward if mFinished[i] changes from false to true?
void GptDecoderBatch::postProcessRequest(SizeType batchIdx) const
{
    auto& stream = mStreams[batchIdx];
    auto manager = BufferManager{stream};

    stream->wait(mEventStart.get());
    auto& dInput = *mDecodingInputs[batchIdx];
    auto& dOutput = *mDecodingOutputs[batchIdx];

    // TODO (rkobus) can we do this inplace?
    auto& outputIds = dOutput.ids;
    auto finalOutputIds = manager.gpu(outputIds->getShape(), outputIds->getDataType());
    IGptDecoder::gatherTree(*finalOutputIds, dOutput, dInput, manager);
    manager.copy(*finalOutputIds, *outputIds);

    auto& event = mEvents[batchIdx];
    stream->record(event.get());
    mStream->wait(event.get());
}

void GptDecoderBatch::newBatch(GenerationInput const& inputs, SamplingConfig const& samplingConfig)
{
    // split batch into single requests
    auto const& inputLengths = inputs.lengths;
    mActualBatchSize = inputLengths->getShape().d[0];

    auto const& jointOutputIdsShape = mJointDecodingOutput->ids->getShape();
    auto const maxBatchSize = jointOutputIdsShape.d[0];
    TLLM_CHECK(mActualBatchSize <= maxBatchSize);
    auto const maxBeamWidth = jointOutputIdsShape.d[1];
    TLLM_CHECK(samplingConfig.beamWidth <= maxBeamWidth);

    auto const inputIdsShape = inputs.ids->getShape();
    TensorPtr inputIdsFlatView = ITensor::view(inputs.ids);
    inputIdsFlatView->reshape(ITensor::makeShape({inputIdsShape.d[1]}));
    auto inputLengthsHost = mBufferManager.copyFrom(*inputLengths, MemoryType::kCPU);
    auto inputLengthsPtr = bufferCast<SizeType>(*inputLengthsHost);
    auto inputOffset = 0;
    for (auto batchIdx = 0; batchIdx < mActualBatchSize; ++batchIdx)
    {
        auto const inputLength = inputLengthsPtr[batchIdx];
        auto const inputShape = ITensor::makeShape({inputLength});
        TensorPtr inputView;
        if (inputs.packed)
        {
            inputView = ITensor::slice(inputIdsFlatView, inputOffset, inputLength);
            inputOffset += inputLength;
        }
        else
        {
            inputView = ITensor::slice(inputs.ids, batchIdx, 1);
            inputView->reshape(inputShape);
        }
        auto request = decoder_batch::Request{inputView, std::nullopt, inputs.endId, inputs.padId};
        request.embeddingBias = inputs.embeddingBiasOpt;
        request.badWordsList = inputs.badWordsList;
        request.stopWordsList = inputs.stopWordsList;
        newRequest(batchIdx, request, extractSamplingConfig(samplingConfig, batchIdx));
    }
}

bool GptDecoderBatch::forward(decoder::Output& output, decoder::Input const& input)
{
    decoder_batch::Input batchInput{input.logits};
    batchInput.cacheIndirection = input.cacheIndirection;

    decoder_batch::Output batchOutput;
    batchOutput.cacheIndirection = output.cacheIndirection;

    forward(batchOutput, batchInput);

    auto finished = getFinished();

    return std::all_of(finished.begin(), finished.end(), [](bool x) { return x; });
}

IStatefulGptDecoder::TensorPtr GptDecoderBatch::getFinalOutputIds() const
{
    for (SizeType batchIdx = 0; batchIdx < mActualBatchSize; ++batchIdx)
    {
        postProcessRequest(batchIdx);
    }
    return ITensor::slice(getOutputIds(), 0, mActualBatchSize);
}
