# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2022-2024, Qualcomm Innovation Center, Inc. All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#
#  1. Redistributions of source code must retain the above copyright notice,
#     this list of conditions and the following disclaimer.
#
#  2. Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions and the following disclaimer in the documentation
#     and/or other materials provided with the distribution.
#
#  3. Neither the name of the copyright holder nor the names of its contributors
#     may be used to endorse or promote products derived from this software
#     without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
#  ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
#  LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
#  CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
#  SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
#  INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
#  CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
#  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#  POSSIBILITY OF SUCH DAMAGE.
#
#  SPDX-License-Identifier: BSD-3-Clause
#
#  @@-COPYRIGHT-END-@@
# =============================================================================
""" Implementation for simulating models running on Quantized hardware """

import contextlib
import tempfile
from dataclasses import dataclass
from pathlib import Path
import os
from typing import Dict, List, Union, Tuple, Optional
import itertools
import json
import warnings
import numpy as np
import onnx

from onnx import helper
import onnxruntime as ort
from onnxruntime import SessionOptions, InferenceSession
from onnxruntime.quantization.onnx_quantizer import ONNXModel
from packaging import version

from aimet_common import _libpymo as libpymo, quantsim
from aimet_common import libquant_info
from aimet_common.defs import QuantScheme, QuantizationDataType
from aimet_common.quantsim import extract_global_quantizer_args, VALID_ENCODING_VERSIONS
from aimet_common.utils import save_json_yaml, AimetLogger, _red
from aimet_common.connected_graph.product import Product
from aimet_onnx import utils
from aimet_onnx.meta.operations import Op
from aimet_onnx.meta.utils import get_op_given_param_name, get_param_shape_using_connected_graph
from aimet_onnx.meta.connectedgraph import ConnectedGraph
from aimet_onnx.qc_quantize_op import QcQuantizeOp, OpMode, TensorQuantizerParams, GroupedBlockQuantizeDequantize
from aimet_onnx.quantsim_config.quantsim_config import QuantSimConfigurator
from aimet_onnx.utils import make_dummy_input, save_model_with_external_weights, add_hook_to_get_activation, \
    remove_activation_hooks

logger = AimetLogger.get_area_logger(AimetLogger.LogAreas.Quant)

# pylint: disable=no-name-in-module, ungrouped-imports, too-many-lines
if version.parse(onnx.__version__) >= version.parse("1.14.0"):
    from onnx import ModelProto
else:
    from onnx.onnx_pb import ModelProto

# List of ops whose outputs are not to be quantized
op_outputs_to_ignore = ["branch", "Flatten", "Gather", "Reshape", "Shape", "Unsqueeze", "Squeeze", "Split",
                        "Compress", "Tile", "Transpose", "Identity"]

# List of ops whose params are not to be quantized
op_params_to_ignore = ['Resize']

allowed_op_type_for_per_channel = ['Conv', 'Gemm', 'MatMul', 'ConvTranspose']

# List of op types whose input and output quantizers to be tied
op_types_to_tie_qtzrs = ['Concat', 'MaxPool', 'AveragePool', 'Resize', 'Max', 'ReduceMax', 'Min', 'ReduceMin']
_tie_qtzrs = False

data_types_to_quantize = [np.float32]


@contextlib.contextmanager
def _apply_constraints(flag: bool):
    """
    Apply runtime specific constraints.
    For certain ``op_types_to_tie_qtzrs``, runtime has constraints to have same encodings for
     input and output quantizers.

    NOTE: Default setting doesn't apply these constraints.
    """
    global _tie_qtzrs # pylint: disable=global-statement
    orig_flag = _tie_qtzrs
    try:
        _tie_qtzrs = flag
        yield
    finally:
        _tie_qtzrs = orig_flag


@dataclass
class EncodingMismatchInfo:
    """
    Dataclass tracking information about mismatched quantizer vs. encoding settings.
    """
    quantizer_name: str
    enabled_mismatch: Optional[Tuple] = None
    dtype_mismatch: Optional[Tuple] = None
    bitwidth_mismatch: Optional[Tuple] = None
    is_symmetric_mismatch: Optional[Tuple] = None
    is_strict_symmetric_mismatch: Optional[Tuple] = None
    is_unsigned_symmetric_mismatch: Optional[Tuple] = None

    def has_mismatch(self) -> bool:
        """
        Returns True if there is a mismatched setting.

        :return: True if there is a mismatched setting, False otherwise
        """
        return (self.enabled_mismatch is not None or
                self.dtype_mismatch is not None or
                self.bitwidth_mismatch is not None or
                self.is_symmetric_mismatch is not None or
                self.is_strict_symmetric_mismatch is not None or
                self.is_unsigned_symmetric_mismatch is not None)


