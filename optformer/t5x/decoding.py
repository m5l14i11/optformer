"""Fast decoding routines for inference from a trained model.

Forked from t5x/decoding.py.
"""
import functools

from typing import Any, Callable, Mapping, Optional, Tuple, Union
import flax
from flax import traverse_util
import jax
from jax import lax
from jax import random
import jax.numpy as jnp
import numpy as np
from t5x import decoding

DecodingState = decoding.DecodingState

# Constants
# "Effective negative infinity" constant for masking in beam search.
NEG_INF = np.array(-1.0e7)

# Temperatures lower than this are considered 0.0, which is handled specially
# with a conditional. This is to avoid numeric issues from exponentiating on
# 1.0/temperature when temperature is close to 0.0.
MIN_TEMPERATURE = np.array(1e-4)

#------------------------------------------------------------------------------
# Temperature Sampling
#------------------------------------------------------------------------------


@flax.struct.dataclass
class SamplingLoopState:
  """Holds sampling state data.

  Attributes:
    cur_index: [batch_size] array position of the sampling loop in the length
      dimension.
    sequences: [batch_size, num_decodes, max_decode_len] array of current
      sampled sequence prefixes.
    cache: any mapping of arrays, e.g. flax attention cache.
    cur_token: [batch_size, num_decodes] single timestep slice containing
      current tokens.
    ended: [batch_size, num_decodes] binary array marking completed sequences.
    rng: Jax PRNGKey
    log_prob: [batch_size, num_decodes] array of log probs for each sequence.
    mask_idx: int - what index in the mask to sub-index when callback is used
  """
  cur_index: jnp.ndarray
  sequences: jnp.ndarray
  cache: Mapping[str, jnp.ndarray]
  cur_token: jnp.ndarray
  ended: jnp.ndarray
  rng: jnp.ndarray
  log_prob: jnp.ndarray
  mask_idx: jnp.ndarray


def _is_tracer(value: Any):
  return isinstance(value, jax.core.Tracer)


