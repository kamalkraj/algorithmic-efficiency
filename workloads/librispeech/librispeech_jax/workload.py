import itertools
import os
from typing import Tuple

import ctcdecode
import jax
import jax.numpy as jnp
import Levenshtein
import numpy as np
import spec
import torch
from flax import jax_utils
from flax import linen as nn

from . import ctc_loss, input_pipeline, models


class LibriSpeechWorkload(spec.Workload):
  """A LibriSpeech workload."""
  
  def __init__(self):
    self._train_loader = None
    self._valid_loader = None
    self._model = models.CNNLSTM()
    self._label_dict = {
        "_": 0,
        " ": 1,
        "'": 2,
        "A": 3,
        "B": 4,
        "C": 5,
        "D": 6,
        "E": 7,
        "F": 8,
        "G": 9,
        "H": 10,
        "I": 11,
        "J": 12,
        "K": 13,
        "L": 14,
        "M": 15,
        "N": 16,
        "O": 17,
        "P": 18,
        "Q": 19,
        "R": 20,
        "S": 21,
        "T": 22,
        "U": 23,
        "V": 24,
        "W": 25,
        "X": 26,
        "Y": 27,
        "Z": 28,
    }
    self._rev_label_dict = {v: k for k, v in self._label_dict.items()}
    self._decoder = ctcdecode.CTCBeamDecoder(
        labels=[str(c) for c in self._rev_label_dict], beam_width=1)
    self._loss = ctc_loss.ctc_loss
  
  def has_reached_goal(self, eval_result: float) -> bool:
    return eval_result < self.target_value
  
  def build_input_queue(self, data_rng, split: str, data_dir: str,
                        batch_size: int):
    torch.manual_seed(data_rng[0])
    train_set = input_pipeline.LibriSpeechDataset(
        os.path.join(data_dir, "features_train-clean-100.csv"))
    valid_set = input_pipeline.LibriSpeechDataset(
        os.path.join(data_dir, "features_test-clean.csv"))

    train_collate_fn = train_set.pad_collate

    self._train_loader = torch.utils.data.DataLoader(
        train_set,
        batch_size=batch_size,
        shuffle=True,
        num_workers=2,
        pin_memory=True,
        collate_fn=train_collate_fn)

    self._valid_loader = torch.utils.data.DataLoader(
        valid_set,
        batch_size=batch_size,
        num_workers=2,
        pin_memory=True,
        collate_fn=train_collate_fn)

    return iter(itertools.cycle(self._train_loader))

  def sync_batch_stats(self, model_state):
    """Sync the batch statistics across replicas."""
    # An axis_name is passed to pmap which can then be used by pmean.
    # In this case each device has its own version of the batch statistics and
    # we average them.
    avg_fn = jax.pmap(lambda x: lax.pmean(x, 'x'), 'x')
    new_model_state = model_state.copy({
      'batch_stats': avg_fn(model_state['batch_stats'])})
    return new_model_state    

  @property
  def param_shapes(self):
    if self._param_shapes is None:
      raise ValueError(
          'This should not happen, workload.init_model_fn() should be called '
          'before workload.param_shapes!')
    return self._param_shapes
  
  @property
  def target_value(self):
    return 0.1

  @property
  def loss_type(self):
    return spec.LossType.CTC_LOSS

  @property
  def num_train_examples(self):
    return 28539

  @property
  def num_eval_examples(self):
    return 2620

  @property
  def train_mean(self):
    return 0.0

  @property
  def train_stddev(self):
    return 1.0
  
  def model_params_types(self):
    pass

  @property
  def max_allowed_runtime_sec(self):
    return 80000

  @property
  def eval_period_time_sec(self):
    return 800

  # Return whether or not a key in spec.ParameterContainer is the output layer
  # parameters.
  def is_output_params(self, param_key: spec.ParameterKey) -> bool:
    pass

  def preprocess_for_train(self, selected_raw_input_batch: spec.Tensor,
                           selected_label_batch: spec.Tensor,
                           train_mean: spec.Tensor, train_stddev: spec.Tensor,
                           rng: spec.RandomState) -> spec.Tensor:
    del train_mean
    del train_stddev
    del rng
    return selected_raw_input_batch, selected_label_batch

  def preprocess_for_eval(
      self,
      raw_input_batch: spec.Tensor,
      train_mean: spec.Tensor,
      train_stddev: spec.Tensor) -> spec.Tensor:
    del train_mean
    del train_stddev
    return raw_input_batch
  
  _InitState = Tuple[spec.ParameterContainer, spec.ModelAuxiliaryState]
  def init_model_fn(self, rng: spec.RandomState) -> _InitState:
    init_val = [jnp.ones((1, 1, 161, 2453), jnp.float32), jnp.array([2087])]
    params, model_state = self._model.init(rng, *init_val, train=True)['params']
    self._param_shapes = jax.tree_map(
      lambda x: spec.ShapeTuple(x.shape),
      params)
    model_state = jax_utils.replicate(model_state)
    params = jax_utils.replicate(params)
    return params, model_state

  def model_fn(
      self,
      params: spec.ParameterContainer,
      augmented_and_preprocessed_input_batch: spec.Tensor,
      model_state: spec.ModelAuxiliaryState,
      mode: spec.ForwardPassMode,
      rng: spec.RandomState,
      update_batch_norm: bool) -> Tuple[spec.Tensor, spec.ModelAuxiliaryState]:
    variables = {'params': params, **model_state}
    train = mode == spec.ForwardPassMode.TRAIN
    features, input_lengths = augmented_and_preprocessed_input_batch
    if update_batch_norm:
      log_y, output_lengths = self._model.apply(
        variables, features, input_lengths, training=train, mutable=['batch_stats'])
      return (log_y, output_lengths), model_state
    else:
      log_y, output_lengths = self._model.apply(
        variables, features, input_lengths, training=train)
      return (log_y, output_lengths), model_state
  
  def loss_fn(
      self,
      label_batch: spec.Tensor,
      logits_batch: spec.Tensor) -> spec.Tensor: 

    log_y, output_lengths = logits_batch
    max_length = log_y.shape[1]
    logprobspaddings = np.array([[0]*x + [1]*(max_length-x) for x in output_lengths])
    labels_lengths = [len(y[y != 0]) for y in label_batch]
    max_length = logits_batch.shape[1]
    labelspaddings = np.array([[0]*x + [1]*(max_length-x) for x in labels_lengths])
    loss, _ = self._loss(log_y, logprobspaddings, label_batch, labelspaddings)

    return jnp.mean(loss)

  