class QuantizationSimModel:
    """ Creates a QuantizationSimModel model by adding quantization simulations ops to a given model """

    # pylint: disable=too-many-arguments, too-many-locals, too-many-instance-attributes
    def __init__(self,
                 model: ModelProto,
                 dummy_input: Dict[str, np.ndarray] = None,
                 quant_scheme: QuantScheme = QuantScheme.post_training_tf_enhanced,
                 rounding_mode: str = 'nearest',
                 default_param_bw: int = 8,
                 default_activation_bw: int = 8,
                 use_symmetric_encodings: bool = False, use_cuda: bool = True,
                 device: int = 0, config_file: str = None,
                 default_data_type: QuantizationDataType = QuantizationDataType.int,
                 user_onnx_libs: List[str] = None, path: str = None):
        """
        Constructor

        :param model: ONNX model
        :param dummy_input: Dummy input to the model. If None, will attempt to auto-generate a dummy input
        :param quant_scheme: Quantization scheme (e.g. QuantScheme.post_training_tf)
        :param rounding_mode: Rounding mode (e.g. nearest)
        :param default_param_bw: Quantization bitwidth for parameter
        :param default_activation_bw: Quantization bitwidth for activation
        :param use_symmetric_encodings: True if symmetric encoding is used.  False otherwise.
        :param use_cuda: True if using CUDA to run quantization op. False otherwise.
        :param config_file: Path to Configuration file for model quantizers
        :param default_data_type: Default data type to use for quantizing all layer inputs, outputs and parameters.
                                 Possible options are QuantizationDataType.int and QuantizationDataType.float.
                                 Note that the mode default_data_type=QuantizationDataType.float is only supported with
                                 default_output_bw=16 and default_param_bw=16
        :param user_onnx_libs: List of paths to all compiled ONNX custom ops libraries
        :param path: Directory to save the artifacts.
        """
        self.model = model
        if not isinstance(model, ONNXModel):
            self.model = ONNXModel(model)
        if not dummy_input:
            dummy_input = make_dummy_input(self.model.model)
        self.qc_quantize_op_dict = {}
        self.connected_graph = ConnectedGraph(self.model)
        self._quant_scheme = quant_scheme
        self._rounding_mode = rounding_mode
        self._default_param_bw = default_param_bw
        self._default_activation_bw = default_activation_bw
        self._default_quantization_data_type = default_data_type
        self._use_symmetric_encodings = use_symmetric_encodings
        self._use_cuda = use_cuda
        if 'CUDAExecutionProvider' not in ort.get_available_providers():
            self._use_cuda = False
        if self._use_cuda:
            self._op_domain = "aimet.customop.cuda"
            self.providers = [('CUDAExecutionProvider', {'device_id': device, 'cudnn_conv_algo_search': 'DEFAULT'}), 'CPUExecutionProvider']
        else:
            self._op_domain = "aimet.customop.cpu"
            self.providers = ['CPUExecutionProvider']
        self._user_onnx_libs = user_onnx_libs
        self.param_names = []
        self.input_quantizers_name = []
        self.activation_names = []
        self.activation_dtypes = {}
        self._path = path if path else tempfile.mkdtemp()
        if not os.path.exists(self._path):
            os.makedirs(self._path, exist_ok=True)

        # Get names of parameters and activations to quantize
        self._get_param_names()
        self._get_activations_to_quantize(dummy_input)

        self._add_quantization_nodes()

        # Apply configurations based on provided config file.
        quantsim_configurator = self._add_configuration_(config_file)
        self._hw_version = quantsim_configurator._get_hw_version()
        self._supported_kernels = quantsim_configurator.get_supported_kernels()
        self._op_to_supported_kernel = quantsim_configurator.get_op_to_supported_kernels()
        self.quant_args = extract_global_quantizer_args(quant_scheme, quantsim_configurator)
        self._apply_exception_rules()
        self._tie_quantizers()

        # Build onnxruntime inference session
        self.session = QuantizationSimModel.build_session(self.model.model, self.providers,
                                                          user_onnx_libs=self._user_onnx_libs, path=self._path)

    def get_supported_kernels(self) -> Dict:
        """
        Return _supported_kernels parsed from the config file
        :return: Dictionary containing supported_kernels
        """
        return self._supported_kernels

    def _add_configuration_(self, config_file: str):
        """
        Add configuration based on config file

        :param config_file: Path to Configuration file for model quantizers
        """
        quantsim_configurator = QuantSimConfigurator(self.model, self.connected_graph, config_file,
                                                     self._default_activation_bw, self._default_param_bw,
                                                     self._default_quantization_data_type)
        quantsim_configurator.configure_quantizers(self.qc_quantize_op_dict, self.param_names, self.activation_names,
                                                   self.input_quantizers_name)

        return quantsim_configurator

    def _get_param_names(self):
        """
        Get the names of params
        """
        valid_ops = self._get_ops_with_parameter()
        for op in valid_ops:
            for param_info in op.parameters.values():
                param, _ = param_info
                if param.name and param.name not in self.param_names:
                    self.param_names.append(param.name)

    def _get_ops_with_parameter(self) -> List[Op]:
        """
        Gets ops with parameters to add quantization nodes for

        :return: Connected graph ops
        """
        valid_ops = list(self.connected_graph.get_all_ops().values())
        return valid_ops

    def _get_activations_to_quantize(self, dummy_input: Dict[str, np.ndarray]):
        """
        Get the names of activations to quantize

        :param dummy_input: Sample input to be run through the model
        """
        try:
            self.activation_dtypes = self._infer_activation_dtypes()
        except onnx.shape_inference.InferenceError:
            self.activation_dtypes = self._observe_activation_dtypes(dummy_input)

        self.input_name_to_nodes = self.model.input_name_to_nodes()
        self.output_name_to_node = self.model.output_name_to_node()

        # Capture model inputs
        for node in self.model.graph().input:
            name = node.name
            if name not in self.activation_names and name not in self.param_names and self._is_tensor_quantizable(name):
                self.activation_names.append(name)

        # Capture intermediate activations and model outputs
        for node in self.model.nodes():
            for name in node.input:
                if name not in self.activation_names and name not in self.param_names and self._is_tensor_quantizable(name):
                    self.activation_names.append(name)
                    self.input_quantizers_name.append(name)

            for name in node.output:
                if name not in self.activation_names and name not in self.param_names and self._is_tensor_quantizable(name):
                    self.activation_names.append(name)

        # Rename model output node
        for node in self.model.graph().output:
            if node.name in self.activation_names:
                node.name += '_updated'

    def _is_tensor_quantizable(self, name: str) -> bool:
        """
        Checks whether the given tensor should be quantized

        :param name: Name of the tensor
        :return: True if the tensor should be quantized
        """
        # Check if the tensor data-type can be quantized
        if name in self.model.get_initializer_name_set():  # static activation
            if self.model.get_initializer(name).data_type != 1:  # 1 corresponds to float, dictionary can be found by using onnx.TensorProto.DataType.items()
                return False
        else:  # dynamic activation
            if name not in self.activation_dtypes or self.activation_dtypes[name] not in data_types_to_quantize:
                return False

        # Check if the tensor is param to certain ops (eg: Resize)
        consumer_nodes = self.input_name_to_nodes.get(name)
        if consumer_nodes:
            for consumer_node in consumer_nodes:
                if consumer_node.op_type in op_params_to_ignore and \
                        consumer_node.input[0] != name:  # except first input rest are params (only valid for unary ops)
                    return False

        # Check if the tensor is output of certain ops
        producer_node = self.output_name_to_node.get(name)
        if producer_node and producer_node.op_type in op_outputs_to_ignore:
            return False

        return True

    def _is_matmul_bias_add(self, cg_op: Op) -> bool:
        """
        For given node, check if the previous and the current nodes are of type 'MatMul' and 'Add' respectively.

        NOTE:
        Linear = (Matmul -> Add) gets fused into a single MatMul / FullyConnected HTP op.
        Second input of Add (Bias) needs to be either uint8 or int32.
        This utility will find such pattern and help ensure that the second input of Add op (bias) won't be configured
         with activation precision.

        :param cg_op: ConnectedGraph op
        :return: True if the MatMul + Add pattern is found, False otherwise.
        """
        if cg_op.type != "Add":
            return False

        for inp1, inp2 in itertools.permutations(cg_op.inputs):
            if not inp1.producer or inp1.producer.type != "MatMul":
                continue
            if len(inp1.consumers) > 1:
                return False

            param = utils.ParamUtils.get_param_by_name(self.model.model, inp2.name)
            # TODO: Refine this check. Checks that param is static tensor with rank 1
            return param and len(param.dims) == 1

        return False

    def _infer_activation_dtypes(self):
        """
        Get the data type for each activation through shape inference
        """
        if self.model.model.ByteSize() >= onnx.checker.MAXIMUM_PROTOBUF:
            with tempfile.TemporaryDirectory(dir=self._path) as tempdir:
                save_path = os.path.join(tempdir, "inferred_model.onnx")
                save_model_with_external_weights(self.model.model, save_path, location=Path(save_path).name + ".data")
                onnx.shape_inference.infer_shapes_path(save_path)
                # Do not load the weights for the shape inference model, we only need to access the graph's `value_info`
                inferred_model = onnx.load(save_path, load_external_data=False)
        else:
            inferred_model = onnx.shape_inference.infer_shapes(self.model.model)

        activation_dtypes = {}
        for val_info in itertools.chain(inferred_model.graph.value_info,
                                        inferred_model.graph.input,
                                        inferred_model.graph.output):
            act_name = val_info.name
            dtype = onnx.mapping.TENSOR_TYPE_TO_NP_TYPE[val_info.type.tensor_type.elem_type]
            activation_dtypes[act_name] = dtype
        return activation_dtypes

    def _observe_activation_dtypes(self, dummy_input: Dict[str, np.ndarray]):
        """
        Get the data type for each activation by returning all activations

        :param dummy_input: Sample input to run through the model
        """
        activations = utils.get_graph_intermediate_activations(self.model.graph())
        hooks = []
        for name in activations:
            hooks.append(add_hook_to_get_activation(self.model.model, name))
        sess = QuantizationSimModel.build_session(self.model.model, ['CPUExecutionProvider'],
                                                  user_onnx_libs=self._user_onnx_libs, path=self._path)
        outputs = sess.run(None, dummy_input)

        activation_dtypes = {}
        for idx, node in enumerate(self.model.graph().output):
            act_name = node.name
            dtype = outputs[idx].dtype
            activation_dtypes[act_name] = dtype
        remove_activation_hooks(self.model.model, hooks)
        return activation_dtypes

    def _add_quantization_nodes(self):
        """
        Call insert functions for quantization nodes
        """
        self._insert_param_quantization_nodes()
        self._insert_activation_quantization_nodes()

    def _replace_input_of_all_nodes(self, old_name, new_name):
        if old_name not in self.connected_graph.get_all_products():
            raise ValueError(f"Tensor name {old_name} was not found in graph tensors "
                             f"{self.connected_graph.get_all_products().keys()}.")

        product = self.connected_graph.get_all_products()[old_name]
        for consumer in product.consumers:
            node = consumer.get_module()
            for idx, tensor in enumerate(node.input):
                if tensor == old_name:
                    node.input[idx] = new_name

    def _insert_param_quantization_nodes(self):
        """
        Insert quantization node for each param tensor
        """
        for name in self.param_names:
            self._replace_input_of_all_nodes(name, name + '_qdq')

            quant_info, tensor_quantizer_params = self._create_quant_info_object_for_param(name)
            custom_node = helper.make_node(
                op_type='QcQuantizeOp',
                inputs=[name],
                outputs=[name + '_qdq'],
                name='QcQuantizeOp_' + name,
                domain=self._op_domain,
                op_name=name,
                quant_info=libpymo.PtrToInt64(quant_info),
            )
            self.model.add_node(custom_node)
            self.qc_quantize_op_dict[name] = QcQuantizeOp(quant_info=quant_info,
                                                          quant_scheme=self._quant_scheme,
                                                          rounding_mode=self._rounding_mode,
                                                          op_mode=OpMode.oneShotQuantizeDequantize,
                                                          bitwidth=self._default_param_bw,
                                                          use_symmetric_encodings=self._use_symmetric_encodings,
                                                          tensor_quantizer_params=tensor_quantizer_params)

    def _create_quant_info_object_for_param(self, param_name: str):
        """
        Creates quant info object for QcQuantizeOp and QDQ node

        :param param_name: Name of the parameter for which the quant info object will be created
        :return: quant info object
        """
        quant_info = libquant_info.QcQuantizeInfo()
        quant_info.usePerChannelMode = False
        op = get_op_given_param_name(self.connected_graph, param_name)
        param_shape = get_param_shape_using_connected_graph(self.connected_graph, param_name)
        tensor_quantizer_params = TensorQuantizerParams(param_shape)

        if len(param_shape) == 1:
            tensor_quantizer_params.channel_axis = 0
            tensor_quantizer_params.block_axis = None
        else:
            channel_axis, block_axis = self._get_quantization_axes(op)
            tensor_quantizer_params.channel_axis = channel_axis
            tensor_quantizer_params.block_axis = block_axis

        return quant_info, tensor_quantizer_params

    @staticmethod
    def _get_quantization_axes(op: Op) -> Tuple[int, int]:
        """
        Gets quantization axes for per-channel and blockwise quantization

        :param op: Connected graph op
        :return: (channel axis, block axis)
        """
        if op.type in ['Conv']:
            return 0, 1
        if op.type in ['ConvTranspose']:
            return 1, 0
        if op.type in ['Gemm']:
            if op.transposed_params:
                return 0, 1
            return 1, 0
        if op.type in ['MatMul']:
            return -1, -2
        return None, None

    def _insert_activation_quantization_nodes(self):
        """
        Insert quantization node for each activation tensor
        """
        for name in self.activation_names:
            self._replace_input_of_all_nodes(name, name + '_updated')
            quant_info = libquant_info.QcQuantizeInfo()
            custom_node = helper.make_node(
                op_type='QcQuantizeOp',
                inputs=[name],
                outputs=[name + '_updated'],
                name='QcQuantizeOp_' + name,
                domain=self._op_domain,
                op_name=name,
                quant_info=libpymo.PtrToInt64(quant_info)
            )
            self.model.add_node(custom_node)
            self.qc_quantize_op_dict[name] = QcQuantizeOp(quant_info=quant_info,
                                                          quant_scheme=self._quant_scheme,
                                                          rounding_mode=self._rounding_mode,
                                                          op_mode=OpMode.updateStats,
                                                          bitwidth=self._default_activation_bw,
                                                          use_symmetric_encodings=self._use_symmetric_encodings)

    @staticmethod
    def build_session(model: onnx.ModelProto, providers: List, user_onnx_libs: List[str] = None, path: str = None):
        """
        Build and return onnxruntime inference session

        :param model: onnx model
        :param providers: providers to execute onnxruntime
        :param user_onnx_libs: list of paths to user custom ONNX op libraries
        :param path: path where to store model external data
        """
        sess_options = SessionOptions()
        shared_library = os.path.dirname(libquant_info.__file__)
        shared_library = os.path.join(shared_library, "libaimet_onnxrt_ops.so")
        sess_options.register_custom_ops_library(shared_library)
        if user_onnx_libs is not None:
            for lib in user_onnx_libs:
                sess_options.register_custom_ops_library(lib)

        # Convert and save ONNX model to external data if larger than 2GB.
        # External data will be saved under same directory.
        path = path if path else tempfile.mkdtemp()
        if not os.path.exists(path):
            os.makedirs(path, exist_ok=True)
        model_size = model.ByteSize()
        save_as_external_data = model_size >= onnx.checker.MAXIMUM_PROTOBUF
        output_path = os.path.join(path, 'model.onnx')
        if save_as_external_data:
            # Note: Saving as external data mutates the saved model, removing all initializer data
            save_model_with_external_weights(model, output_path, location=Path(output_path).name + ".data")

        path_or_bytes = output_path if save_as_external_data else model.SerializeToString()
        session = InferenceSession(
            path_or_bytes=path_or_bytes,
            sess_options=sess_options,
            providers=providers,
        )
        return session

    def get_qc_quantize_op(self):
        """
        Return dict of qc quantize ops
        """
        return self.qc_quantize_op_dict

    def get_op_quantizers(self, op: Op) -> (List, List, Dict):
        """
        This function returns the input, output and param quantizers of the given connected graph op.

        :param op: Connected Graph Op
        :return: list of input quantizers, list of output quantizers and dictionary of param quantizers
        """
        input_quantizers = []
        output_quantizers = []
        param_quantizers = {}

        # Capture as input quantizer if tensor is not a layer output or parameter
        for cg_product in op.inputs:
            if not cg_product.producer and not cg_product.is_parm:
                input_name = cg_product.name
                if input_name in self.qc_quantize_op_dict:
                    input_quantizers.append(self.qc_quantize_op_dict[input_name])

        # Capture output quantizers of the op
        for cg_product in op.outputs:
            if cg_product.name in self.qc_quantize_op_dict:
                output_quantizers.append(self.qc_quantize_op_dict[cg_product.name])

        # Capture param quantizers of the op
        for param_name, (_, param_type) in op.parameters.items():
            if param_name in self.qc_quantize_op_dict:
                param_quantizers[param_type] = self.qc_quantize_op_dict[param_name]

        return input_quantizers, output_quantizers, param_quantizers

    def _apply_exception_rules(self):
        """
        Apply exception rules to specific op. For example, a rule can override high bitwidth to GroupNorm op.
        """
        # pylint:disable = too-many-branches
        for op in self.connected_graph.get_all_ops().values():
            _, output_quantizers, param_quantizers = self.get_op_quantizers(op)

            if op.type == 'GroupNormalization':
                if self._hw_version not in {'V73', 'V75', 'V79', 'V81'}:
                    continue
                if 'weight' in param_quantizers:
                    output_quantizer = output_quantizers[0]
                    for _, param_quantizer in param_quantizers.items():
                        param_quantizer.bitwidth = output_quantizer.bitwidth
                        param_quantizer.use_symmetric_encodings = output_quantizer.use_symmetric_encodings

            elif op.type == 'MatMul':
                # Apply exception rule only to dynamic matmuls
                if op.inputs[1].name in self.param_names:
                    continue
                target_quantizer_for_first_input = self._get_closest_enabled_quantizer(op.inputs[0])
                target_quantizer_for_second_input = self._get_closest_enabled_quantizer(op.inputs[1])

                # According to opdef for Matmul in HTP:
                # 16bit Weight(second input for dynamic MatMul) must have 16bit Activation(first input for dynamic MatMul).
                # 16bit Activation and 16bit Weight require minimum arch V73.
                # 16bit Weight must be symmetric quantized.

                # Below are the possible combinations for MatMul with 8/16 bitwidth:
                # If version is V73/V75: {input0->8, input1->8 symm/asymm} {input0->16 , input1->8 symm/asymm} {input0->16, input1->16 symmetric}
                # If version is lesser than V73: {input0->8, input1->8 symmetric} {input0->16, input1->8 symmetric}
                if self._hw_version in {'V66', 'V68', 'V69'}:
                    if target_quantizer_for_second_input is None:
                        logger.warning("The target quantizer for second input could not be found. MatMul exception rule does not apply for op: %s.", op.name)
                    else:
                        target_quantizer_for_second_input.use_symmetric_encodings = True
                        target_quantizer_for_second_input.bitwidth = 8
                elif self._hw_version in {'V73', 'V75', 'V79', 'V81'}:
                    if target_quantizer_for_first_input is None or target_quantizer_for_second_input is None:
                        logger.warning("The target quantizers could not be found. MatMul exception rule does not apply for op: %s.", op.name)
                    elif target_quantizer_for_second_input.bitwidth == 16:
                        target_quantizer_for_second_input.use_symmetric_encodings = True
                        target_quantizer_for_first_input.bitwidth = 16

            elif self._is_matmul_bias_add(op):
                # Disable intermediate output quantization and bias quantization
                for inp in op.inputs:
                    if inp.name in self.qc_quantize_op_dict:
                        self.qc_quantize_op_dict[inp.name].enabled = False

    def _get_closest_enabled_quantizer(self, tensor: Product):
        """
        Returns closest enabled quantizer to `tensor` traversing upwards

        :param tensor: Tensor for which to find quantizer
        """
        quantizer = self.qc_quantize_op_dict.get(tensor.name, None)
        if quantizer and quantizer.enabled:
            return quantizer
        if not tensor.producer:
            return None
        if not tensor.producer.inputs:
            return None
        # Assume first input to parent op is the relevant upstream activation
        upstream_tensor = tensor.producer.inputs[0]
        return self._get_closest_enabled_quantizer(upstream_tensor)

    def save_model_graph(self, filename_prefix: str):
        """
        Save model to given path

        :param filename_prefix: filename to save the onnx model
        """
        self.model.save_model_to_file(os.path.join(self._path, filename_prefix) + '.onnx')

    def compute_encodings(self, forward_pass_callback, forward_pass_callback_args):
        """
        Compute and return the encodings of each tensor quantizer

        :param forward_pass_callback: A callback function that simply runs forward passes on the model. This callback
            function should use representative data for the forward pass, so the calculated encodings work for all
            data samples. This callback internally chooses the number of data samples it wants to use for calculating
            encodings.
        :param forward_pass_callback_args: These argument(s) are passed to the forward_pass_callback as-is. Up to
            the user to determine the type of this parameter. E.g. could be simply an integer representing the number
            of data samples to use. Or could be a tuple of parameters or an object representing something more complex.
            If set to None, forward_pass_callback will be invoked with no parameters.
        """
        for op_name, qc_op in self.qc_quantize_op_dict.items():
            qc_op.reset_encoding_stats()
            if op_name in self.activation_names:
                qc_op.op_mode = OpMode.updateStats
            else:
                qc_op.op_mode = OpMode.oneShotQuantizeDequantize
                if qc_op.is_encoding_frozen():
                    qc_op.op_mode = OpMode.quantizeDequantize

        forward_pass_callback(self.session, forward_pass_callback_args)
        for op_name, qc_op in self.qc_quantize_op_dict.items():
            if qc_op.data_type == QuantizationDataType.int and not qc_op.is_encoding_frozen():
                qc_op.compute_encodings()
            qc_op.op_mode = OpMode.quantizeDequantize

    def _get_encodings(self, quantizer_names, enc_version):
        encoding_dict = {}
        for name in quantizer_names:
            encoding = self.qc_quantize_op_dict[name].export_encodings(enc_version)
            if encoding is None:
                continue
            encoding_dict[name] = encoding

        if version.parse(enc_version) < version.parse("1.0.0"):
            return encoding_dict

        for name, encoding in encoding_dict.items():
            encoding["name"] = name
        return list(encoding_dict.values())

    def _export_encodings(self, encoding_file_path):
        """
        Export encodings to json file

        :param encoding_file_path: path to save the encoding file
        """
        enc_version = quantsim.encoding_version
        if enc_version not in VALID_ENCODING_VERSIONS:
            raise NotImplementedError(f'Encoding version {enc_version} not in set of valid encoding '
                                      f'versions {VALID_ENCODING_VERSIONS}.')

        param_encodings = self._get_encodings(self.param_names, enc_version)
        activation_encodings = self._get_encodings(self.activation_names, enc_version)

        encodings_dict = {'version': enc_version,
                          'activation_encodings': activation_encodings,
                          'param_encodings': param_encodings,
                          'quantizer_args': self.quant_args}

        save_json_yaml(encoding_file_path, encodings_dict)

    def remove_quantization_nodes(self):
        """
        Remove quantization nodes
        """
        self.model = self.remove_quantizers(self.model)

    @staticmethod
    def remove_quantizers(model: ONNXModel):
        """
        Removes all QcQuantizeOp layers from model
        """
        nodes_to_remove = []
        for node in model.nodes():
            if node.op_type == 'QcQuantizeOp':
                nodes_to_remove.append(node)
            else:
                for name in node.input:
                    model.replace_input_of_all_nodes(name, name.replace('_qdq', '').replace('_updated', ''))
        model.remove_nodes(nodes_to_remove)

        for node in model.graph().output:
            node.name = node.name.replace('_updated', '')

        return model

    def export(self, path: str, filename_prefix: str):
        """
        Compute encodings and export to files

        :param path: dir to save encoding files
        :param filename_prefix: filename to save encoding files
        """
        if quantsim.encoding_version == '0.6.1':
            msg = _red("Encoding version 0.6.1 will be deprecated in a future release, with version 1.0.0 becoming "
                       "the default. If your code depends on parsing the exported encodings file, ensure that it is "
                       "updated to be able to parse 1.0.0 format.\n"
                       "To swap the encoding version to 1.0.0, run the following lines prior to calling quantsim "
                       "export:\n\n"
                       "from aimet_common import quantsim\n"
                       "quantsim.encoding_version = '1.0.0'")
            warnings.warn(msg, DeprecationWarning, stacklevel=2)
        self._export_encodings(os.path.join(path, filename_prefix) + '.encodings')
        self.remove_quantization_nodes()
        if self.model.model.ByteSize() >= onnx.checker.MAXIMUM_PROTOBUF:
            # Note: Saving as external data mutates the saved model, removing all initializer data
            save_model_with_external_weights(self.model.model, os.path.join(path, filename_prefix) + '.onnx')
        else:
            self.model.save_model_to_file(os.path.join(path, filename_prefix) + '.onnx')

    def set_and_freeze_param_encodings(self, encoding_path: str):
        """
        Set and freeze parameter encodings from encodings JSON file

        :param encoding_path: path from where to load parameter encodings file
        """

        # Load encodings file
        with open(encoding_path) as json_file:
            encodings = json.load(json_file)

        for quantizer_name in encodings:
            if quantizer_name in self.qc_quantize_op_dict:
                libpymo_encodings = _create_libpymo_encodings(encodings[quantizer_name])
                is_symmetric, is_strict_symmetric, is_unsigned_symmetric = \
                    get_symmetric_properties(encodings[quantizer_name])
                data_type = QuantizationDataType.int if encodings[quantizer_name][0]['dtype'] == 'int' else \
                    QuantizationDataType.float
                self.qc_quantize_op_dict[quantizer_name].update_quantizer_and_load_encodings(libpymo_encodings,
                                                                                             is_symmetric,
                                                                                             is_strict_symmetric,
                                                                                             is_unsigned_symmetric,
                                                                                             data_type)
                self.qc_quantize_op_dict[quantizer_name].freeze_encodings()

    def get_all_quantizers(self) -> Tuple[List, List]:
        """
        Returns all QcQuantizeOps through which TensorQuantizer's attributes can be accessed.
        """
        param_quantizers = []
        activation_quantizers = []

        for param in self.param_names:
            param_quantizers.append(self.qc_quantize_op_dict[param])

        for activation in self.activation_names:
            activation_quantizers.append(self.qc_quantize_op_dict[activation])

        return param_quantizers, activation_quantizers

    def _tie_quantizers(self):
        """
        Tie the input and output quantizers for given op types.
        """
        if not _tie_qtzrs:
            return

        cg = self.connected_graph

        def _set_quant_info(dst_qtzr_node_name: str, src_qtzr: QcQuantizeOp):
            """
            Set quant_info attribute (pointer to the libquant_info object)

            :param dst_qtzr_node_name: destination quantizer node name in graph.
            :param src_qtzr: source quantizer.
            """
            for node in self.model.graph().node:
                if node.op_type == 'QcQuantizeOp' and node.name == dst_qtzr_node_name:
                    for atr in node.attribute:
                        if atr.name == "quant_info":
                            atr.i = libpymo.PtrToInt64(src_qtzr.quant_info)
                            return

        def _set_qtzr(dst_qtzr: QcQuantizeOp, src_qtzr: QcQuantizeOp):
            """
            Set the dst quantizer by src quantizer and update quant_info attribute (pointer to the libquant_info object)
             in the graph node.

            :param dst_qtzr: destination quantizer.
            :param src_qtzr: source quantizer
            """
            for name, qtzr in self.qc_quantize_op_dict.items():
                if dst_qtzr == qtzr:
                    self.qc_quantize_op_dict[name] = src_qtzr
                    dst_qtzr_node_name = 'QcQuantizeOp_' + name
                    # update quant_info attribute (pointer to the libquant_info object) in the graph node.
                    _set_quant_info(dst_qtzr_node_name, src_qtzr)
                    return

        def _set_src_qtzr(x: Product, consumer: Op, src_qtzr):
            producer = x.producer

            if not producer:
                # ``x`` is a root input (i.e. has no producer).
                # In this case, set the input quantizer of the consumer to ``src_qtzr``
                i = consumer.inputs.index(x)
                inp_qtzr, _, __ = self.get_op_quantizers(consumer)
                if i >= len(inp_qtzr):
                    return

                _set_qtzr(dst_qtzr=inp_qtzr[i], src_qtzr=src_qtzr)
                return

            _, out_qtzr, __ = self.get_op_quantizers(producer)

            if out_qtzr:
                # There exists output quantizer associated with the graph node ``producer``
                # In this case, set the output quantizer of the producer to ``src_qtzr`
                outputs = producer.outputs
                i = outputs.index(x)
                _set_qtzr(dst_qtzr=out_qtzr[i], src_qtzr=src_qtzr)

            if not out_qtzr or producer.type in op_outputs_to_ignore:
                # 1. There is no output quantizer associated with the graph node ``producer``, or
                # 2. op is a math invariant op (reshape, permute, etc.).
                # In these cases, propagate encoding further to the ancestors
                for inp in producer.inputs:
                    _set_src_qtzr(inp, consumer=producer, src_qtzr=src_qtzr)

        for op in reversed(cg.ordered_ops):
            if op.type not in op_types_to_tie_qtzrs:
                continue

            _, out_qtzr, __ = self.get_op_quantizers(op)

            if not out_qtzr:
                continue

            if len(out_qtzr) != 1:
                msg = 'Encoding propagation is only supported for ops with exactly ' \
                      f'1 output quantizer, but found {len(out_qtzr)} ' \
                      'output quantizers'
                raise RuntimeError(msg)

            for inp in op.inputs:
                _set_src_qtzr(inp, consumer=op, src_qtzr=out_qtzr[0])