def temperature_sample(
    inputs: jnp.ndarray,
    cache: Mapping[str, jnp.ndarray],
    tokens_to_logits: Callable[[DecodingState],
                               Tuple[jnp.ndarray, Mapping[str, jnp.ndarray]]],
    eos_id: int,
    decode_rng: Optional[jnp.ndarray] = None,
    num_decodes: int = 1,
    temperature: Union[float, jnp.ndarray] = 1.0,
    topk: int = 0,
    topp: float = 0.0,
    cache_offset: int = 0,
    initial_index: Optional[jnp.ndarray] = None,
    max_decode_len: Optional[Union[int, jnp.ndarray]] = None,
    max_decode_steps_hard_limit: Optional[int] = None,
    rescale_log_probs: bool = True,
    state_callback_fn: Optional[Callable[[SamplingLoopState],
                                         SamplingLoopState]] = None,
    logit_callback_fn: Optional[Callable[[jnp.ndarray, SamplingLoopState],
                                         jnp.ndarray]] = None,
    decode_until: Optional[int] = None,
) -> Tuple[jnp.ndarray, jnp.ndarray]:
  """Temperature sampling for language model generation.

  The temperature sampling is performed `num_decodes` times in a vectorized
  manner by expanding the batch dimension. This is similar to how beam search
  expands the batch dimension to process each batch element with multiple beams.

  This function dynamically updates the `inputs` array by sampling from the
  model logits, which is provided by `tokens_to_logits` callable. The input
  sequences are expanded at the end, populated and sliced by dropping the first
  position.

  If `inputs` has non-zero entries, those values are not modified, i.e.,
  the sampled values for those positions are discarded. This simulates the
  teacher forcing on the prefix positions.

  There are a few important observations related to this function.

  1. The `inputs` is assumed to be a non-packed sequence.

  2. If `initial_index=None`, then `inputs`[:, 0] is ignored. We will use 0 as a
     BOS token to start the generation. This inherently assumes that `inputs` is
     already shifted to the right by one position. If `initial_index=an_array`,
     the token values at `inputs`[:, initial_index] are used as the token to
     start the generation.

  3. The loop index, i, is a vector of shape [batch_size]. When beginning
     generation from scratch, each value will always have the same value. When
     beginning with a partially filled cache, the loop index of different
     elements can differ, via providing a value for `initial_index`.

  3. Unless all batch elements generated the eos_id before reaching the end, we
     always make `max_decode_len = inputs.shape[1]` number of calls to
     `tokens_to_logits` when decoding from scratch and
     `max_decode_len - jnp.minimum(initial_index)` number of calls when starting
     from a partially filled cache.

  4. Let `output` be the output sequences, i.e.,`sequences`[:, 1:]. Then
     `output`[:, j] are the tokens generated when the while loop counter `i =
     j`.  Therefore, we generate the last token when `i = max_decode_len - 1`
     and exit the while loop as all `i`s are incremented to `max_decode_len`.

  5. Once `eos_id = 1` is generated, the subsequent predictions are all replaced
     by padding token 0.

  6. When using a partially filled cache, different batch elements can have
     different lengths. This means an input that has a longer input will have
     fewer steps until its `i` value reaches `max_decode_len` than an input with
     a shorter input. We keep these longer examples alive, doing busy work
     continually overwriting a new garbage token at the end of the sequence
     until shorter examples finish.

  7. When using a partially filled cache, providing a value for `initial_index`,
     the attention cache index should be a vector of [batch_size].

  We show three examples to illustrate how this function works. In addition to
  input and output of the function, we also show two intermediate values:
  `expanded_prompt_inputs` and `final_sequences`. Also for simplicity, the
  examples are limited to `num_decodes = 1` usage and the `num_decodes`
  dimension is omitted.

  ```
  Example 1:
                   inputs = [0, 5, 6, 1, 0]
   expanded_prompt_inputs = [0, 5, 6, 1, 0, 0]
          final_sequences = [0, 5, 6, 1, a, b]  # before slicing.
                   output = [5, 6, 1, a, b]
    where `a` is prediction while taking 1 as input and `b` is prediction while
    taking `a` as input.

  Example 2 (early stopping):
                    inputs = [[0, 5, 1, 0, 0, 0, 0],
                              [0, 8, 0, 0, 0, 0, 0]
    expanded_prompt_inputs = [[0, 5, 1, 0, 0, 0, 0, 0],
                              [0, 8, 0, 0, 0, 0, 0, 0]
           final_sequences = [[0, 5, 1, a, b,   c=1, 0, 0],
                              [0, 8, d, e, f=1, g=0, 0, 0]]
                    output = [[5, 1, a, b,   c=1, 0, 0],
                              [8, d, e, f=1, g=0, 0, 0]]

    In this example, there are two sequences. Let's look at sequence 0. The
    first generated token is `a`, which is in turn used to generate `b`.
    Finally, `c = 1` is generated with the input `b`. Then the loop terminates
    early because 1 is the `eos_id`.

    Now consider sequence 1. The when `f = 1` was generated, it is considered
    done. Since sequence 0 is not done at this point, the next prediction, i.e.,
    `g` is zerod out. This continues until the end.

  Example 3 (prefilled cache):
                    inputs = [[0, 5, 2, 6, 1, 0],
                              [0, 8, 1, 0, 0, 0]]
    expanded_prompt_inputs = [[0, 5, 2, 6, 1, 0, 0, 0],
                              [0, 8, 1, 0, 0, 0, 0, 0]]
         max_decode_length = 6
   i = [4, 2]
              input_tokens = [[1],
                              [1]]
             output_tokens = [[a],
                              [b]]
    expanded_prompt_inputs = [[0, 5, 2, 6, 1, a, 0, 0],
                              [0, 8, 1, b, 0, 0, 0, 0]]
   i = [5, 3]
              input_tokens = [[a],
                              [b]]
             output_tokens = [[c],
                              [d]]
    expanded_prompt_inputs = [[0, 5, 2, 6, 1, a, c, 0],
                              [0, 8, 1, b, d, 0, 0, 0]]
   i = [6, 4]
              input_tokens = [[c],
                              [d]]
             output_tokens = [[y],
                              [e]]
    expanded_prompt_inputs = [[0, 5, 2, 6, 1, a, c, y],
                              [0, 8, 1, b, d, e, 0, 0]]
   i = [6, 5]
              input_tokens = [[z],
                              [e]]
             output_tokens = [[z],
                              [f]]
    expanded_prompt_inputs = [[0, 5, 2, 6, 1, a, c, z],
                              [0, 8, 1, b, d, e, f, 0]]
   i = [6, 6]
    exit
                   outputs = [[5, 2, 6, 1, a, c],
                              [8, 1, b, d, e, f]]

    In this example, there are two sequences with different input lengths. Thus
    the two caches had been filled to different positions. As we decode, the
    first sequence hits the max decode length before the second. In order to
    avoid prematurely ending decoding for the second sequence, the first
    sequence continually overwrites the final token.

  Example 4 (prefilled cache and max decode steps):
                    inputs = [[0, 2, 0, 0, 0, 0, 0, 0],
                              [0, 3, 4, 0, 0, 0, 0, 0]]
    expanded_prompt_inputs = [[0, 2, 0, 0, 0, 0, 0, 0, 0, 0]
                              [0, 3, 4, 0, 0, 0, 0, 0, 0, 0]]
           initial_indices = [1, 2]
           max_decode_step = 2

   Then `max_decode_len = [3, 4]`.
   i = [1, 2]
              input_tokens = [[2],
                              [4]]
             output_tokens = [[a],
                              [b]]
    expanded_prompt_inputs = [[0, 2, a, 0, 0, 0, 0, 0, 0, 0]
                              [0, 3, 4, b, 0, 0, 0, 0, 0, 0]]
   i = [2, 3]]
              input_tokens = [[a],
                              [b]]
             output_tokens = [[c],
                              [d]]
    expanded_prompt_inputs = [[0, 2, a, c, 0, 0, 0, 0, 0, 0]
                              [0, 3, 4, b, d, 0, 0, 0, 0, 0]]
    This is the last while loop iteration with i == max_decode_len - 1.
                   outputs = [[2, a, c, 0, 0, 0, 0, 0]
                              [3, 4, b, d, 0, 0, 0, 0]]
  ```

  Args:
    inputs: array: [batch_size, max_decode_len] int32 sequence of tokens.
    cache: flax attention cache.
    tokens_to_logits: fast autoregressive decoder function taking single token
      slices and cache and returning next-token logits and updated cache.
    eos_id: int: end-of-sentence token for target vocabulary.
    decode_rng: JAX PRNGKey.
    num_decodes: number of decoded sequences to be returned.
    temperature: float: sampling temperature factor. As it approaches zero this
      becomes equivalent to greedy sampling.
    topk: integer: if nonzero only use the top-k logits to sample next token, if
      zero don't use any cutoff and sample from full logits over vocabulary.
    topp: float: if nonzero only use the smallest number of logits whose
      cumulative sum of probs adds up to (at least) topp. Will raise ValueError
      if it's nonzero when topk is nonzero.
    cache_offset: axis offset for cache, arising from scanned layers.
    initial_index: Optional[array]: [batch_size] int32 a vector of loop indexes
      to start decoding at.
    max_decode_len: int: an optional maximum number of decoding steps. If
      None, it will decode until the full input shape `inputs.shape[1]` is
      filled. max_decode_len begins counting after the prompt, so it will
      decode at most len(prompt) + max_decode_len tokens.
    max_decode_steps_hard_limit: int: an optional fixed hard limit on
      max_decode_steps. If this is set (not None and > 0), and max_decode_len
      is also set, then max_decode_len will be clipped to this limit. The
      value max_decode_len can be an ndarray, but max_decode_steps_hard_limit
      must be a Python integer or None.
    rescale_log_probs: bool: whether to apply temperature, topp, and topk
      rescaling to the log probs which are returned. If True, the log_probs will
      include these transformations (for example, with topk=1, all log_probs
      will be identically 0.0). If False, the log_probs will not be affected,
      and topk/topp/temperature will not affect sequence probabilities.
    state_callback_fn: Function that modifies the sampling loop state before
      each step. This can be used to manipulate any part of the state either on
      the accelerator or on the host using host callback. The function should
      take a SamplingLoopState as argument, and it returns the updated state.
      See `decoding_test.py` for an example usage.
    logit_callback_fn: Function that modifies the logits before each temperature
      sampling step. The function should take arguments (logits, state) and it
      should return the modified logits. See `decoding_test.py` for an example
      usage.
    decode_until: Timestep to stop decoding. Note that max_decode_len is used to
      specify the maximum length of instantiated decoding tensors.

  Returns:
    A tuple (decodes, log_prob) where `decodes` is sampled sequences with shape
    [batch_size, num_decodes, max_decode_len] sorted by `log_prob`, which is log
    probability of each of the sampled sequences.
  """
  if decode_rng is None:
    decode_rng = jax.random.PRNGKey(0)

  max_decode_steps = max_decode_len
  if (max_decode_steps_hard_limit is not None and
      max_decode_steps_hard_limit > 0 and max_decode_steps is not None):
    max_decode_steps = jnp.minimum(max_decode_steps,
                                   max_decode_steps_hard_limit)

  # [batch, len] -> [batch * num_decodes, len]
  expanded_inputs = flat_batch_beam_expand(inputs, num_decodes)
  expanded_cache = cache_map(
      functools.partial(
          flat_batch_beam_expand, beam_size=num_decodes, offset=cache_offset),
      cache)
  if initial_index is not None:
    initial_index = flat_batch_beam_expand(initial_index, num_decodes)

  # expanded_decodes: [batch * num_decodes, len]
  # expanded_log_prob: [batch * num_decodes]
  expanded_decodes, expanded_log_prob = _temperature_sample_single_trial(
      expanded_inputs,
      expanded_cache,
      tokens_to_logits,
      eos_id,
      decode_rng,
      temperature,
      topk,
      topp,
      initial_index=initial_index,
      max_decode_steps=max_decode_steps,
      rescale_log_probs=rescale_log_probs,
      state_callback_fn=state_callback_fn,
      logit_callback_fn=logit_callback_fn,
      decode_until=decode_until)

  batch_size = inputs.shape[0]
  # [batch * num_decodes, len] -> [batch, num_decodes, len]
  decodes = unflatten_beam_dim(expanded_decodes, batch_size, num_decodes)
  # [batch * num_decodes] -> [batch, num_decodes]
  log_prob = unflatten_beam_dim(expanded_log_prob, batch_size, num_decodes)

  # Sort `decodes` and `log_prob` by increasing log probabilities of the sampled
  # sequence.
  # [batch, num_decodes, 1]
  idxs = jnp.expand_dims(jnp.argsort(log_prob, axis=-1), axis=-1)

  # returns [batch, num_decodes, len], [batch, num_decodes] in sorted order.
  return jnp.take_along_axis(
      decodes, idxs, axis=1), jnp.take_along_axis(
          log_prob, jnp.squeeze(idxs, axis=-1), axis=-1)


