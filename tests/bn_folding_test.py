# Copyright 2019 Google LLC
#
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Tests layers from folded_layers.py."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
import numpy as np
from numpy.testing import assert_allclose
from numpy.testing import assert_raises
import tempfile

import tensorflow as tf
from tensorflow.keras import layers
from tensorflow.keras.models import Model
from tensorflow.keras.backend import clear_session
from tensorflow.keras.utils import to_categorical
from tensorflow.keras import metrics

from qkeras import QConv2DBatchnorm
from qkeras import QConv2D
from qkeras import utils as qkeras_utils


def get_qconv2d_model(input_shape, kernel_size, kernel_quantizer=None):
  num_class = 2

  x = x_in = layers.Input(input_shape, name="input")

  x = QConv2D(
      filters=2, kernel_size=kernel_size, strides=(4, 4),
      kernel_initializer="ones",
      bias_initializer="zeros", use_bias=False,
      kernel_quantizer=kernel_quantizer, bias_quantizer=None,
      name="conv2d")(x)

  x = layers.BatchNormalization(
      axis=-1,
      momentum=0.99,
      epsilon=0.001,
      center=True,
      scale=True,
      beta_initializer="zeros",
      gamma_initializer="ones",
      moving_mean_initializer="zeros",
      moving_variance_initializer="ones",
      beta_regularizer=None,
      gamma_regularizer=None,
      beta_constraint=None,
      gamma_constraint=None,
      renorm=False,
      renorm_clipping=None,
      renorm_momentum=0.99,
      fused=None,
      trainable=True,
      virtual_batch_size=None,
      adjustment=None,
      name="bn")(
          x)
  x = layers.Flatten(name="flatten")(x)
  x = layers.Dense(num_class, use_bias=False, kernel_initializer="ones",
                   name="dense")(x)
  x = layers.Activation("softmax", name="softmax")(x)
  model = Model(inputs=[x_in], outputs=[x])
  return model


def get_qconv2d_batchnorm_model(input_shape, kernel_size, folding_mode,
                                kernel_quantizer=None):
  num_class = 2

  x = x_in = layers.Input(input_shape, name="input")
  x = QConv2DBatchnorm(
      filters=2, kernel_size=kernel_size, strides=(4, 4),
      kernel_initializer="ones", bias_initializer="zeros", use_bias=False,
      kernel_quantizer=kernel_quantizer, beta_initializer="zeros",
      gamma_initializer="ones", moving_mean_initializer="zeros",
      moving_variance_initializer="ones", folding_mode=folding_mode,
      name="foldconv2d")(x)

  x = layers.Flatten(name="flatten")(x)
  x = layers.Dense(num_class, use_bias=False, kernel_initializer="ones",
                   name="dense")(x)
  x = layers.Activation("softmax", name="softmax")(x)
  model = Model(inputs=[x_in], outputs=[x])
  return model


def get_models_with_one_layer(kernel_quantizer, folding_mode, ema_freeze_delay):

  x_shape = (2, 2, 1)
  loss_fn = tf.keras.losses.MeanSquaredError()
  optimizer = tf.keras.optimizers.SGD(learning_rate=1e-3)

  # define a model with seperate conv2d and bn layers
  x = x_in = layers.Input(x_shape, name="input")
  x = QConv2D(
      filters=2, kernel_size=(2, 2), strides=(4, 4),
      kernel_initializer="ones",
      bias_initializer="zeros", use_bias=False,
      kernel_quantizer=kernel_quantizer, bias_quantizer=None,
      name="conv2d")(x)
  x = layers.BatchNormalization(
      axis=-1,
      momentum=0.99,
      epsilon=0.001,
      center=True,
      scale=True,
      beta_initializer="zeros",
      gamma_initializer="ones",
      moving_mean_initializer="zeros",
      moving_variance_initializer="ones",
      beta_regularizer=None,
      gamma_regularizer=None,
      beta_constraint=None,
      gamma_constraint=None,
      renorm=False,
      renorm_clipping=None,
      renorm_momentum=0.99,
      fused=None,
      trainable=True,
      virtual_batch_size=None,
      adjustment=None,
      name="bn")(x)
  unfold_model = Model(inputs=[x_in], outputs=[x])
  unfold_model.compile(loss=loss_fn, optimizer=optimizer, metrics="acc")

  x = x_in = layers.Input(x_shape, name="input")
  x = QConv2DBatchnorm(
      filters=2, kernel_size=(2, 2), strides=(4, 4),
      kernel_initializer="ones", bias_initializer="zeros", use_bias=False,
      kernel_quantizer=kernel_quantizer, beta_initializer="zeros",
      gamma_initializer="ones", moving_mean_initializer="zeros",
      moving_variance_initializer="ones", folding_mode=folding_mode,
      ema_freeze_delay=ema_freeze_delay,
      name="foldconv2d")(x)
  fold_model = Model(inputs=[x_in], outputs=[x])
  fold_model.compile(loss=loss_fn, optimizer=optimizer, metrics="acc")

  return (unfold_model, fold_model)