def load_encodings_to_sim(quant_sim_model: QuantizationSimModel, onnx_encoding_path: str, strict=True) -> \
        List[EncodingMismatchInfo]:
    """
    Loads the saved encodings to quant sim model. The encoding filename to load should end in .encodings,
    generated as part of quantsim export.

    :param quant_sim_model: Quantized model to load encodings for. Note: The model configuration should be the same as
        when encodings were exported.
    :param onnx_encoding_path: Path of the encodings file to load.
    :param strict: If set to True and encoding settings between encodings to load do not line up with Quantsim
        initialized settings, an assertion will be thrown. If set to False, quantizer settings will update to align with
        encodings to load.
    :return: List of EncodingMismatchInfo objects containing quantizer names and mismatched settings
    """
    mismatched_encodings = []

    # Load encodings file
    with open(onnx_encoding_path) as json_file:
        encodings = json.load(json_file)

    validate_encodings_to_load(encodings, quant_sim_model)

    # First pass through quantizers to check for mismatched encodings
    for quantizer_name, quantizer in quant_sim_model.qc_quantize_op_dict.items():
        if quantizer_name not in encodings['activation_encodings'] and \
                quantizer_name not in encodings['param_encodings']:
            mismatched_info = get_encoding_mismatch_info(quantizer_name, quantizer, None)
            if mismatched_info.has_mismatch():
                mismatched_encodings.append(mismatched_info)
            continue

        if quantizer_name in encodings['activation_encodings']:
            encodings_to_load = encodings['activation_encodings'][quantizer_name]
        else:
            encodings_to_load = encodings['param_encodings'][quantizer_name]

        mismatched_info = get_encoding_mismatch_info(quantizer_name, quantizer, encodings_to_load)
        if mismatched_info.has_mismatch():
            mismatched_encodings.append(mismatched_info)

    log_and_catch_mismatched_encodings(mismatched_encodings, strict)

    # Second pass through quantizers to set quantizer settings
    for quantizer_name, quantizer in quant_sim_model.qc_quantize_op_dict.items():
        if quantizer_name not in encodings['activation_encodings'] and \
                quantizer_name not in encodings['param_encodings']:
            quantizer.enabled = False
            continue

        if quantizer_name in encodings['activation_encodings']:
            encodings_to_load = encodings['activation_encodings'][quantizer_name]
        else:
            encodings_to_load = encodings['param_encodings'][quantizer_name]

        is_symmetric, is_strict_symmetric, is_unsigned_symmetric = \
            get_symmetric_properties(encodings_to_load)
        data_type = QuantizationDataType.int if encodings_to_load[0]['dtype'] == 'int' else \
                QuantizationDataType.float
        libpymo_encodings = _create_libpymo_encodings(encodings_to_load)
        quant_sim_model.qc_quantize_op_dict[quantizer_name].update_quantizer_and_load_encodings(
            libpymo_encodings, is_symmetric, is_strict_symmetric, is_unsigned_symmetric, data_type)

    return mismatched_encodings