def _temperature_sample_single_trial(
    inputs: jnp.ndarray,
    cache: Mapping[str, jnp.ndarray],
    tokens_to_logits: Callable[[DecodingState],
                               Tuple[jnp.ndarray, Mapping[str, jnp.ndarray]]],
    eos_id: int,
    prng_key: jnp.ndarray,
    temperature: Union[float, jnp.ndarray] = 1.0,
    topk: int = 20,
    topp: Union[float, jnp.ndarray] = 0.0,
    initial_index: Optional[jnp.ndarray] = None,
    max_decode_steps: Optional[Union[int, jnp.ndarray]] = None,
    rescale_log_probs: bool = True,
    state_callback_fn: Optional[Callable[[SamplingLoopState],
                                         SamplingLoopState]] = None,
    logit_callback_fn: Optional[Callable[[jnp.ndarray, SamplingLoopState],
                                         jnp.ndarray]] = None,
    decode_until: Optional[int] = None,
) -> jnp.ndarray:
  """A helper function for `temperature_sample`."""

  # We can check the values of topp and topk only if they are not dynamic.
  if not _is_tracer(topp) and topp and topk:
    raise ValueError('At most one of `topp` or `topk` may be non-zero.')

  batch_size, max_decode_len = inputs.shape

  if max_decode_steps is not None:
    # We can check the max_decode_steps bounds only if it is not dynamic.
    if not _is_tracer(max_decode_steps) and max_decode_steps > inputs.shape[1]:
      raise ValueError('Cannot decode more steps than the sequence length.')

    # The number of decode steps required to process the prefix is the number
    #   of non-zero tokens, since inputs[0] == 0 is the BOS token.
    # `max_decode_len[j]` is the number of non-padding tokens in the jth element
    #   of the returned sequences capped at `len(inputs)`, assuming that the
    #   early stop doesn't occur. This is true with or without
    #   `max_decode_steps`.
    # When the while loop index `i` for the `j`th element `i[j] =
    #   max_decode_len[j] - 1`, the generated token populate sequences[i[j]+1]].
    #   Since sequences[:, 0] is BOS token, the generated token is
    #   `max_decode_len[j]`th non-padding tokens and hence `j`th element is
    #   ended.
    max_decode_len = jnp.sum(inputs != 0, axis=1) + max_decode_steps
    max_decode_len = jnp.minimum(inputs.shape[1], max_decode_len)

  if decode_until is None:
    decode_until = max_decode_len

  # In the case of starting generation from a non-zero index, it is possible for
  # one batch element to reach `max_decode_len` number of decoding steps before
  # another. In order to let the last element decoder all the way to
  # `max_decode_len` number of steps, we add a final garbage token to the end of
  # the sequences. Any element that has reached `max_decode_len` before the rest
  # of the elements will continually overwrite this token until all elements
  # finish.
  # [batch, length+1] -> [batch, length+2]
  extra_input_tokens = 2
  expanded_prompt_inputs = jnp.append(
      inputs,
      jnp.zeros((batch_size, extra_input_tokens), dtype=inputs.dtype),
      axis=1)
  end_marker = jnp.array(eos_id)

  temperature = jnp.asarray(temperature)

  # Initialize sampling loop state.
  # initial loop PRNGKey
  rng0 = prng_key
  # the per batch-item holding current token in loop.
  if initial_index is None:
    # the per batch-item loop position counter.
    i0 = jnp.zeros((batch_size), dtype=jnp.int32)
    # the per batch-item holding current token in loop.
    token0 = jnp.zeros((batch_size, 1), dtype=jnp.int32)
    mask_idx0 = jnp.array(0)
  else:
    # the per batch-item loop position counter.
    i0 = initial_index
    # the per batch-item holding current token in loop.
    # Select the token that the initial index is pointing to.
    token0 = jnp.take_along_axis(
        expanded_prompt_inputs, jnp.expand_dims(i0, axis=1), axis=1)
    mask_idx0 = jnp.array(initial_index[0])

  # per batch-item state bit indicating if sentence has finished.
  ended0 = jnp.zeros((batch_size, 1), dtype=jnp.bool_)
  # (batch, length+2) array containing prefix prompt tokens for sampling loop
  # as well as the generated output of newly sampled tokens.
  sequences0 = expanded_prompt_inputs
  log_prob0 = jnp.zeros((batch_size,), dtype=jnp.float32)
  sampling_loop_init_state = SamplingLoopState(i0, sequences0, cache, token0,
                                               ended0, rng0, log_prob0,
                                               mask_idx0)
  # Initial eos count to be used to determine whether eos is "generated". Many
  # inputs follow the format bos, inputs..., eos, targets..., eos. By counting
  # the number of eos tokens we can detect when a new one is added, instead of
  # just finding the one that probably ends the inputs.
  # [batch, 1]
  initial_eos_count = jnp.sum(sequences0 == end_marker, axis=-1, keepdims=True)

  def sampling_loop_cond_fn(state: SamplingLoopState) -> bool:
    """Sampling loop termination condition."""
    # Have all sampled sequences reached an end marker?
    # Different elements in the batch can be at different loop indices, if any
    # of our examples are not at the end, keep going.
    all_sequences_ended = jnp.all(state.ended)
    return ~all_sequences_ended

  def sampling_loop_body_fn(state: SamplingLoopState) -> SamplingLoopState:
    """Sampling loop state update."""

    if state_callback_fn is not None:
      state = state_callback_fn(state)

    # Split RNG for sampling.
    rng1, rng2 = random.split(state.rng)
    # Call fast-decoder model on current tokens to get next-position logits.
    decoding_state = DecodingState(
        cur_index=state.cur_index,
        sequences=state.sequences[:, :-extra_input_tokens],
        cur_token=state.cur_token,
        cache=state.cache)
    logits, new_cache = tokens_to_logits(decoding_state)
    # Sample next token from logits.

    if logit_callback_fn is not None:
      logits = logit_callback_fn(logits, state.mask_idx)

    def sample_logits_with_nonzero_temperature(logits):
      scaled_logits = logits / jnp.maximum(temperature, MIN_TEMPERATURE)
      if topk:
        # Get top-k logits and their indices, sample within these top-k tokens.
        topk_logits, _ = lax.top_k(scaled_logits, topk)
        cutoff_logit = topk_logits[:, -1, None]
        scaled_logits = jnp.where(scaled_logits < cutoff_logit,
                                  jnp.full_like(scaled_logits, NEG_INF),
                                  scaled_logits)

      # When topp is dynamic, we always use it since we cannot check
      # non-zeroness (but it will have no effect if topp is 0.0).
      if _is_tracer(topp) or topp:
        logits_sorted = jnp.sort(
            scaled_logits, axis=-1)[:, ::-1]  # sort descending
        sorted_cum_probs = jnp.cumsum(
            jax.nn.softmax(logits_sorted, axis=-1), axis=-1)
        cutoff_index = jnp.sum(sorted_cum_probs < topp, axis=-1, keepdims=True)
        cutoff_logit = jnp.take_along_axis(logits_sorted, cutoff_index, axis=-1)
        scaled_logits = jnp.where(scaled_logits < cutoff_logit,
                                  jnp.full_like(scaled_logits, NEG_INF),
                                  scaled_logits)

      # [batch]
      next_token = random.categorical(rng1, scaled_logits).astype(jnp.int32)

      # log probability of the current token conditioned on the previously
      # sampled and prefix tokens.
      # [batch, vocab] -> [batch, vocab]
      if rescale_log_probs:
        log_probs = jax.nn.log_softmax(scaled_logits)
      else:
        log_probs = jax.nn.log_softmax(logits)
      # [batch, vocab] -> [batch]
      next_log_prob = jnp.squeeze(
          jnp.take_along_axis(
              log_probs, jnp.expand_dims(next_token, axis=1), axis=-1),
          axis=-1)

      return (next_token, next_log_prob)

    def sample_logits_with_zero_temperature(logits):
      # For zero temperature, we always want the greedy output, regardless
      # of the values of topk and topp.

      next_token = jnp.argmax(logits, -1).astype(jnp.int32)

      if rescale_log_probs:
        next_log_prob = jnp.zeros_like(next_token, dtype=jnp.float32)
      else:
        log_probs = jax.nn.log_softmax(logits)
        next_log_prob = jnp.squeeze(
            jnp.take_along_axis(
                log_probs, jnp.expand_dims(next_token, axis=1), axis=-1),
            axis=-1)

      return (next_token, next_log_prob)

    # Perform sampling with temperature
    (next_token,
     next_log_prob) = lax.cond(temperature > MIN_TEMPERATURE,
                               sample_logits_with_nonzero_temperature,
                               sample_logits_with_zero_temperature, logits)

    # When different batch elements are at different points in the loop counter,
    # it is possible that an element that started at a higher index will reach
    # `max_decode_len` before other elements. When this happens we need to make
    # sure this element continuous overwrites our new garbage collection index.
    # Here we clamp `i` to `max_decode_len`. This will cause the a write to
    # `max_decode_len + 1` which is the final index in `sequences`. Subsequent
    # loop body executions will also get their value clamped causing continual
    # overwriting of the final garbage position until all examples are finished.
    i = jnp.minimum(state.cur_index, max_decode_len)

    # Only use sampled tokens if we're past provided prefix tokens.
    # Select the next token from sequences.
    # [batch]
    next_input_token = jnp.squeeze(
        jnp.take_along_axis(
            state.sequences, jnp.expand_dims(i + 1, axis=1), axis=1),
        axis=1)
    # Check if the next token is padding (a target) or non-padding (an input).
    # Mask will have `1` for targets and `0` for inputs.
    out_of_prompt = (next_input_token == 0)
    # Select the sampled next token for targets and the actual next token for
    # inputs (teacher forcing).
    # [batch]
    next_token = (
        next_token * out_of_prompt + next_input_token * ~out_of_prompt)

    # only add probability if outside prefix region
    # [batch] -> [batch]
    next_log_prob = state.log_prob + (
        next_log_prob * out_of_prompt) * jnp.squeeze(
            ~state.ended, axis=-1).astype(jnp.int32)

    # [batch] -> [batch, 1]
    next_token = jnp.expand_dims(next_token, axis=-1)

    # If end-marker reached for batch item, only emit padding tokens.
    # [batch, 1] * [batch, 1] -> [batch, 1]
    next_token_or_endpad = next_token * ~state.ended
    # Add current sampled tokens to recorded sequences.
    one_hot = jax.nn.one_hot(
        i + 1, state.sequences.shape[1], dtype=state.sequences.dtype)
    new_sequences = state.sequences * (1 -
                                       one_hot) + next_token_or_endpad * one_hot
    # new_sequences = dynamic_update_vector_slice_in_dim(sequences,
    #                                                    next_token_or_endpad,
    #                                                    i + 1,
    #                                                    0)
    # Count eos tokens in the sequences and compare to the initial count
    # [batch, 1]
    cur_eos_count = jnp.sum(new_sequences == end_marker, axis=-1, keepdims=True)
    # [batch, 1]

    # Have we reached max decoding length?
    # We generally index into sequences[:, i + 1], and sequences.shape[1] =
    # max_decode_len + 2, therefore i == max_decode_len - 1 will write to
    # sequences[-2] which is our last valid location. i == max_decode_len will
    # write to sequences[-1] which is our garbage collection token. Thus `i`
    # should be strictly less than max_decode_len.
    has_additional_eos = cur_eos_count > initial_eos_count
    ended = state.ended | has_additional_eos | jnp.expand_dims(
        i >= decode_until - 1, axis=1)
    return SamplingLoopState(i + 1, new_sequences, new_cache,
                             next_token_or_endpad, ended, rng2,
                             next_log_prob, state.mask_idx + 1)

  # Run sampling loop and collect final state.
  final_state = lax.while_loop(sampling_loop_cond_fn, sampling_loop_body_fn,
                               sampling_loop_init_state)

  # Pick part of the state corresponding to the sampled sequences.
  final_sequences = final_state.sequences
  log_prob = final_state.log_prob
  # Drop the first position because they are dummy bos tokens. Drop the new
  # garbage collection token at the end too.
  return final_sequences[:, 1:-1], log_prob