def get_debug_model(model):
  layer_output_list = []
  for layer in model.layers:
    if layer.__class__.__name__ not in ["Flatten", "InputLayer"]:
      layer_output_list.append(layer.output)

  debug_model = Model(inputs=model.inputs, outputs=layer_output_list)
  return debug_model


def generate_dataset(train_size=10,
                     batch_size=5,
                     input_shape=(3, 3, 1),
                     num_class=2):
  """create tf.data.Dataset with shape: (N,) + input_shape."""

  x_train = np.random.randint(
      4, size=(train_size, input_shape[0], input_shape[1], input_shape[2]))
  x_train = np.random.rand(
      train_size, input_shape[0], input_shape[1], input_shape[2])

  y_train = np.random.randint(num_class, size=train_size)
  y_train = to_categorical(y_train, num_class)

  train_ds = tf.data.Dataset.from_tensor_slices((x_train, y_train))
  train_ds = train_ds.batch(batch_size)
  return train_ds


def run_training(model, epochs, loss_fn, loss_metric, optimizer,
                 train_ds, do_print=False):

  # Iterate over epochs.
  for epoch in range(epochs):
    if do_print:
      print("- epoch {} -".format(epoch))

    # Iterate over the batches of the dataset.
    for step, (x_batch_train, y_batch_train) in enumerate(train_ds):
      if do_print:
        print("\n   - step {} -".format(step))
      with tf.GradientTape() as tape:
        predictions = model(x_batch_train, training=True)

        if epoch == epochs - 1:
          if do_print:
            print("y_pred:", predictions)
            print("y:", y_batch_train)
          output_predictions = predictions

        # Compute loss
        loss = loss_fn(y_batch_train, predictions)

        grads = tape.gradient(loss, model.trainable_weights)
        if do_print:
          if epoch == epochs - 1:
            # print("old trainable:", model.trainable_weights)
            print("grads:", grads)
        optimizer.apply_gradients(zip(grads, model.trainable_weights))

        if do_print:
          if epoch == epochs - 1:
            # print("new trainable:", model.trainable_weights)
            print("loss:", loss)
        loss_metric(loss)
        if do_print:
          if epoch == epochs - 1:
            print("mean loss = %.4f" % (loss_metric.result()))

  return output_predictions


def test_loading():
  """Test to load model using different approahches."""

  loss_fn = tf.keras.losses.MeanSquaredError()
  loss_metric = metrics.Mean()
  optimizer = tf.keras.optimizers.SGD(learning_rate=1e-3)
  x_shape = (2, 2, 1)

  custom_objects = {}
  qkeras_utils._add_supported_quantized_objects(custom_objects)

  train_ds = generate_dataset(train_size=1, batch_size=1,
                              input_shape=x_shape, num_class=2)

  model_fold = get_qconv2d_batchnorm_model(
      input_shape=x_shape, kernel_size=(2, 2),
      folding_mode="ema_stats_folding")
  model_fold.compile(loss=loss_fn, optimizer=optimizer, metrics="acc")

  run_training(model_fold, 10, loss_fn, loss_metric, optimizer, train_ds,
               do_print=False)

  # test load model from json to ensure saving/loading model architecture works
  json_string = model_fold.to_json()
  clear_session()
  model_from_json = qkeras_utils.quantized_model_from_json(json_string)
  assert json_string == model_from_json.to_json()

  # test reload model from hdf5 files to ensure saving/loading works
  _, fname = tempfile.mkstemp(".h5")
  model_fold.save(fname)
  model_loaded = qkeras_utils.load_qmodel(fname)
  weight1 = model_fold.layers[1].get_folded_quantized_weight()
  weight2 = model_loaded.layers[1].get_folded_quantized_weight()
  assert_allclose(weight1[0], weight2[0], rtol=1e-4)
  assert_allclose(weight1[1], weight2[1], rtol=1e-4)

  # test convert a folded model to a normal model for zpm
  # the kernel/bias weight in the normal model should be the same as the folded
  # kernel/bias in the folded model
  normal_model = qkeras_utils.convert_folded_model_to_normal(model_fold)
  weight2 = normal_model.layers[1].get_weights()
  assert_allclose(weight1[0], weight2[0], rtol=1e-4)
  assert_allclose(weight1[1], weight2[1], rtol=1e-4)