def validate_encodings_to_load(encodings_to_load: Dict, quant_sim_model: QuantizationSimModel):
    """
    Validate that all names of encodings to load are found in the model.

    :param encodings_to_load: Encodings to load
    :param quant_sim_model: Quantsim model to check for encoding names.
    """
    # Check that all encoding names in the encodings to load are found in the model. This check only works for verifying
    # that names in encodings_to_load are valid. The reverse check will not work, since quantizers which are disabled
    # will not show up in encodings_to_load.
    encoding_names_not_found = []
    for quantizer_name in (list(encodings_to_load['activation_encodings'].keys()) +
                           list(encodings_to_load['param_encodings'].keys())):
        if quantizer_name not in quant_sim_model.qc_quantize_op_dict:
            encoding_names_not_found.append(quantizer_name)
    if encoding_names_not_found:
        logger.error('The following encoding names were present in the encodings to load but not found in the model: '
                     '%s', str(encoding_names_not_found))
        raise AssertionError('The following encoding names were present in the encodings to load but not found in the '
                             'model: ' + str(encoding_names_not_found))


def log_and_catch_mismatched_encodings(mismatched_encodings: List[EncodingMismatchInfo], strict: bool):
    """
    If mismatched_encodings is not empty, log details for each entry. If strict is True, raise an AssertionError.

    :param mismatched_encodings: List of mismatched quantizer names and encoding settings
    :param strict: If True, raise an AssertionError if there are mismatched settings
    """
    if mismatched_encodings:
        logging_strings = ['The following quantizers had settings not matching with provided encodings to load:']
        for mismatched_encoding_info in mismatched_encodings:
            logging_strings.append(mismatched_encoding_info.quantizer_name + ':')
            if mismatched_encoding_info.enabled_mismatch:
                logging_strings.append(f'\tenabled: {mismatched_encoding_info.enabled_mismatch[0]}, '
                                       f'loaded encoding enabled: '
                                       f'{mismatched_encoding_info.enabled_mismatch[1]}')

            if mismatched_encoding_info.dtype_mismatch:
                logging_strings.append(f'\tdtype: {mismatched_encoding_info.dtype_mismatch[0]}, '
                                       f'loaded encoding dtype: '
                                       f'{mismatched_encoding_info.dtype_mismatch[1]}')

            if mismatched_encoding_info.bitwidth_mismatch:
                logging_strings.append(f'\tbitwidth: '
                                       f'{mismatched_encoding_info.bitwidth_mismatch[0]}, loaded encoding bitwidth:'
                                       f'{mismatched_encoding_info.bitwidth_mismatch[1]}')

            if mismatched_encoding_info.is_symmetric_mismatch:
                logging_strings.append(f'\tsymmetric: '
                                       f'{mismatched_encoding_info.is_symmetric_mismatch[0]}, '
                                       f'loaded encoding symmetric: '
                                       f'{mismatched_encoding_info.is_symmetric_mismatch[1]}')

            if mismatched_encoding_info.is_strict_symmetric_mismatch:
                logging_strings.append(f'\tstrict symmetric: '
                                       f'{mismatched_encoding_info.is_strict_symmetric_mismatch[0]}, '
                                       f'loaded encoding strict symmetric: '
                                       f'{mismatched_encoding_info.is_strict_symmetric_mismatch[1]}')

            if mismatched_encoding_info.is_unsigned_symmetric_mismatch:
                logging_strings.append(f'\tunsigned symmetric: '
                                       f'{mismatched_encoding_info.is_unsigned_symmetric_mismatch[0]}, '
                                       f'loaded encoding unsigned symmetric: '
                                       f'{mismatched_encoding_info.is_unsigned_symmetric_mismatch[1]}')
        log_message = '\n'.join(logging_strings)
        if strict:
            logger.error(log_message)
            raise AssertionError(log_message)
        logger.info(log_message)


