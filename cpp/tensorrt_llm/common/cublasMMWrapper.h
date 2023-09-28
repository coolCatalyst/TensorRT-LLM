/*
 * Copyright (c) 2019-2022, NVIDIA CORPORATION.  All rights reserved.
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

#include "tensorrt_llm/common/cublasAlgoMap.h"
#include "tensorrt_llm/common/cudaUtils.h"
#include <cublasLt.h>
#include <cublas_v2.h>
#include <cuda_runtime.h>
#include <map>
#include <mutex>
#include <optional>
#include <string>

namespace tensorrt_llm
{
namespace common
{

class cublasMMWrapper
{
protected:
    std::shared_ptr<cublasHandle_t> cublas_handle_;
    std::shared_ptr<cublasLtHandle_t> cublaslt_handle_;

    cudaDataType_t Atype_;
    cudaDataType_t Btype_;
    cudaDataType_t Ctype_;
    cudaDataType_t computeType_;

    cudaStream_t stream_;
    cublasAlgoMap* cublas_algo_map_;
    std::mutex* mu_;

    void* cublas_workspace_ = nullptr;

    friend class cublasINT8MMWrapper;

    void _Int8Gemm(const int m, const int n, const int k, const int8_t* A, const int lda, const int8_t* B,
        const int ldb, void* C, const int ldc, const void* alpha, const int mode, const bool per_column_scaling);

public:
    cublasMMWrapper(std::shared_ptr<cublasHandle_t> cublasHandle, std::shared_ptr<cublasLtHandle_t> cublasLtHandle,
        cudaStream_t stream, cublasAlgoMap* map, std::mutex* mu, void* workspace);

    ~cublasMMWrapper();

    cublasMMWrapper(const cublasMMWrapper& wrapper);

    cublasStatus_t cublasLtMatmulWrapper(cublasLtHandle_t lightHandle, cublasLtMatmulDesc_t computeDesc,
        const void* alpha, const void* A, cublasLtMatrixLayout_t Adesc, const void* B, cublasLtMatrixLayout_t Bdesc,
        const void* beta, const void* C, cublasLtMatrixLayout_t Cdesc, void* D, cublasLtMatrixLayout_t Ddesc,
        const cublasLtMatmulAlgo_t* algo, void* workspace, size_t workspaceSizeInBytes, cudaStream_t stream);

    bool checkTactic(cublasOperation_t transa, cublasOperation_t transb, const int m, const int n, const int k,
        const int lda, const int ldb, const int ldc, const cublasLtMatmulHeuristicResult_t& algo) const;

    std::vector<cublasLtMatmulHeuristicResult_t> getTactics(cublasOperation_t transa, cublasOperation_t transb,
        const int m, const int n, const int k, const int lda, const int ldb, const int ldc);

    std::vector<cublasLtMatmulHeuristicResult_t> getTactics(cublasLtHandle_t lightHandle,
        cublasLtMatmulDesc_t computeDesc, cublasLtMatrixLayout_t Adesc, cublasLtMatrixLayout_t Bdesc,
        cublasLtMatrixLayout_t Cdesc, cublasLtMatrixLayout_t Ddesc);

    std::pair<bool, cublasLtMatmulAlgo_t> findBestAlgo(cublasLtHandle_t lightHandle, cublasLtMatmulDesc_t computeDesc,
        const void* alpha, const void* A, cublasLtMatrixLayout_t Adesc, const void* B, cublasLtMatrixLayout_t Bdesc,
        const void* beta, const void* C, cublasLtMatrixLayout_t Cdesc, void* D, cublasLtMatrixLayout_t Ddesc,
        cudaStream_t stream);

    using MatrixLayout = std::tuple<cudaDataType_t, cublasLtOrder_t, uint64_t, uint64_t>;
    using cache_idx_t = std::tuple<cublasLtMatmulDesc_t, std::array<MatrixLayout, 4>>;
    std::map<cache_idx_t, cublasLtMatmulAlgo_t> algo_cache;

    MatrixLayout createMatrixLayout(cublasLtMatrixLayout_t Mdesc);

    void Gemm(cublasOperation_t transa, cublasOperation_t transb, const int m, const int n, const int k,
        const void* alpha, const void* A, cudaDataType_t Atype, int lda, const void* B, cudaDataType_t Btype, int ldb,
        const void* beta, void* C, cudaDataType_t Ctype, int ldc, cudaDataType_t computeType, cublasGemmAlgo_t algo);

    void Gemm(cublasOperation_t transa, cublasOperation_t transb, const int m, const int n, const int k, const void* A,
        const int lda, const void* B, const int ldb, void* C, const int ldc);

    void Gemm(cublasOperation_t transa, cublasOperation_t transb, const int m, const int n, const int k, const void* A,
        const int lda, const void* B, const int ldb, void* C, const int ldc,
        const std::optional<cublasLtMatmulHeuristicResult_t>& algo);

    void Gemm(cublasOperation_t transa, cublasOperation_t transb, const int m, const int n, const int k, const void* A,
        const int lda, const void* B, const int ldb, void* C, const int ldc, float f_alpha, float f_beta);

    void Gemm(cublasOperation_t transa, cublasOperation_t transb, const int m, const int n, const int k, const void* A,
        const int lda, const void* B, const int ldb, void* C, const int ldc, float f_alpha, float f_beta,
        const cublasLtMatmulAlgo_t& algo, bool hasAlgo);

    void setWorkspace(void* workspace);

    void Int8Gemm(const int m, const int n, const int k, const int8_t* A, const int lda, const int8_t* B, const int ldb,
        int8_t* C, const int ldc, const float* alpha, const bool per_column_scaling = false);

    void Int8Gemm(const int m, const int n, const int k, const int8_t* A, const int lda, const int8_t* B, const int ldb,
        int32_t* C, const int ldc);

    void setFP32GemmConfig();
    void setFP16GemmConfig();
#ifdef ENABLE_BF16
    void setBF16GemmConfig();
#endif
#ifdef ENABLE_FP8
    void setFP8GemmConfig(cudaDataType_t outputType = CUDA_R_16F);
#endif

    void setStream(cudaStream_t stream);

    void setGemmConfig(cudaDataType_t aType, cudaDataType_t bType, cudaDataType_t cType, cudaDataType_t computeType);

    CublasDataType getCublasDataType(cudaDataType_t data_type);

#if (CUDART_VERSION >= 11000)
    void Gemm(cublasOperation_t transa, cublasOperation_t transb, const int m, const int n, const int k, const void* A,
        const int lda, const void* B, const int ldb, const void* bias, void* C, const int ldc);
#endif

    void stridedBatchedGemm(cublasOperation_t transa, cublasOperation_t transb, const int m, const int n, const int k,
        const void* A, const int lda, const int64_t strideA, const void* B, const int ldb, const int64_t strideB,
        void* C, const int ldc, const int64_t strideC, const int batchCount, const float f_alpha = 1.0f,
        const float f_beta = 0.0f);

    void stridedBatchedGemm(cublasOperation_t transa, cublasOperation_t transb, const int m, const int n, const int k,
        const float f_alpha, const void* A, cudaDataType_t AType, const int lda, const int64_t strideA, const void* B,
        cudaDataType_t BType, const int ldb, const int64_t strideB, const float f_beta, void* C, cudaDataType_t CType,
        const int ldc, const int64_t strideC, const int batch_count, cudaDataType_t computeType);

    void batchedGemm(cublasOperation_t transa, cublasOperation_t transb, const int m, const int n, const int k,
        const void* const* A, const int lda, const void* const* B, const int ldb, void* const* C, const int ldc,
        const int batch_count);

    bool isFuseBatchGemm(const int batch_count, const int m, const int k, const int n);

    cublasHandle_t getCublasHandle()
    {
        return *(this->cublas_handle_);
    }

    cublasLtHandle_t getCublasLtHandle() const
    {
        return *(this->cublaslt_handle_);
    }
};

} // namespace common

} // namespace tensorrt_llm