def test_same_training_and_prediction():
  """test if fold/unfold layer has the same training and prediction output."""

  epochs = 5
  loss_fn = tf.keras.losses.MeanSquaredError()
  loss_metric = metrics.Mean()
  optimizer = tf.keras.optimizers.SGD(learning_rate=1e-3)

  x_shape = (2, 2, 1)
  kernel = np.array([[[[1., 1.]], [[1., 0.]]], [[[1., 1.]], [[0., 1.]]]])
  gamma = np.array([2., 1.])
  beta = np.array([0., 1.])
  moving_mean = np.array([1., 1.])
  moving_variance = np.array([1., 2.])
  iteration = np.array(-1)
  folded_kernel_quantized = np.array([[[[1.99900079, 0.706930101]],
                                       [[1.99900079, 0]]],
                                      [[[1.99900079, 0.706930101]],
                                       [[0, 0.706930101]]]])
  folded_bias_quantized = np.array([-1.99900079, 0.293069899])
  train_ds = generate_dataset(train_size=10, batch_size=10, input_shape=x_shape,
                              num_class=2)

  (unfold_model, fold_model_batch) = get_models_with_one_layer(
      kernel_quantizer=None, folding_mode="batch_stats_folding",
      ema_freeze_delay=10)
  (_, fold_model_ema) = get_models_with_one_layer(
      kernel_quantizer=None, folding_mode="ema_stats_folding",
      ema_freeze_delay=10)

  unfold_model.layers[1].set_weights([kernel])
  unfold_model.layers[2].set_weights(
      [gamma, beta, moving_mean, moving_variance])
  fold_model_batch.layers[1].set_weights([
      kernel, gamma, beta, folded_kernel_quantized, folded_bias_quantized,
      iteration, moving_mean, moving_variance
  ])
  fold_model_ema.layers[1].set_weights([
      kernel, gamma, beta, folded_kernel_quantized, folded_bias_quantized,
      iteration, moving_mean, moving_variance
  ])

  # check if prediction is the same
  y1 = unfold_model.predict(train_ds)
  y2_batch = fold_model_batch.predict(train_ds)
  y2_ema = fold_model_ema.predict(train_ds)
  assert_allclose(y1, y2_batch, rtol=1e-4)
  assert_allclose(y1, y2_ema, rtol=1e-4)

  # check if training for a number of epochs, and before bn freeeze, models
  # reached the same point
  y1 = run_training(unfold_model, epochs, loss_fn, loss_metric, optimizer,
                    train_ds, do_print=False)
  y2_batch = run_training(fold_model_batch, epochs, loss_fn, loss_metric,
                          optimizer, train_ds, do_print=False)
  y2_ema = run_training(fold_model_ema, epochs, loss_fn, loss_metric, optimizer,
                        train_ds, do_print=False)
  assert_allclose(y1, y2_batch, rtol=1e-4)
  assert_allclose(y1, y2_ema, rtol=1e-4)

  # check if training for long enough (after bn freezes), unfold model and fold
  # models should be different, but the two folding modes should be the same
  epochs = 5
  iteration = np.array(8)
  (unfold_model, fold_model_batch) = get_models_with_one_layer(
      kernel_quantizer=None, folding_mode="batch_stats_folding",
      ema_freeze_delay=10)
  (_, fold_model_ema) = get_models_with_one_layer(
      kernel_quantizer=None, folding_mode="ema_stats_folding",
      ema_freeze_delay=10)
  unfold_model.layers[1].set_weights([kernel])
  unfold_model.layers[2].set_weights(
      [gamma, beta, moving_mean, moving_variance])
  fold_model_batch.layers[1].set_weights([
      kernel, gamma, beta, folded_kernel_quantized, folded_bias_quantized,
      iteration, moving_mean, moving_variance
  ])
  fold_model_ema.layers[1].set_weights([
      kernel, gamma, beta, folded_kernel_quantized, folded_bias_quantized,
      iteration, moving_mean, moving_variance
  ])
  y1 = run_training(
      unfold_model,
      epochs,
      loss_fn,
      loss_metric,
      optimizer,
      train_ds,
      do_print=False)
  y2_batch = run_training(
      fold_model_batch,
      epochs,
      loss_fn,
      loss_metric,
      optimizer,
      train_ds,
      do_print=False)
  y2_ema = run_training(
      fold_model_ema,
      epochs,
      loss_fn,
      loss_metric,
      optimizer,
      train_ds,
      do_print=False)
  assert_raises(AssertionError, assert_allclose, y1, y2_batch, rtol=1e-4)
  assert_allclose(y2_batch, y2_ema, rtol=1e-4)