def _create_libpymo_encodings(encoding: Dict[str, Union[str, int, float]]) -> List[libpymo.TfEncoding]:
    """
    Given encoding dict, return a TfEncoding object with corresponding info.

    :param encoding: Encoding dict to create TfEncoding object with
    :return: TfEncoding object containing encoding dict info
    """
    libpymo_encodings = []
    for enc_val in encoding:
        enc = libpymo.TfEncoding()
        enc.bw = enc_val['bitwidth']
        enc.delta, enc.max, enc.min, enc.offset = 0.0, 0.0, 0.0, 0
        if enc_val['dtype'] == 'int':
            enc.delta, enc.max, enc.min, enc.offset = (enc_val['scale'], enc_val['max'], enc_val['min'],
                                                       enc_val['offset'])
        libpymo_encodings.append(enc)
    return libpymo_encodings


def get_symmetric_properties(encodings: List[Dict]) -> Tuple[Optional[bool], Optional[bool], Optional[bool]]:
    """
    Return symmetric properties of the given encodings. If encodings are float, return None for each.

    :param encodings: Encodings to get symmetric properties for
    :return: Tuple of is_symmetric, is_strict_symmetric, and is_unsigned symmetric properties
    """
    if encodings[0]['dtype'] == 'float':
        return None, None, None

    is_symmetric = encodings[0]['is_symmetric'] == 'True'

    is_strict_symmetric = False
    if is_symmetric and encodings[0]['offset'] == -2**(encodings[0]['bitwidth'] - 1) + 1:
        is_strict_symmetric = True

    # Note: Even if the original quantizer had is_unsigned_symmetric set to True, if any observed values were negative,
    # the resulting encodings will look signed. This logic can only perform a best effort check to return True only if
    # any encoding showed unsigned symmetric properties.
    is_unsigned_symmetric = False
    if is_symmetric:
        for encoding in encodings:
            if encoding['offset'] == 0:
                is_unsigned_symmetric = True
                break
    return is_symmetric, is_strict_symmetric, is_unsigned_symmetric

