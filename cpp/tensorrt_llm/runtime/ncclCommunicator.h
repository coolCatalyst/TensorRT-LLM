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

#include "tensorrt_llm/runtime/cudaStream.h"
#include "tensorrt_llm/runtime/iBuffer.h"
#include "tensorrt_llm/runtime/worldConfig.h"

struct ncclComm;
typedef struct ncclComm* ncclComm_t;

namespace tensorrt_llm::runtime
{

class NcclCommunicator
{
public:
    template <typename T>
    void send(T* sendbuff, size_t count, int peer, CudaStream const& stream) const;

    template <typename T>
    void send(IBuffer const& buf, int peer, CudaStream const& stream) const
    {
        send(bufferCast<T>(buf), buf.getSize(), peer, stream);
    }

    template <typename T>
    void receive(T* sendbuff, size_t count, int peer, CudaStream const& stream) const;

    template <typename T>
    void receive(IBuffer& buf, int peer, CudaStream const& stream) const
    {
        receive(bufferCast<T>(buf), buf.getSize(), peer, stream);
    }

    static std::shared_ptr<NcclCommunicator> createPipelineComm(WorldConfig const& worldConfig);

private:
    ncclComm_t mComm;
};

} // namespace tensorrt_llm::runtime
