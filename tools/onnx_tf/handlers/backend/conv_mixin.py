import tensorflow as tf

from onnx_tf.common import get_data_format
from onnx_tf.common import get_perm_from_formats
from onnx_tf.common import supports_device
from onnx_tf.common import exception
from .broadcast_mixin import BroadcastMixin
from .pad_mixin import PadMixin

# Constant string used to indicate that requested padding
# is not natively supported in Tensorflow.
PAD_TF_INCOMPATIBLE = "PAD_TF_INCOMPATIBLE"


class ConvMixin(BroadcastMixin):

  @classmethod
  def conv(cls, node, input_dict, transpose=False):
    """ Convolution method for both conv and transposed conv
    For transposed conv,
      Attr pads is not used for input, but declares how much output is padded.
      Here, output means output from transposed conv which already pad output_padding if set.
      So the pseudo explanation for output should be:
        output = conv_transpose_output + output_padding - pads
      And conv_transpose_output shape should be:
        conv_transpose_output_shape[i] = strides[i] * (input_shape[i] - 1) + kernel_shape[i]
    """
    x = input_dict[node.inputs[0]]
    x_rank = len(x.get_shape())
    x_shape = x.get_shape().as_list()
    spatial_size = x_rank - 2

    support_cuda = supports_device("CUDA")
    storage_format, compute_format = get_data_format(x_rank)
    compute_c_idx = compute_format.find("C")
    spatial_format = "".join([d for d in compute_format if d not in ["N", "C"]])

    in_weights = input_dict[node.inputs[1]]
    weights_rank = len(in_weights.get_shape())
    if transpose:
      # Translate weights from (C x M x KH x KW) to (KH x KW X M X C)
      perm = list(range(2, weights_rank)) + [1, 0]
    else:
      # Translate weights from (M x C x KH x KW) to (KH x KW X C X M)
      perm = list(range(2, weights_rank)) + [1, 0]

    if "kernel_shape" in node.attrs.keys():
      kernel_shape = node.attrs["kernel_shape"]
      assert in_weights.get_shape().as_list()[2:] == kernel_shape, (
          "kernel_shape "
          "attr of convolution does not match the actual weight "
          "passed to this operation, attr {}, actual {}").format(
              kernel_shape,
              in_weights.get_shape().as_list())
    else:
      kernel_shape = in_weights.get_shape().as_list()[2:]

    weights = tf.transpose(in_weights, perm)
    dilations = node.attrs.get("dilations", [1] * spatial_size)
    strides = node.attrs.get("strides", [1] * spatial_size)

    pads = node.attrs.get("pads", [0, 0] * spatial_size)

    # Check auto_pad nonexistent or NOTSET first
    if "auto_pad" not in node.attrs or node.attrs["auto_pad"] == "NOTSET":
      if not transpose:
        if pads != [0, 0] * spatial_size:
          x = PadMixin.get_padding_as_op(x, pads)
        pad_mode = "VALID"
      else:
        pad_mode = "NOTSET"
    # Then we use auto_pad to setup pad_mode
    elif node.attrs["auto_pad"] == "SAME_UPPER":
      pad_mode = "SAME"
    elif node.attrs["auto_pad"] == "VALID":
      pad_mode = "VALID"
    elif node.attrs["auto_pad"] == "SAME_LOWER":
      if transpose:
        pad_mode = PAD_TF_INCOMPATIBLE
      else:
        pad_mode = "SAME"
    else:
      raise ValueError("Invalid auto_pad attribute: {}".format(
          node.attrs["auto_pad"]))

    # Currently auto_pad = SAME_LOWER is not supported
    if pad_mode is PAD_TF_INCOMPATIBLE:
      if transpose:
        exception.OP_UNSUPPORTED_EXCEPT("ConvTranspose with auto_pad `SAME_LOWER`", "Tensorflow")
      else:
        exception.OP_UNSUPPORTED_EXCEPT("Conv with auto_pad `SAME_LOWER`", "Tensorflow")

    group = node.attrs.get("group", 1)

    if (group == 1 ) and not transpose:
        if ("auto_pad" in node.attrs) and (node.attrs["auto_pad"] == "SAME_LOWER") and (strides != [1,1]) :
          pad = [1,1]
          if support_cuda:
            x = tf.pad(x, [[0,0], [0,0], pad, pad])
          else:
            x = tf.pad(x, [[0,0], pad, pad, [0,0]])
            x = tf.transpose( x, perm=get_perm_from_formats(storage_format, compute_format))
          pad_mode = "VALID"

        #tf.conv2d(input, filter, strides, padding, use_cudnn_on_gpu=True, data_format='NHWC', name=None)
        #tf.convolution(input, filter, padding, strides=None, dilation_rate=None, name=None, data_format=None)
        output = tf.nn.convolution( x, weights, pad_mode,
                            strides=strides, dilation_rate=dilations, data_format=compute_format)

        if len(node.inputs) == 2:
          if not support_cuda:
            output = tf.transpose( output, perm=get_perm_from_formats(compute_format, storage_format))
        else:
          bias = input_dict[node.inputs[2]]
          bias = cls.explicit_broadcast([x, bias], compute_c_idx)

          if support_cuda:
            output = tf.add(output, bias)
          else:
            output = tf.add(output, bias)
            output = tf.transpose(
                output, perm=get_perm_from_formats(compute_format, storage_format))

        return [output]

    weight_groups = tf.split(weights, num_or_size_splits=group, axis=-1)

    if support_cuda:
      xs = tf.split(x, num_or_size_splits=group, axis=1)
    else:
      x = tf.transpose(
          x, perm=get_perm_from_formats(storage_format, compute_format))
      xs = tf.split(x, num_or_size_splits=group, axis=-1)

    convolved = []
    if transpose:
      if dilations != [1] * spatial_size:
        raise RuntimeError("Cannot set non-1 dilation for conv transpose.")
      convolved = []
      for (x, weight) in zip(xs, weight_groups):
        x_spatial_shape = [
            x_shape[storage_format.find(d)] for d in spatial_format
        ]
        weights_shape = weights.get_shape().as_list()
        output_shape = node.attrs.get("output_shape", None)
        conv_output_shape = [x_shape[storage_format.find("N")]]

        # calculate output shape
        if pad_mode == "NOTSET":
          if output_shape is None:
            conv_output_shape += [
                strides[i] * x_spatial_shape[i] - strides[i] +
                (kernel_shape[i] - 1) * dilations[i] + 1
                for i in list(range(spatial_size))
            ]
          else:
            conv_output_shape += [
                s + pads[i] + pads[spatial_size + i]
                for i, s in enumerate(output_shape[-2:])
            ]
          conv_output_shape.insert(compute_c_idx, weights_shape[-2])

          def handle_dynamic_batch_size(output_shape, batch_idx):
            output_shape[batch_idx] = tf.shape(x)[batch_idx]
            return tf.stack(output_shape)

          # process dynamic batch size
          if conv_output_shape[storage_format.find("N")] is None:
            batch_idx = storage_format.find("N")
            conv_output_shape = handle_dynamic_batch_size(conv_output_shape,
                    batch_idx)

          # make strides to match input rank
          strides_full = [1] + strides
          strides_full.insert(compute_c_idx, 1)

          # get corresponding function in tf
          if spatial_size == 1:
            conv_func = tf.contrib.nn.conv1d_transpose
            strides_full = strides[0]
          elif spatial_size == 2:
            conv_func = tf.nn.conv2d_transpose
          elif spatial_size == 3:
            conv_func = tf.nn.conv3d_transpose
          else:
            raise NotImplementedError(
                "Transposed convolution for {}d is not implemented in Tensorflow".
                format(spatial_size))

          # use raw input x to do transposed conv
          conv_rs = conv_func(
              x,
              weight,
              conv_output_shape,
              strides_full,
              padding="VALID",
              data_format=compute_format)

          # pad output first by output_padding attr
          if "output_padding" in node.attrs and output_shape is None:
            output_padding = [[0, 0]] + [[0, p] for p in node.attrs["output_padding"]]
            output_padding.insert(compute_c_idx, [0, 0])
            conv_rs = tf.pad(conv_rs, output_padding)

          # remove pads set in pads attr
          conv_rs_shape = conv_rs.get_shape().as_list()
          begin = [0] + pads[:spatial_size]
          begin.insert(compute_c_idx, 0)
          size = [
              s if d in ["N", "C"] else s - pads[spatial_format.find(d)] -
              pads[spatial_format.find(d) + spatial_size]
              for d, s in zip(compute_format, conv_rs_shape)
          ]

          # process dynamic batch size
          if size[compute_format.find("N")] is None:
            batch_idx = compute_format.find("N")
            size = handle_dynamic_batch_size(size, batch_idx)

          conv_rs = tf.slice(conv_rs, begin=begin, size=size)

          convolved.append(conv_rs)
        else:
          # No need to check pads if auto_pad is specifically provided.
          # The assumption is that once auto_pad is provided as either VALID
          # or SAME_UPPER (SAME_LOWER is currently not supported in TF) the
          # output_shape will always be inferred. That is, the output_shape
          # and output_padding will not be used in this case.
          if pad_mode == "VALID":
            conv_output_shape += [
                strides[i] * (x_spatial_shape[i] - 1) + weights_shape[i]
                for i in list(range(spatial_size))
            ]
          else:
            conv_output_shape += [
                strides[i] * x_spatial_shape[i]
                for i in list(range(spatial_size))
            ]
          conv_output_shape.insert(compute_c_idx, weights_shape[-2])

          # process dynamic batch size
          if conv_output_shape[storage_format.find("N")] is None:
            batch_idx = storage_format.find("N")
            conv_output_shape = handle_dynamic_batch_size(conv_output_shape,
                    batch_idx)

          # make strides to match input rank
          strides_full = [1] + strides
          strides_full.insert(compute_c_idx, 1)

          # get corresponding function in tf
          if spatial_size == 1:
            conv_func = tf.contrib.nn.conv1d_transpose
            strides_full = strides[0]
          elif spatial_size == 2:
            conv_func = tf.nn.conv2d_transpose
          elif spatial_size == 3:
            conv_func = tf.nn.conv3d_transpose
          else:
            raise NotImplementedError(
                "Transposed convolution for {}d is not implemented in Tensorflow".
                format(spatial_size))

          # use raw input x to do transposed conv
          conv_rs = conv_func(
              x,
              weight,
              conv_output_shape,
              strides_full,
              padding=pad_mode,
              data_format=compute_format)
          convolved.append(conv_rs)

    else:
      # FIXME: 修改支持 "SAME_LOWER" 模式
      #self.prev_layer = keras.layers.ZeroPadding2D(((1,0),(1,0)))(self.prev_layer)
      if True:
        if ("auto_pad" in node.attrs) and (node.attrs["auto_pad"] == "SAME_LOWER") and (strides != [1,1]) :
          pad = [1,1]
          if support_cuda:
            xs = [tf.pad(x, [[0,0], [0,0], pad, pad]) for x in xs]
          else:
            xs = [tf.pad(x, [[0,0], pad, pad, [0,0]]) for x in xs]
          pad_mode = "VALID"

      convolved = [
          tf.nn.convolution(
              x,
              weight,
              pad_mode,
              strides=strides,
              dilation_rate=dilations,
              data_format=compute_format)
          for (x, weight) in zip(xs, weight_groups)
      ]

    if len(node.inputs) == 2:
      if support_cuda:
        output = tf.concat(convolved, axis=1)
      else:
        output = tf.concat(convolved, axis=-1)
        output = tf.transpose(
            output, perm=get_perm_from_formats(compute_format, storage_format))
    else:
      bias = input_dict[node.inputs[2]]
      bias = cls.explicit_broadcast([x, bias], compute_c_idx)

      if support_cuda:
        output = tf.concat(convolved, axis=1)
        output = tf.add(output, bias)
      else:
        output = tf.concat(convolved, axis=-1)
        output = tf.add(output, bias)
        output = tf.transpose(
            output, perm=get_perm_from_formats(compute_format, storage_format))

    return [output]