def get_encoding_mismatch_info(quantizer_name: str, quantizer: QcQuantizeOp,
                               encodings_to_load: Optional[List[Dict]]) -> EncodingMismatchInfo:
    """
    Check that quantizer settings align with the settings in encodings_to_load. If settings do not align, track the
    mismatching settings in a EncodingMismatchInfo object and add it to mismatched_encodings_info list.

    :param quantizer_name: Name of quantizer to check
    :param quantizer: Quantizer to check
    :param encodings_to_load: Encodings to check
    """
    encoding_mismatch_info = EncodingMismatchInfo(quantizer_name)

    # Match enabled state
    if quantizer.enabled and encodings_to_load is None:
        encoding_mismatch_info.enabled_mismatch = (quantizer.enabled, False)
    if not quantizer.enabled and encodings_to_load is not None:
        encoding_mismatch_info.enabled_mismatch = (quantizer.enabled, True)

    if encodings_to_load is not None:
        is_symmetric, is_strict_symmetric, is_unsigned_symmetric = get_symmetric_properties(encodings_to_load)

        if quantizer.bitwidth != encodings_to_load[0]['bitwidth']:
            encoding_mismatch_info.bitwidth_mismatch = (quantizer.bitwidth, encodings_to_load[0]['bitwidth'])
        if quantizer.data_type.name != encodings_to_load[0]['dtype']:
            encoding_mismatch_info.dtype_mismatch = (quantizer.data_type.name, encodings_to_load[0]['dtype'])
        if quantizer.use_symmetric_encodings != is_symmetric:
            encoding_mismatch_info.is_symmetric_mismatch = (quantizer.use_symmetric_encodings, is_symmetric)
        if quantizer.use_strict_symmetric != is_strict_symmetric:
            encoding_mismatch_info.is_strict_symmetric_mismatch = (quantizer.use_strict_symmetric, is_strict_symmetric)

        # Unsigned symmetric is a special case because even if the setting is true, the encodings may appear to be
        # signed symmetric if any observed tensor values were < 0.
        # In this case, only mark a mismatch if quantizer was set to signed symmetric but an unsigned symmetric
        # encoding was seen.
        if quantizer.use_unsigned_symmetric != is_unsigned_symmetric and not quantizer.use_unsigned_symmetric:
            encoding_mismatch_info.is_unsigned_symmetric_mismatch = (quantizer.use_unsigned_symmetric,
                                                                     is_unsigned_symmetric)

    return encoding_mismatch_info


