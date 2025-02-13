//==============================================================================
//
//  @@-COPYRIGHT-START-@@
//
//  Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
//
//  Redistribution and use in source and binary forms, with or without
//  modification, are permitted provided that the following conditions are met:
//
//  1. Redistributions of source code must retain the above copyright notice,
//     this list of conditions and the following disclaimer.
//
//  2. Redistributions in binary form must reproduce the above copyright notice,
//     this list of conditions and the following disclaimer in the documentation
//     and/or other materials provided with the distribution.
//
//  3. Neither the name of the copyright holder nor the names of its contributors
//     may be used to endorse or promote products derived from this software
//     without specific prior written permission.
//
//  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
//  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
//  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
//  ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
//  LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
//  CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
//  SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
//  INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
//  CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
//  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
//  POSSIBILITY OF SUCH DAMAGE.
//
//  SPDX-License-Identifier: BSD-3-Clause
//
//  @@-COPYRIGHT-END-@@
//
//==============================================================================

#include "cuda_util.hpp"
#include "tensor_utils.hpp"

namespace DlQuantization
{

template <typename DTYPE>
__global__ void permuteTensorKernel(const DTYPE* in, DTYPE* out, int numElements, int numDims,
                                    const TensorDim* inputStrides, const TensorDim* outputStrides)
{
    for (size_t i = blockIdx.x * blockDim.x + threadIdx.x; i < numElements; i += blockDim.x * gridDim.x)
    {
        size_t outputIdx = 0;
        size_t remainder = i;
        for (auto dim = 0; dim < numDims; dim++)
        {
            size_t dimIdx = remainder / inputStrides[dim];
            remainder     = remainder - dimIdx * inputStrides[dim];
            outputIdx += outputStrides[dim] * dimIdx;
        }

        out[outputIdx] = in[i];
    }
}


template <typename T>
void permuteKernelGPU(const T* inTensor, T* outTensor, size_t numel, const TensorDims& inputStrides,
                      const TensorDims& outputStrides, void* stream)
{
    size_t numDims       = inputStrides.size();
    int64_t totalThreads = numel;
    int64_t gridSize     = CUDA_NUM_BLOCKS(totalThreads);
    TensorDim strideData[2][numDims];
    auto cuStream = static_cast<cudaStream_t>(stream);

    // Copy the stride information to the cuda device
    for (int i = 0; i < numDims; i++)
    {
        strideData[0][i] = inputStrides[i];
        strideData[1][i] = outputStrides[i];
    }
    TensorDim* deviceStrideData;
    cudaMalloc((void**) &deviceStrideData, 2 * numDims * sizeof(TensorDim));
    cudaMemcpyAsync(deviceStrideData, strideData, 2 * numDims * sizeof(TensorDim), cudaMemcpyHostToDevice, cuStream);

    // Launch the cuda kernel
    permuteTensorKernel<<<gridSize, CUDA_NUM_THREADS, 0, cuStream>>>(inTensor, outTensor, numel, numDims,
                                                                     deviceStrideData, deviceStrideData + numDims);

    // Free the device stride data
    cudaFree(deviceStrideData);
}


template void permuteKernelGPU(const float* intensor, float* outTensor, size_t numel, const TensorDims& inputStrides,
                               const TensorDims& outputStrides, void* stream);

template void permuteKernelGPU(const double* intensor, double* outTensor, size_t numel, const TensorDims& inputStrides,
                               const TensorDims& outputStrides, void* stream);

}   // namespace DlQuantization
