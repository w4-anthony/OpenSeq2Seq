# Copyright (c) 2018 NVIDIA Corporation
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals
from six.moves import range

import tensorflow as tf
from .automatic_loss_scaler import AutomaticLossScaler


class MixedPrecisionOptimizerWrapper(tf.train.Optimizer):
  def __init__(self, optimizer, automatic_loss_scaler=None):
    super(MixedPrecisionOptimizerWrapper, self).__init__(
      optimizer._use_locking,
      optimizer._name + '-MP',
    )
    self._optimizer = optimizer
    self._fp32_to_fp16 = {}
    self._loss_scaler = automatic_loss_scaler

  def compute_gradients(self, loss, var_list=None,
                        gate_gradients=tf.train.Optimizer.GATE_OP,
                        aggregation_method=None,
                        colocate_gradients_with_ops=False,
                        grad_loss=None):
    if self._loss_scaler:
      loss *= self._loss_scaler.loss_scale

    grads_and_vars_fp16 = self._optimizer.compute_gradients(
      loss, var_list=var_list,
      gate_gradients=gate_gradients,
      aggregation_method=aggregation_method,
      colocate_gradients_with_ops=colocate_gradients_with_ops,
      grad_loss=grad_loss,
    )

    # collecting regularization functions
    reg_var_funcs = tf.get_collection('REGULARIZATION_FUNCTIONS')
    reg_funcs = dict(map(lambda x: (x[0].name, x[1]), reg_var_funcs))

    # creating FP-32 variables and filling the fp32 dict
    grads_and_vars_fp32 = []
    with tf.variable_scope('FP32-master-copy'):
      for grad, var in grads_and_vars_fp16:
        if var.dtype.base_dtype == tf.float16:
          fp32_var = tf.Variable(
            initial_value=tf.cast(var.initialized_value(), tf.float32),
            name=var.name.split(':')[0],
            expected_shape=var.shape,
            dtype=tf.float32,
            trainable=False,
            # necessary for cudnn_rnn layers which have unknown shape
            validate_shape=bool(var.get_shape()),
            collections=[tf.GraphKeys.GLOBAL_VARIABLES,
                         "FP32_MASTER_COPIES"],
          )
          self._fp32_to_fp16[fp32_var.name] = var
          fp32_grad = tf.cast(grad, tf.float32)
          # adding regularization part with respect to fp32 copy
          if var.name in reg_funcs:
            fp32_grad += tf.gradients(
              tf.contrib.layers.apply_regularization(
                reg_funcs[var.name],
                [fp32_var],
              ),
              fp32_var,
            )[0]
          grads_and_vars_fp32.append((fp32_grad, fp32_var))
        else:
          grads_and_vars_fp32.append((grad, var))

    # Unscale gradients if necessary
    if self._loss_scaler:
      grads_and_vars_fp32 = _scale_grads(grads_and_vars_fp32,
                                         1. / self._loss_scaler.loss_scale)

    return grads_and_vars_fp32

  def apply_gradients(self, grads_and_vars, global_step=None, name=None):

    def apply_ops_wrapper():
      update_op = self._optimizer.apply_gradients(grads_and_vars,
                                                  global_step, name)
      apply_ops = []
      with tf.control_dependencies([update_op]):
        for grad, var in grads_and_vars:
          if var.name in self._fp32_to_fp16:
            dst_var = self._fp32_to_fp16[var.name]
            apply_ops.append(
              tf.assign(dst_var, tf.cast(var, tf.float16)))
      if apply_ops:
        return tf.group(apply_ops)
      return update_op

    if self._loss_scaler:
      grad_has_nans, grad_amax = AutomaticLossScaler.check_grads(grads_and_vars)
      should_skip_update = tf.logical_or(tf.is_inf(grad_amax), grad_has_nans)
      loss_scale_update_op = self._loss_scaler.update_op(grad_has_nans,
                                                         grad_amax)
      with tf.control_dependencies([loss_scale_update_op]):
        return tf.cond(should_skip_update,
                       tf.no_op,
                       apply_ops_wrapper)
    else:
      return apply_ops_wrapper()
  #
  # def _apply_dense(self, *args, **kwargs):
  #   """Calls this same method on the underlying optimizer."""
  #   return self._optimizer._apply_dense(*args, **kwargs)
  #
  # def _resource_apply_dense(self, *args, **kwargs):
  #   """Calls this same method on the underlying optimizer."""
  #   return self._optimizer._resource_apply_dense(*args, **kwargs)
  #
  # def _resource_apply_sparse_duplicate_indices(self, *args, **kwargs):
  #   """Calls this same method on the underlying optimizer."""
  #   return self._optimizer._resource_apply_sparse_duplicate_indices(*args,
  #                                                                   **kwargs)
  #
  # def _resource_apply_sparse(self, *args, **kwargs):
  #   """Calls this same method on the underlying optimizer."""
  #   return self._optimizer._resource_apply_sparse(*args, **kwargs)
  #
  # def _apply_sparse_duplicate_indices(self, *args, **kwargs):
  #   """Calls this same method on the underlying optimizer."""
  #   return self._optimizer._apply_sparse_duplicate_indices(*args, **kwargs)
  #
  # def _apply_sparse(self, *args, **kwargs):
  #   """Calls this same method on the underlying optimizer."""
  #   return self._optimizer._apply_sparse(*args, **kwargs)
  #
  # def _prepare(self, *args, **kwargs):
  #   """Calls this same method on the underlying optimizer."""
  #   return self._optimizer._prepare(*args, **kwargs)
  #
  # def _create_slots(self, *args, **kwargs):
  #   """Calls this same method on the underlying optimizer."""
  #   return self._optimizer._create_slots(*args, **kwargs)
  #
  # def _valid_dtypes(self, *args, **kwargs):
  #   """Calls this same method on the underlying optimizer."""
  #   return self._optimizer._valid_dtypes(*args, **kwargs)
  #
  # def _finish(self, *args, **kwargs):
  #   """Calls this same method on the underlying optimizer."""
  #   return self._optimizer._finish(*args, **kwargs)


def mp_regularizer_wrapper(regularizer):
  def func_wrapper(weights):
    if weights.dtype.base_dtype == tf.float16:
      tf.add_to_collection('REGULARIZATION_FUNCTIONS', (weights, regularizer))
      # disabling the inner regularizer
      return None
    else:
      return regularizer(weights)

  return func_wrapper


def _scale_grads(grads_and_vars, scale):
  scaled_grads_and_vars = []
  for grad, var in grads_and_vars:
    if grad is not None:
      if isinstance(grad, tf.IndexedSlices):
        grad_values = grad.values * scale
        grad = tf.IndexedSlices(grad_values, grad.indices, grad.dense_shape)
      else:
        grad *= scale
    scaled_grads_and_vars.append((grad, var))
  return scaled_grads_and_vars