def set_blockwise_quantization_for_weights(sim: QuantizationSimModel,
                                           op_types: Union[str, Tuple],
                                           bitwidth: int,
                                           symmetric: bool,
                                           block_size: int,
                                           strict: bool = False):
    """
    Set weight quantizers for the given operator types to use blockwise affine quantization.

    :param sim: Quantsim object to configure weight quantizers for
    :param op_types: Operator types for which to enable blockwise weight quantizaiton
    :param bitwidth: Bitwidth for quantization
    :param symmetric: True if quantization is symmetric, False otherwise
    :param block_size: Block size for affine quantization. The block size will be applied to the weight's input features
        dimension, while per-channel will be used for the weight's output features dimension
    :param strict: If False, only enable blockwise quant for layers with dimensions evenly divisible by block_size.
        If True, throw an error for layers with incompatible shapes.

    Examples:

        >>> # Assume 'sim' is a QuantizationSimModel object
        >>> # Allows setting of all Linear and Conv weight quantizers to block_size 64 in the input_channels dimension:
        >>> set_blockwise_quantization_for_weights(sim=sim,
        ...                                        op_types=("Gemm", "MatMul", "Conv"),
        ...                                        bitwidth=4,
        ...                                        symmetric=True,
        ...                                        block_size=64)
    """

    if isinstance(op_types, str):
        op_types = (op_types, )

    for op in sim.connected_graph.ordered_ops:
        if op.type in op_types:
            _, _, param_quantizers = sim.get_op_quantizers(op)

            if "weight" in param_quantizers:
                weight_quantizer: QcQuantizeOp = param_quantizers["weight"]

                try:
                    weight_quantizer._enable_blockwise_quantization(block_size) # pylint:disable = protected-access
                except ValueError as e:
                    if strict:
                        raise e
                else:
                    weight_quantizer.set_bitwidth(bitwidth)
                    weight_quantizer.use_symmetric_encodings = symmetric
                    weight_quantizer.data_type = QuantizationDataType.int