#------------------------------------------------------------------------------
# BEAM Sampling
#------------------------------------------------------------------------------


# Beam handling utility functions:


def cache_map(fn, cache: Any) -> Mapping[str, jnp.ndarray]:
  """Maps function over that caches, even multiple caches in various layers.

  Args:
    fn: The function to apply.
    cache: The cache to apply it to.

  Returns:
    The result of applying `fn` to the cache.
  """
  frozen = isinstance(cache, flax.core.FrozenDict)
  if frozen:
    cache = flax.core.unfreeze(cache)
  flat_cache = traverse_util.flatten_dict(cache)
  keyvals = {k: v for k, v in flat_cache.items() if k[-1] != 'cache_index'}
  # Exclude cached relative position bias from beam expansion, etc.
  # Also excludes scalar index in absolute position embedder from expansion.
  # TODO: generalize cache_map to accept a list of leaf names to
  #   map over, instead of doing this ad-hoc.
  exclusion_list = ['cached_bias', 'position_embedder_index']
  keyvals = {k: v for k, v in keyvals.items() if k[-1] not in exclusion_list}

  keyvals = jax.tree_map(fn, keyvals)
  flat_cache.update(keyvals)
  new_cache = traverse_util.unflatten_dict(flat_cache)
  if frozen:
    new_cache = flax.core.freeze(new_cache)
  return new_cache


def add_beam_dim(x: jnp.ndarray,
                 beam_size: int,
                 offset: int = 0) -> jnp.ndarray:
  """Creates new beam dimension in non-scalar array and tiles into it."""
  x = jnp.expand_dims(x, axis=offset + 1)
  tile_dims = [1] * x.ndim
  tile_dims[offset + 1] = beam_size
  return jnp.tile(x, tile_dims)


def flatten_beam_dim(x: jnp.ndarray, offset: int = 0) -> jnp.ndarray:
  """Flattens the first two dimensions of a non-scalar array."""
  xshape = list(x.shape)
  b_sz = xshape.pop(offset)
  xshape[offset] *= b_sz
  return x.reshape(xshape)


def unflatten_beam_dim(x: jnp.ndarray,
                       batch_size: int,
                       beam_size: int,
                       offset: int = 0) -> jnp.ndarray:
  """Unflattens the first, flat batch*beam dimension of a non-scalar array."""
  assert batch_size * beam_size == x.shape[offset]
  xshape = list(x.shape)
  newshape = xshape[:offset] + [batch_size, beam_size] + xshape[offset + 1:]
  return x.reshape(newshape)


def flat_batch_beam_expand(x: jnp.ndarray,
                           beam_size: int,
                           offset: int = 0) -> jnp.ndarray:
  """Expands the each batch item by beam_size in batch_dimension."""
  return flatten_beam_dim(add_beam_dim(x, beam_size, offset), offset)