def set_grouped_blockwise_quantization_for_weights(sim: QuantizationSimModel,
                                                   op_types: Union[str, Tuple],
                                                   bitwidth: int,
                                                   decompressed_bw: int,
                                                   block_size: int,
                                                   strict: bool = False):
    """
    Set weight parameter quantizers of modules to grouped blockwise quantization.

    :param sim: Quantsim to set weight quantizers for
    :param op_types: Operator types for which to enable grouped blockwise weight quantizaiton
    :param bitwidth: Bitwidth for affine quantization
    :param decompressed_bw: Decompressed bw for grouped block quantization
    :param block_size: Block size for affine quantization. The block size will be applied to the weight's input features
        dimension, while per-channel will be used for the weight's output features dimension

    Examples:

        >>> # Assume 'sim' is a QuantizationSimModel object
        >>> # Sets of all Gemm, MatMul, and Conv weight quantizers to block_size 64 in the input_channels dimension:
        >>> set_grouped_blockwise_quantization_for_weights(sim=sim,
        ...                                                op_types=("Gemm", "MatMul", "Conv"),
        ...                                                bitwidth=4,
        ...                                                decompressed_bw=8,
        ...                                                block_size=64)
    """

    if isinstance(op_types, str):
        op_types = (op_types, )

    for op in sim.connected_graph.ordered_ops:

        if op.type in op_types:
            _, _, param_quantizers = sim.get_op_quantizers(op)


            if "weight" in param_quantizers:
                weight_quantizer: QcQuantizeOp = param_quantizers["weight"]

                try:
                    grouped_quantizer = GroupedBlockQuantizeDequantize(weight_quantizer.quant_info,
                                                                       bitwidth,
                                                                       decompressed_bw,
                                                                       block_size,
                                                                       weight_quantizer.quant_scheme,
                                                                       weight_quantizer.op_mode,
                                                                       weight_quantizer.tensor_quantizer_params)
                except ValueError as e:
                    if strict:
                        raise e
                else:
                    for name, quantizer in sim.qc_quantize_op_dict.items():
                        if quantizer is weight_quantizer:
                            sim.qc_quantize_op_dict[name] = grouped_quantizer


# pylint: disable=protected-access
def clamp_activation_encodings(quant_sim: QuantizationSimModel, clamp_val: float):
    """
    Clamp activations to specific range if out of bound.

    :param quant_sim: quantsim object
    :param clamp_val: positive float value
    :return:
    """
    for act_name in quant_sim.activation_names:
        quantizer = quant_sim.qc_quantize_op_dict.get(act_name)
        is_clipped = quantizer.clip_and_recompute_encodings(clamp_val)
        if is_clipped:
            logger.info("Clamped tensor %s", act_name)
