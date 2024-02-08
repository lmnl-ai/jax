# Copyright 2018 The JAX Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import annotations

from collections.abc import Generator, Iterable, Sequence
from contextlib import contextmanager, ExitStack
import inspect
import io
import functools
from functools import partial
import math
import re
import os
import tempfile
import textwrap
from typing import Any, Callable
import unittest
import warnings
import zlib

from absl.testing import absltest
from absl.testing import parameterized

import numpy as np
import numpy.random as npr

import jax
from jax import lax
from jax.experimental.compilation_cache import compilation_cache
from jax._src.interpreters import mlir
from jax.tree_util import tree_map, tree_all, tree_flatten, tree_unflatten
from jax._src import api
from jax._src import pjit as pjit_lib
from jax._src import config
from jax._src import core
from jax._src import dispatch
from jax._src import linear_util as lu
from jax._src import dtypes as _dtypes
from jax._src import monitoring
from jax._src import stages
from jax._src.cloud_tpu_init import running_in_cloud_tpu_vm
from jax._src.interpreters import pxla
from jax._src.numpy.util import promote_dtypes, promote_dtypes_inexact
from jax._src.util import unzip2
from jax._src.public_test_util import (  # noqa: F401
    _assert_numpy_allclose, _check_dtypes_match, _default_tolerance, _dtype, check_close, check_grads,
    check_jvp, check_vjp, default_gradient_tolerance, default_tolerance, tolerance)
from jax._src import xla_bridge


# This submodule includes private test utilities that are not exported to
# jax.test_util. Functionality appearing here is for internal use only, and
# may be changed or removed at any time and without any deprecation cycle.

_TEST_DUT = config.DEFINE_string(
    'jax_test_dut', '',
    help=
    'Describes the device under test in case special consideration is required.'
)

NUM_GENERATED_CASES = config.DEFINE_integer(
  'jax_num_generated_cases',
  int(os.getenv('JAX_NUM_GENERATED_CASES', '10')),
  help='Number of generated cases to test')

_MAX_CASES_SAMPLING_RETRIES = config.DEFINE_integer(
  'max_cases_sampling_retries',
  int(os.getenv('JAX_MAX_CASES_SAMPLING_RETRIES', '100')),
  'Number of times a failed test sample should be retried. '
  'When an unseen case cannot be generated in this many trials, the '
  'sampling process is terminated.'
)

_SKIP_SLOW_TESTS = config.DEFINE_bool(
    'jax_skip_slow_tests',
    config.bool_env('JAX_SKIP_SLOW_TESTS', False),
    help='Skip tests marked as slow (> 5 sec).'
)

_TEST_TARGETS = config.DEFINE_string(
  'test_targets', os.getenv('JAX_TEST_TARGETS', ''),
  'Regular expression specifying which tests to run, called via re.search on '
  'the test name. If empty or unspecified, run all tests.'
)
_EXCLUDE_TEST_TARGETS = config.DEFINE_string(
  'exclude_test_targets', os.getenv('JAX_EXCLUDE_TEST_TARGETS', ''),
  'Regular expression specifying which tests NOT to run, called via re.search '
  'on the test name. If empty or unspecified, run all tests.'
)
TEST_WITH_PERSISTENT_COMPILATION_CACHE = config.DEFINE_bool(
    'jax_test_with_persistent_compilation_cache',
    config.bool_env('JAX_TEST_WITH_PERSISTENT_COMPILATION_CACHE', False),
    help='If enabled, the persistent compilation cache will be enabled for all '
    'test cases. This can be used to increase compilation cache coverage.')

# We sanitize test names to ensure they work with "unitttest -k" and
# "pytest -k" test filtering. pytest accepts '[' and ']' but unittest -k
# does not. We replace sequences of problematic characters with a single '_'.
kSanitizeNameRE = re.compile(r"[ \"'\[\](){}<>=,._]+")
def sanitize_test_name(s: str) -> str:
  return kSanitizeNameRE.sub("_", s)

def num_float_bits(dtype):
  return _dtypes.finfo(_dtypes.canonicalize_dtype(dtype)).bits

def to_default_dtype(arr):
  """Convert a value to an array with JAX's default dtype.

  This is generally used for type conversions of values returned by numpy functions,
  to make their dtypes take into account the state of the ``jax_enable_x64`` and
  ``jax_default_dtype_bits`` flags.
  """
  arr = np.asarray(arr)
  dtype = _dtypes._default_types.get(arr.dtype.kind)
  return arr.astype(_dtypes.canonicalize_dtype(dtype)) if dtype else arr

def with_jax_dtype_defaults(func, use_defaults=True):
  """Return a version of a function with outputs that match JAX's default dtypes.

  This is generally used to wrap numpy functions within tests, in order to make
  their default output dtypes match those of corresponding JAX functions, taking
  into account the state of the ``jax_enable_x64`` and ``jax_default_dtype_bits``
  flags.

  Args:
    use_defaults : whether to convert any given output to the default dtype. May be
      a single boolean, in which case it specifies the conversion for all outputs,
      or may be a pytree with the same structure as the function output.
  """
  @functools.wraps(func)
  def wrapped(*args, **kwargs):
    result = func(*args, **kwargs)
    if isinstance(use_defaults, bool):
      return tree_map(to_default_dtype, result) if use_defaults else result
    else:
      f = lambda arr, use_default: to_default_dtype(arr) if use_default else arr
      return tree_map(f, result, use_defaults)
  return wrapped

def is_sequence(x):
  try:
    iter(x)
  except TypeError:
    return False
  else:
    return True

def _normalize_tolerance(tol):
  tol = tol or 0
  if isinstance(tol, dict):
    return {np.dtype(k): v for k, v in tol.items()}
  else:
    return {k: tol for k in _default_tolerance}

def join_tolerance(tol1, tol2):
  tol1 = _normalize_tolerance(tol1)
  tol2 = _normalize_tolerance(tol2)
  out = tol1
  for k, v in tol2.items():
    out[k] = max(v, tol1.get(k, 0))
  return out


def check_eq(xs, ys, err_msg=''):
  assert_close = partial(_assert_numpy_allclose, err_msg=err_msg)
  tree_all(tree_map(assert_close, xs, ys))


@contextmanager
def capture_stdout() -> Generator[Callable[[], str], None, None]:
  with unittest.mock.patch('sys.stdout', new_callable=io.StringIO) as fp:
    def _read() -> str:
      return fp.getvalue()
    yield _read


@contextmanager
def count_device_put():
  batched_device_put = pxla.batched_device_put
  count = [0]

  def make_fn_and_count(fn):
    def fn_and_count(*args, **kwargs):
      count[0] += 1
      # device_put handlers might call `dispatch.device_put` (e.g. on an
      # underlying payload or several). We only want to count these
      # recursive puts once, so we skip counting more than the outermost
      # one in such a call stack.
      pxla.batched_device_put = batched_device_put
      try:
        return fn(*args, **kwargs)
      finally:
        pxla.batched_device_put = batched_device_put_and_count
    return fn_and_count

  batched_device_put_and_count = make_fn_and_count(batched_device_put)

  pxla.batched_device_put = batched_device_put_and_count
  try:
    yield count
  finally:
    pxla.batched_device_put = batched_device_put


@contextmanager
def count_primitive_compiles():
  dispatch.xla_primitive_callable.cache_clear()

  count = [-1]
  try:
    yield count
  finally:
    count[0] = dispatch.xla_primitive_callable.cache_info().misses


@contextmanager
def count_pjit_cpp_cache_miss():
  original_pjit_lower = pjit_lib._pjit_lower
  count = [0]

  def pjit_lower_and_count(*args, **kwargs):
    count[0] += 1
    return original_pjit_lower(*args, **kwargs)

  pjit_lib._pjit_lower = pjit_lower_and_count
  try:
    yield count
  finally:
    pjit_lib._pjit_lower = original_pjit_lower


@contextmanager
def count_jit_tracing_cache_miss():
  original_create_pjit_jaxpr = pjit_lib._create_pjit_jaxpr
  count = [0]

  @lu.cache
  def create_pjit_jaxpr_and_count(*args):
    count[0] += 1
    return original_create_pjit_jaxpr(*args)

  pjit_lib._create_pjit_jaxpr = create_pjit_jaxpr_and_count
  try:
    yield count
  finally:
    pjit_lib._create_pjit_jaxpr = original_create_pjit_jaxpr


@contextmanager
def count_aot_jit_cpp_cache_miss():
  original_call = stages.Compiled.call
  count = [0]

  def compiled_call_count(*args, **kwargs):
    count[0] += 1
    return original_call(*args, **kwargs)

  stages.Compiled.call = compiled_call_count
  try:
    yield count
  finally:
    stages.Compiled.call = original_call


@contextmanager
def count_jit_and_pmap_compiles():
  # No need to clear any caches since we generally jit and pmap fresh callables
  # in tests.

  mlir_lower = mlir.lower_jaxpr_to_module
  count = [0]

  def mlir_lower_and_count(*args, **kwargs):
    count[0] += 1
    return mlir_lower(*args, **kwargs)

  mlir.lower_jaxpr_to_module = mlir_lower_and_count
  try:
    yield count
  finally:
    mlir.lower_jaxpr_to_module = mlir_lower


@contextmanager
def count_subjaxpr_to_hlo_conversion(fun_name: str):
  # No need to clear any caches since we generally jit and pmap fresh callables
  # in tests.

  mlir_lower = mlir.lower_jaxpr_to_fun
  count = [0]

  def mlir_lower_and_count(ctx, name, *args, **kwargs):
    if name == fun_name:
      count[0] += 1
    return mlir_lower(ctx, name, *args, **kwargs)

  mlir.lower_jaxpr_to_fun = mlir_lower_and_count
  try:
    yield count
  finally:
    mlir.lower_jaxpr_to_fun = mlir_lower


@contextmanager
def assert_num_jit_and_pmap_compilations(times):
  with count_jit_and_pmap_compiles() as count:
    yield
  if count[0] != times:
    raise AssertionError(f"Expected exactly {times} XLA compilations, "
                         f"but executed {count[0]}")


def device_under_test():
  return _TEST_DUT.value or xla_bridge.get_backend().platform

def supported_dtypes():
  if device_under_test() == "tpu":
    types = {np.bool_, np.int8, np.int16, np.int32, np.uint8, np.uint16,
             np.uint32, _dtypes.bfloat16, np.float16, np.float32, np.complex64}
  elif device_under_test() == "iree":
    types = {np.bool_, np.int8, np.int16, np.int32, np.uint8, np.uint16,
             np.uint32, np.float32}
  else:
    types = {np.bool_, np.int8, np.int16, np.int32, np.int64,
             np.uint8, np.uint16, np.uint32, np.uint64,
             _dtypes.bfloat16, np.float16, np.float32, np.float64,
             np.complex64, np.complex128}
  if not config.enable_x64.value:
    types -= {np.uint64, np.int64, np.float64, np.complex128}
  return types

def is_device_rocm():
  return xla_bridge.get_backend().platform_version.startswith('rocm')

def is_device_cuda():
  return 'cuda' in xla_bridge.get_backend().platform_version

def is_cloud_tpu():
  return running_in_cloud_tpu_vm

def pjrt_c_api_version_at_least(major_version: int, minor_version: int):
  pjrt_c_api_versions = xla_bridge.backend_pjrt_c_api_version()
  if pjrt_c_api_versions is None:
    return True
  return pjrt_c_api_versions >= (major_version, minor_version)

def get_tpu_version() -> int:
  if device_under_test() != "tpu":
    raise ValueError("Device is not TPU")
  kind = jax.devices()[0].device_kind
  if kind.endswith(' lite'):
    kind = kind[:-len(' lite')]
  assert kind[:-1] == "TPU v", kind
  return int(kind[-1])

def is_device_tpu_at_least(version: int) -> bool:
  if device_under_test() != "tpu":
    return False
  return get_tpu_version() >= version

def is_device_tpu(version: int | None = None, variant: str = "") -> bool:
  if device_under_test() != "tpu":
    return False
  if version is None:
    return True
  device_kind = jax.devices()[0].device_kind
  expected_version = f"v{version}{variant}"
  # Special case v5e until the name is updated in device_kind
  if expected_version == "v5e":
    return "v5 lite" in device_kind
  return expected_version in device_kind

def _get_device_tags():
  """returns a set of tags defined for the device under test"""
  if is_device_rocm():
    device_tags = {device_under_test(), "rocm"}
  elif is_device_cuda():
    device_tags = {device_under_test(), "cuda"}
  else:
    device_tags = {device_under_test()}
  return device_tags

def test_device_matches(device_types: Iterable[str]) -> bool:
  assert not isinstance(
      device_types, str
  ), 'device_types should be a list of strings'
  tags = _get_device_tags()
  for device_type in device_types:
    assert isinstance(device_type, str), device_type
    if device_type in tags:
      return True
  return False

test_device_matches.__test__ = False  # This isn't a test case, pytest.

def _device_filter(predicate):
  def skip(test_method):
    @functools.wraps(test_method)
    def test_method_wrapper(self, *args, **kwargs):
      device_tags = _get_device_tags()
      if not predicate():
        test_name = getattr(test_method, '__name__', '[unknown test]')
        raise unittest.SkipTest(
          f"{test_name} not supported on device with tags {device_tags}.")
      return test_method(self, *args, **kwargs)
    return test_method_wrapper
  return skip

def skip_on_devices(*disabled_devices):
  """A decorator for test methods to skip the test on certain devices."""
  return _device_filter(lambda: not test_device_matches(disabled_devices))

def run_on_devices(*enabled_devices):
  """A decorator for test methods to run the test only on certain devices."""
  return _device_filter(lambda: test_device_matches(enabled_devices))

def device_supports_buffer_donation():
  """A decorator for test methods to run the test only on devices that support
  buffer donation."""
  return _device_filter(
      lambda: test_device_matches(mlir._platforms_with_donation)
  )


def set_host_platform_device_count(nr_devices: int):
  """Returns a closure that undoes the operation."""
  prev_xla_flags = os.getenv("XLA_FLAGS")
  flags_str = prev_xla_flags or ""
  # Don't override user-specified device count, or other XLA flags.
  if "xla_force_host_platform_device_count" not in flags_str:
    os.environ["XLA_FLAGS"] = (flags_str +
                               f" --xla_force_host_platform_device_count={nr_devices}")
  # Clear any cached backends so new CPU backend will pick up the env var.
  xla_bridge.get_backend.cache_clear()
  def undo():
    if prev_xla_flags is None:
      del os.environ["XLA_FLAGS"]
    else:
      os.environ["XLA_FLAGS"] = prev_xla_flags
    xla_bridge.get_backend.cache_clear()
  return undo


def skip_on_flag(flag_name, skip_value):
  """A decorator for test methods to skip the test when flags are set."""
  def skip(test_method):        # pylint: disable=missing-docstring
    @functools.wraps(test_method)
    def test_method_wrapper(self, *args, **kwargs):
      flag_value = config._read(flag_name)
      if flag_value == skip_value:
        test_name = getattr(test_method, '__name__', '[unknown test]')
        raise unittest.SkipTest(
          f"{test_name} not supported when FLAGS.{flag_name} is {flag_value}")
      return test_method(self, *args, **kwargs)
    return test_method_wrapper
  return skip


def pytest_mark_if_available(marker: str):
  """A decorator for test classes or methods to pytest.mark if installed."""
  def wrap(func_or_class):
    try:
      import pytest
    except ImportError:
      return func_or_class
    return getattr(pytest.mark, marker)(func_or_class)
  return wrap


def format_test_name_suffix(opname, shapes, dtypes):
  arg_descriptions = (format_shape_dtype_string(shape, dtype)
                      for shape, dtype in zip(shapes, dtypes))
  return '{}_{}'.format(opname.capitalize(), '_'.join(arg_descriptions))


# We use special symbols, represented as singleton objects, to distinguish
# between NumPy scalars, Python scalars, and 0-D arrays.
class ScalarShape:
  def __len__(self): return 0
  def __getitem__(self, i): raise IndexError(f"index {i} out of range.")
class _NumpyScalar(ScalarShape): pass
class _PythonScalar(ScalarShape): pass
NUMPY_SCALAR_SHAPE = _NumpyScalar()
PYTHON_SCALAR_SHAPE = _PythonScalar()


# Some shape combinations don't make sense.
def is_valid_shape(shape, dtype):
  if shape == PYTHON_SCALAR_SHAPE:
    return dtype == np.dtype(type(np.array(0, dtype=dtype).item()))
  return True


def _dims_of_shape(shape):
  """Converts `shape` to a tuple of dimensions."""
  if type(shape) in (list, tuple):
    return shape
  elif isinstance(shape, ScalarShape):
    return ()
  elif np.ndim(shape) == 0:
    return (shape,)
  else:
    raise TypeError(type(shape))


def _cast_to_shape(value, shape, dtype):
  """Casts `value` to the correct Python type for `shape` and `dtype`."""
  if shape is NUMPY_SCALAR_SHAPE:
    # explicitly cast to NumPy scalar in case `value` is a Python scalar.
    return np.dtype(dtype).type(value)
  elif shape is PYTHON_SCALAR_SHAPE:
    # explicitly cast to Python scalar via https://stackoverflow.com/a/11389998
    return np.asarray(value).item()
  elif type(shape) in (list, tuple):
    assert np.shape(value) == tuple(shape)
    return value
  elif np.ndim(shape) == 0:
    assert np.shape(value) == (shape,)
    return value
  else:
    raise TypeError(type(shape))


def dtype_str(dtype):
  return np.dtype(dtype).name


def format_shape_dtype_string(shape, dtype):
  if isinstance(shape, np.ndarray):
    return f'{dtype_str(dtype)}[{shape}]'
  elif isinstance(shape, list):
    shape = tuple(shape)
  return _format_shape_dtype_string(shape, dtype)

@functools.lru_cache(maxsize=64)
def _format_shape_dtype_string(shape, dtype):
  if shape is NUMPY_SCALAR_SHAPE:
    return dtype_str(dtype)
  elif shape is PYTHON_SCALAR_SHAPE:
    return 'py' + dtype_str(dtype)
  elif type(shape) is tuple:
    shapestr = ','.join(str(dim) for dim in shape)
    return f'{dtype_str(dtype)}[{shapestr}]'
  elif type(shape) is int:
    return f'{dtype_str(dtype)}[{shape},]'
  else:
    raise TypeError(type(shape))


def _rand_dtype(rand, shape, dtype, scale=1., post=lambda x: x):
  """Produce random values given shape, dtype, scale, and post-processor.

  Args:
    rand: a function for producing random values of a given shape, e.g. a
      bound version of either np.RandomState.randn or np.RandomState.rand.
    shape: a shape value as a tuple of positive integers.
    dtype: a numpy dtype.
    scale: optional, a multiplicative scale for the random values (default 1).
    post: optional, a callable for post-processing the random values (default
      identity).

  Returns:
    An ndarray of the given shape and dtype using random values based on a call
    to rand but scaled, converted to the appropriate dtype, and post-processed.
  """
  if _dtypes.issubdtype(dtype, np.unsignedinteger):
    r = lambda: np.asarray(scale * abs(rand(*_dims_of_shape(shape)))).astype(dtype)
  else:
    r = lambda: np.asarray(scale * rand(*_dims_of_shape(shape))).astype(dtype)
  if _dtypes.issubdtype(dtype, np.complexfloating):
    vals = r() + 1.0j * r()
  else:
    vals = r()
  return _cast_to_shape(np.asarray(post(vals), dtype), shape, dtype)


def rand_fullrange(rng, standardize_nans=False):
  """Random numbers that span the full range of available bits."""
  def gen(shape, dtype, post=lambda x: x):
    dtype = np.dtype(dtype)
    size = dtype.itemsize * math.prod(_dims_of_shape(shape))
    vals = rng.randint(0, np.iinfo(np.uint8).max, size=size, dtype=np.uint8)
    vals = post(vals).view(dtype)
    if shape is PYTHON_SCALAR_SHAPE:
      # Sampling from the full range of the largest available uint type
      # leads to overflows in this case; sample from signed ints instead.
      if dtype == np.uint64:
        vals = vals.astype(np.int64)
      elif dtype == np.uint32 and not config.enable_x64.value:
        vals = vals.astype(np.int32)
    vals = vals.reshape(shape)
    # Non-standard NaNs cause errors in numpy equality assertions.
    if standardize_nans and np.issubdtype(dtype, np.floating):
      vals[np.isnan(vals)] = np.nan
    return _cast_to_shape(vals, shape, dtype)
  return gen


def rand_default(rng, scale=3):
  return partial(_rand_dtype, rng.randn, scale=scale)


def rand_nonzero(rng):
  post = lambda x: np.where(x == 0, np.array(1, dtype=x.dtype), x)
  return partial(_rand_dtype, rng.randn, scale=3, post=post)


def rand_positive(rng):
  post = lambda x: x + 1
  return partial(_rand_dtype, rng.rand, scale=2, post=post)


def rand_small(rng):
  return partial(_rand_dtype, rng.randn, scale=1e-3)


def rand_not_small(rng, offset=10.):
  post = lambda x: x + np.where(x > 0, offset, -offset)
  return partial(_rand_dtype, rng.randn, scale=3., post=post)


def rand_small_positive(rng):
  return partial(_rand_dtype, rng.rand, scale=2e-5)

def rand_uniform(rng, low=0.0, high=1.0):
  assert low < high
  post = lambda x: x * (high - low) + low
  return partial(_rand_dtype, rng.rand, post=post)


def rand_some_equal(rng):

  def post(x):
    x_ravel = x.ravel()
    if len(x_ravel) == 0:
      return x
    flips = rng.rand(*np.shape(x)) < 0.5
    return np.where(flips, x_ravel[0], x)

  return partial(_rand_dtype, rng.randn, scale=100., post=post)


def rand_some_inf(rng):
  """Return a random sampler that produces infinities in floating types."""
  base_rand = rand_default(rng)

  # TODO: Complex numbers are not correctly tested
  # If blocks should be switched in order, and relevant tests should be fixed
  def rand(shape, dtype):
    """The random sampler function."""
    if not _dtypes.issubdtype(dtype, np.floating):
      # only float types have inf
      return base_rand(shape, dtype)

    if _dtypes.issubdtype(dtype, np.complexfloating):
      base_dtype = np.real(np.array(0, dtype=dtype)).dtype
      out = (rand(shape, base_dtype) +
             np.array(1j, dtype) * rand(shape, base_dtype))
      return _cast_to_shape(out, shape, dtype)

    dims = _dims_of_shape(shape)
    posinf_flips = rng.rand(*dims) < 0.1
    neginf_flips = rng.rand(*dims) < 0.1

    vals = base_rand(shape, dtype)
    vals = np.where(posinf_flips, np.array(np.inf, dtype=dtype), vals)
    vals = np.where(neginf_flips, np.array(-np.inf, dtype=dtype), vals)

    return _cast_to_shape(np.asarray(vals, dtype=dtype), shape, dtype)

  return rand

def rand_some_nan(rng):
  """Return a random sampler that produces nans in floating types."""
  base_rand = rand_default(rng)

  def rand(shape, dtype):
    """The random sampler function."""
    if _dtypes.issubdtype(dtype, np.complexfloating):
      base_dtype = np.real(np.array(0, dtype=dtype)).dtype
      out = (rand(shape, base_dtype) +
             np.array(1j, dtype) * rand(shape, base_dtype))
      return _cast_to_shape(out, shape, dtype)

    if not _dtypes.issubdtype(dtype, np.floating):
      # only float types have inf
      return base_rand(shape, dtype)

    dims = _dims_of_shape(shape)
    r = rng.rand(*dims)
    nan_flips = r < 0.1
    neg_nan_flips = r < 0.05

    vals = base_rand(shape, dtype)
    vals = np.where(nan_flips, np.array(np.nan, dtype=dtype), vals)
    vals = np.where(neg_nan_flips, np.array(-np.nan, dtype=dtype), vals)

    return _cast_to_shape(np.asarray(vals, dtype=dtype), shape, dtype)

  return rand

def rand_some_inf_and_nan(rng):
  """Return a random sampler that produces infinities in floating types."""
  base_rand = rand_default(rng)

  # TODO: Complex numbers are not correctly tested
  # If blocks should be switched in order, and relevant tests should be fixed
  def rand(shape, dtype):
    """The random sampler function."""
    if not _dtypes.issubdtype(dtype, np.floating):
      # only float types have inf
      return base_rand(shape, dtype)

    if _dtypes.issubdtype(dtype, np.complexfloating):
      base_dtype = np.real(np.array(0, dtype=dtype)).dtype
      out = (rand(shape, base_dtype) +
             np.array(1j, dtype) * rand(shape, base_dtype))
      return _cast_to_shape(out, shape, dtype)

    dims = _dims_of_shape(shape)
    posinf_flips = rng.rand(*dims) < 0.1
    neginf_flips = rng.rand(*dims) < 0.1
    nan_flips = rng.rand(*dims) < 0.1

    vals = base_rand(shape, dtype)
    vals = np.where(posinf_flips, np.array(np.inf, dtype=dtype), vals)
    vals = np.where(neginf_flips, np.array(-np.inf, dtype=dtype), vals)
    vals = np.where(nan_flips, np.array(np.nan, dtype=dtype), vals)

    return _cast_to_shape(np.asarray(vals, dtype=dtype), shape, dtype)

  return rand

# TODO(mattjj): doesn't handle complex types
def rand_some_zero(rng):
  """Return a random sampler that produces some zeros."""
  base_rand = rand_default(rng)

  def rand(shape, dtype):
    """The random sampler function."""
    dims = _dims_of_shape(shape)
    zeros = rng.rand(*dims) < 0.5

    vals = base_rand(shape, dtype)
    vals = np.where(zeros, np.array(0, dtype=dtype), vals)

    return _cast_to_shape(np.asarray(vals, dtype=dtype), shape, dtype)

  return rand


def rand_int(rng, low=0, high=None):
  def fn(shape, dtype):
    nonlocal high
    gen_dtype = dtype if np.issubdtype(dtype, np.integer) else np.int64
    if low == 0 and high is None:
      if np.issubdtype(dtype, np.integer):
        high = np.iinfo(dtype).max
      else:
        raise ValueError("rand_int requires an explicit `high` value for "
                         "non-integer types.")
    return rng.randint(low, high=high, size=shape,
                       dtype=gen_dtype).astype(dtype)
  return fn

def rand_unique_int(rng, high=None):
  def fn(shape, dtype):
    return rng.choice(np.arange(high or math.prod(shape), dtype=dtype),
                      size=shape, replace=False)
  return fn

def rand_bool(rng):
  def generator(shape, dtype):
    return _cast_to_shape(
      np.asarray(rng.rand(*_dims_of_shape(shape)) < 0.5, dtype=dtype),
      shape, dtype)
  return generator

def check_raises(thunk, err_type, msg):
  try:
    thunk()
    assert False
  except err_type as e:
    assert str(e).startswith(msg), f"\n{e}\n\n{msg}\n"

def check_raises_regexp(thunk, err_type, pattern):
  try:
    thunk()
    assert False
  except err_type as e:
    assert re.match(pattern, str(e)), f"{e}\n\n{pattern}\n"


def iter_eqns(jaxpr):
  # TODO(necula): why doesn't this search in params?
  yield from jaxpr.eqns
  for subjaxpr in core.subjaxprs(jaxpr):
    yield from iter_eqns(subjaxpr)

def assert_dot_precision(expected_precision, fun, *args):
  jaxpr = api.make_jaxpr(fun)(*args)
  precisions = [eqn.params['precision'] for eqn in iter_eqns(jaxpr.jaxpr)
                if eqn.primitive == lax.dot_general_p]
  for precision in precisions:
    msg = f"Unexpected precision: {expected_precision} != {precision}"
    if isinstance(precision, tuple):
      assert precision[0] == expected_precision, msg
      assert precision[1] == expected_precision, msg
    else:
      assert precision == expected_precision, msg

def assert_dot_preferred_element_type(expected, fun, *args, **kwargs):
  jaxpr = api.make_jaxpr(partial(fun, **kwargs))(*args)
  pref_eltypes = [eqn.params['preferred_element_type'] for eqn in iter_eqns(jaxpr.jaxpr)
                   if eqn.primitive == lax.dot_general_p]
  for pref_eltype in pref_eltypes:
    msg = f"Unexpected preferred_element_type: {expected} != {pref_eltype}"
    assert expected == pref_eltype, msg

def cases_from_gens(*gens):
  sizes = [1, 3, 10]
  cases_per_size = int(NUM_GENERATED_CASES.value / len(sizes)) + 1
  for size in sizes:
    for i in range(cases_per_size):
      yield (f'_{size}_{i}',) + tuple(gen(size) for gen in gens)

def named_cases_from_sampler(gen):
  seen = set()
  retries = 0
  rng = npr.RandomState(42)
  def choose_one(x):
    if not isinstance(x, (list, tuple)):
      x = list(x)
    return [x[rng.randint(len(x))]]
  while (len(seen) < NUM_GENERATED_CASES.value and
         retries < _MAX_CASES_SAMPLING_RETRIES.value):
    retries += 1
    cases = list(gen(choose_one))
    if not cases:
      continue
    if len(cases) > 1:
      raise RuntimeError("Generator is expected to only return a single case when sampling")
    case = cases[0]
    if case["testcase_name"] in seen:
      continue
    retries = 0
    seen.add(case["testcase_name"])
    yield case


# Random sampling for every parameterized test is expensive. Do it once and
# cache the result.
@functools.cache
def _choice(n, m):
  rng = np.random.RandomState(42)
  return rng.choice(n, size=m, replace=False)

def sample_product_testcases(*args, **kw):
  """Non-decorator form of sample_product."""
  args = [list(arg) for arg in args]
  kw = [(k, list(v)) for k, v in kw.items()]
  n = math.prod(len(a) for a in args) * math.prod(len(v) for _, v in kw)
  testcases = []
  for i in _choice(n, min(n, NUM_GENERATED_CASES.value)):
    testcase = {}
    for a in args:
      testcase.update(a[i % len(a)])
      i //= len(a)
    for k, v in kw:
      testcase[k] = v[i % len(v)]
      i //= len(v)
    testcases.append(testcase)
  return testcases

def sample_product(*args, **kw):
  """Decorator that samples from a cartesian product of test cases.

  Similar to absltest.parameterized.product(), except that it samples from the
  cartesian product rather than returning the whole thing.

  Arguments:
    *args: each positional argument is a list of dictionaries. The entries
      in a dictionary correspond to name=value argument pairs; one dictionary
      will be chosen for each test case. This allows multiple parameters to be
      correlated.
    **kw: each keyword argument is a list of values. One value will be chosen
      for each test case.
  """
  return parameterized.parameters(*sample_product_testcases(*args, **kw))


class JaxTestLoader(absltest.TestLoader):
  def getTestCaseNames(self, testCaseClass):
    names = super().getTestCaseNames(testCaseClass)
    if _TEST_TARGETS.value:
      pattern = re.compile(_TEST_TARGETS.value)
      names = [name for name in names
               if pattern.search(f"{testCaseClass.__name__}.{name}")]
    if _EXCLUDE_TEST_TARGETS.value:
      pattern = re.compile(_EXCLUDE_TEST_TARGETS.value)
      names = [name for name in names
               if not pattern.search(f"{testCaseClass.__name__}.{name}")]
    return names


def with_config(**kwds):
  """Test case decorator for subclasses of JaxTestCase"""
  def decorator(cls):
    assert inspect.isclass(cls) and issubclass(cls, JaxTestCase), "@with_config can only wrap JaxTestCase class definitions."
    cls._default_config = {}
    for b in cls.__bases__:
      cls._default_config.update(b._default_config)
    cls._default_config.update(kwds)
    return cls
  return decorator


def promote_like_jnp(fun, inexact=False):
  """Decorator that promotes the arguments of `fun` to `jnp.result_type(*args)`.

  jnp and np have different type promotion semantics; this decorator allows
  tests make an np reference implementation act more like a jnp
  implementation.
  """
  _promote = promote_dtypes_inexact if inexact else promote_dtypes
  def wrapper(*args, **kw):
    flat_args, tree = tree_flatten(args)
    args = tree_unflatten(tree, _promote(*flat_args))
    return fun(*args, **kw)
  return wrapper


class JaxTestCase(parameterized.TestCase):
  """Base class for JAX tests including numerical checks and boilerplate."""
  _default_config = {
    'jax_enable_checks': True,
    'jax_enable_key_reuse_checks': True,
    'jax_numpy_dtype_promotion': 'strict',
    'jax_numpy_rank_promotion': 'raise',
    'jax_traceback_filtering': 'off',
    'jax_legacy_prng_key': 'error',
  }

  _compilation_cache_exit_stack: ExitStack | None = None

  # TODO(mattjj): this obscures the error messages from failures, figure out how
  # to re-enable it
  # def tearDown(self) -> None:
  #   assert core.reset_trace_state()

  def setUp(self):
    super().setUp()
    self._original_config = {}
    for key, value in self._default_config.items():
      self._original_config[key] = config._read(key)
      config.update(key, value)

    # We use the adler32 hash for two reasons.
    # a) it is deterministic run to run, unlike hash() which is randomized.
    # b) it returns values in int32 range, which RandomState requires.
    self._rng = npr.RandomState(zlib.adler32(self._testMethodName.encode()))

  def tearDown(self):
    for key, value in self._original_config.items():
      config.update(key, value)
    super().tearDown()

  @classmethod
  def setUpClass(cls):
    if TEST_WITH_PERSISTENT_COMPILATION_CACHE.value:
      cls._compilation_cache_exit_stack = ExitStack()
      stack = cls._compilation_cache_exit_stack
      stack.enter_context(config.enable_compilation_cache(True))
      stack.enter_context(config.raise_persistent_cache_errors(True))
      stack.enter_context(config.persistent_cache_min_compile_time_secs(0))
      stack.enter_context(config.persistent_cache_min_entry_size_bytes(0))

      tmp_dir = stack.enter_context(tempfile.TemporaryDirectory())
      compilation_cache.set_cache_dir(tmp_dir)
      stack.callback(lambda: compilation_cache.reset_cache())

  @classmethod
  def tearDownClass(cls):
    if TEST_WITH_PERSISTENT_COMPILATION_CACHE.value:
      cls._compilation_cache_exit_stack.close()

  def rng(self):
    return self._rng

  def assertArraysEqual(self, x, y, *, check_dtypes=True, err_msg='',
                        allow_object_dtype=False, verbose=True):
    """Assert that x and y arrays are exactly equal."""
    if check_dtypes:
      self.assertDtypesMatch(x, y)
    x = np.asarray(x)
    y = np.asarray(y)

    if (not allow_object_dtype) and (x.dtype == object or y.dtype == object):
      # See https://github.com/google/jax/issues/17867
      raise TypeError(
        "assertArraysEqual may be poorly behaved when np.asarray casts to dtype=object. "
        "If comparing PRNG keys, consider random_test.KeyArrayTest.assertKeysEqual. "
        "If comparing collections of arrays, consider using assertAllClose. "
        "To let this test proceed anyway, pass allow_object_dtype=True.")

    # Work around https://github.com/numpy/numpy/issues/18992
    with np.errstate(over='ignore'):
      np.testing.assert_array_equal(x, y, err_msg=err_msg,
                                    verbose=verbose)

  def assertArraysAllClose(self, x, y, *, check_dtypes=True, atol=None,
                           rtol=None, err_msg=''):
    """Assert that x and y are close (up to numerical tolerances)."""
    self.assertEqual(x.shape, y.shape)
    atol = max(tolerance(_dtype(x), atol), tolerance(_dtype(y), atol))
    rtol = max(tolerance(_dtype(x), rtol), tolerance(_dtype(y), rtol))

    _assert_numpy_allclose(x, y, atol=atol, rtol=rtol, err_msg=err_msg)

    if check_dtypes:
      self.assertDtypesMatch(x, y)

  def assertDtypesMatch(self, x, y, *, canonicalize_dtypes=True):
    if not config.enable_x64.value and canonicalize_dtypes:
      self.assertEqual(_dtypes.canonicalize_dtype(_dtype(x), allow_extended_dtype=True),
                       _dtypes.canonicalize_dtype(_dtype(y), allow_extended_dtype=True))
    else:
      self.assertEqual(_dtype(x), _dtype(y))

  def assertAllClose(self, x, y, *, check_dtypes=True, atol=None, rtol=None,
                     canonicalize_dtypes=True, err_msg=''):
    """Assert that x and y, either arrays or nested tuples/lists, are close."""
    if isinstance(x, dict):
      self.assertIsInstance(y, dict)
      self.assertEqual(set(x.keys()), set(y.keys()))
      for k in x.keys():
        self.assertAllClose(x[k], y[k], check_dtypes=check_dtypes, atol=atol,
                            rtol=rtol, canonicalize_dtypes=canonicalize_dtypes,
                            err_msg=err_msg)
    elif is_sequence(x) and not hasattr(x, '__array__'):
      self.assertTrue(is_sequence(y) and not hasattr(y, '__array__'))
      self.assertEqual(len(x), len(y))
      for x_elt, y_elt in zip(x, y):
        self.assertAllClose(x_elt, y_elt, check_dtypes=check_dtypes, atol=atol,
                            rtol=rtol, canonicalize_dtypes=canonicalize_dtypes,
                            err_msg=err_msg)
    elif hasattr(x, '__array__') or np.isscalar(x):
      self.assertTrue(hasattr(y, '__array__') or np.isscalar(y))
      if check_dtypes:
        self.assertDtypesMatch(x, y, canonicalize_dtypes=canonicalize_dtypes)
      x = np.asarray(x)
      y = np.asarray(y)
      self.assertArraysAllClose(x, y, check_dtypes=False, atol=atol, rtol=rtol,
                                err_msg=err_msg)
    elif x == y:
      return
    else:
      raise TypeError((type(x), type(y)))

  def assertMultiLineStrippedEqual(self, expected, what):
    """Asserts two strings are equal, after dedenting and stripping each line."""
    expected = textwrap.dedent(expected)
    what = textwrap.dedent(what)
    ignore_space_re = re.compile(r'\s*\n\s*')
    expected_clean = re.sub(ignore_space_re, '\n', expected.strip())
    what_clean = re.sub(ignore_space_re, '\n', what.strip())
    if what_clean != expected_clean:
      # Print it so we can copy-and-paste it into the test
      print(f"Found\n{what}\n")
    self.assertMultiLineEqual(expected_clean, what_clean,
                              msg=f"Found\n{what}\nExpecting\n{expected}")

  @contextmanager
  def assertNoWarnings(self):
    with warnings.catch_warnings():
      warnings.simplefilter("error")
      yield

  def _CompileAndCheck(self, fun, args_maker, *, check_dtypes=True, tol=None,
                       rtol=None, atol=None, check_cache_misses=True):
    """Helper method for running JAX compilation and allclose assertions."""
    args = args_maker()

    def wrapped_fun(*args):
      self.assertTrue(python_should_be_executing)
      return fun(*args)

    python_should_be_executing = True
    python_ans = fun(*args)

    python_shapes = tree_map(lambda x: np.shape(x), python_ans)
    np_shapes = tree_map(lambda x: np.shape(np.asarray(x)), python_ans)
    self.assertEqual(python_shapes, np_shapes)

    cache_misses = dispatch.xla_primitive_callable.cache_info().misses
    python_ans = fun(*args)
    if check_cache_misses:
      self.assertEqual(
          cache_misses, dispatch.xla_primitive_callable.cache_info().misses,
          "Compilation detected during second call of {} in op-by-op "
          "mode.".format(fun))

    cfun = api.jit(wrapped_fun)
    python_should_be_executing = True
    monitored_ans = cfun(*args)

    python_should_be_executing = False
    compiled_ans = cfun(*args)

    self.assertAllClose(python_ans, monitored_ans, check_dtypes=check_dtypes,
                        atol=atol or tol, rtol=rtol or tol)
    self.assertAllClose(python_ans, compiled_ans, check_dtypes=check_dtypes,
                        atol=atol or tol, rtol=rtol or tol)

    args = args_maker()

    python_should_be_executing = True
    python_ans = fun(*args)

    python_should_be_executing = False
    compiled_ans = cfun(*args)

    self.assertAllClose(python_ans, compiled_ans, check_dtypes=check_dtypes,
                        atol=atol or tol, rtol=rtol or tol)

  def _CheckAgainstNumpy(self, numpy_reference_op, lax_op, args_maker,
                         check_dtypes=True, tol=None, atol=None, rtol=None,
                         canonicalize_dtypes=True):
    args = args_maker()
    lax_ans = lax_op(*args)
    numpy_ans = numpy_reference_op(*args)
    self.assertAllClose(numpy_ans, lax_ans, check_dtypes=check_dtypes,
                        atol=atol or tol, rtol=rtol or tol,
                        canonicalize_dtypes=canonicalize_dtypes)

_PJIT_IMPLEMENTATION = jax.jit
_PJIT_IMPLEMENTATION._name = "jit"
_NOOP_JIT_IMPLEMENTATION = lambda x, *args, **kwargs: x
_NOOP_JIT_IMPLEMENTATION._name = "noop"

JIT_IMPLEMENTATION = (
  _PJIT_IMPLEMENTATION,
  _NOOP_JIT_IMPLEMENTATION,
)

class BufferDonationTestCase(JaxTestCase):
  def assertDeleted(self, x):
    self.assertTrue(x.is_deleted())

  def assertNotDeleted(self, x):
    self.assertFalse(x.is_deleted())


@contextmanager
def ignore_warning(*, message='', category=Warning, **kw):
  with warnings.catch_warnings():
    warnings.filterwarnings("ignore", message=message, category=category, **kw)
    yield

# -------------------- Mesh parametrization helpers --------------------

MeshSpec = list[tuple[str, int]]

@contextmanager
def with_mesh(named_shape: MeshSpec) -> Generator[None, None, None]:
  """Test utility for setting up meshes given mesh data from `schedules`."""
  # This is similar to the `with_mesh` function above, but isn't a decorator.
  axis_names, shape = unzip2(named_shape)
  size = math.prod(shape)
  local_devices = list(jax.local_devices())
  if len(local_devices) < size:
    raise unittest.SkipTest(f"Test requires {size} local devices")
  mesh_devices = np.array(local_devices[:size]).reshape(shape)  # type: ignore
  with jax.sharding.Mesh(mesh_devices, axis_names):
    yield

def with_mesh_from_kwargs(f):
  return lambda *args, **kwargs: with_mesh(kwargs['mesh'])(f)(*args, **kwargs)

def with_and_without_mesh(f):
  return parameterized.named_parameters(
    {"testcase_name": name, "mesh": mesh, "axis_resources": axis_resources}
    for name, mesh, axis_resources in (
      ('', (), ()),
      ('Mesh', (('x', 2),), (('i', 'x'),))
    ))(with_mesh_from_kwargs(f))

def create_global_mesh(mesh_shape, axis_names):
  size = math.prod(mesh_shape)
  if len(jax.devices()) < size:
    raise unittest.SkipTest(f"Test requires {size} global devices.")
  devices = sorted(jax.devices(), key=lambda d: d.id)
  mesh_devices = np.array(devices[:size]).reshape(mesh_shape)
  global_mesh = jax.sharding.Mesh(mesh_devices, axis_names)
  return global_mesh


class _cached_property:
  null = object()

  def __init__(self, method):
    self._method = method
    self._value = self.null

  def __get__(self, obj, cls):
    if self._value is self.null:
      self._value = self._method(obj)
    return self._value


class _LazyDtypes:
  """A class that unifies lists of supported dtypes.

  These could be module-level constants, but device_under_test() is not always
  known at import time, so we need to define these lists lazily.
  """
  def supported(self, dtypes):
    supported = supported_dtypes()
    return type(dtypes)(d for d in dtypes if d in supported)

  @_cached_property
  def custom_floats(self):
    return [np.dtype(t) for t in [
      _dtypes.bfloat16, _dtypes.float8_e4m3b11fnuz,
      _dtypes.float8_e4m3fn, _dtypes.float8_e4m3fnuz,
      _dtypes.float8_e5m2, _dtypes.float8_e5m2fnuz]]

  @_cached_property
  def floating(self):
    return self.supported([np.float32, np.float64])

  @_cached_property
  def all_floating(self):
    return self.supported([_dtypes.bfloat16, np.float16, np.float32, np.float64])

  @_cached_property
  def integer(self):
    return self.supported([np.int32, np.int64])

  @_cached_property
  def all_integer(self):
    return self.supported([np.int8, np.int16, np.int32, np.int64])

  @_cached_property
  def unsigned(self):
    return self.supported([np.uint32, np.uint64])

  @_cached_property
  def all_unsigned(self):
    return self.supported([np.uint8, np.uint16, np.uint32, np.uint64])

  @_cached_property
  def complex(self):
    return self.supported([np.complex64, np.complex128])

  @_cached_property
  def boolean(self):
    return self.supported([np.bool_])

  @_cached_property
  def inexact(self):
    return self.floating + self.complex

  @_cached_property
  def all_inexact(self):
    return self.all_floating + self.complex

  @_cached_property
  def numeric(self):
    return self.floating + self.integer + self.unsigned + self.complex

  @_cached_property
  def all(self):
    return (self.all_floating + self.all_integer + self.all_unsigned +
            self.complex + self.boolean)


dtypes = _LazyDtypes()


def strict_promotion_if_dtypes_match(dtypes):
  """
  Context manager to enable strict promotion if all dtypes match,
  and enable standard dtype promotion otherwise.
  """
  if all(dtype == dtypes[0] for dtype in dtypes):
    return jax.numpy_dtype_promotion('strict')
  return jax.numpy_dtype_promotion('standard')

_version_regex = re.compile(r"([0-9]+(?:\.[0-9]+)*)(?:(rc|dev).*)?")
def parse_version(v: str) -> tuple[int, ...]:
  m = _version_regex.match(v)
  if m is None:
    raise ValueError(f"Unable to parse version '{v}'")
  return tuple(int(x) for x in m.group(1).split('.'))

def numpy_version():
  return parse_version(np.__version__)

def parameterized_filterable(*,
    kwargs: Sequence[dict[str, Any]],
    testcase_name: Callable[[dict[str, Any]], str] | None = None,
    one_containing: str | None = None,
):
  """
  Decorator for named parameterized tests, with filtering.

  Works like parameterized.named_parameters, except that it supports the
  `one_containing` option. This is useful to select only one of the tests,
  and to leave the test name unchanged (helps with specifying the desired test
  when debugging).

  Args:
    kwargs: Each entry is a set of kwargs to be passed to the test function.
    testcase_name: Optionally, a function to construct the testcase_name from
      one kwargs dict. If not given then kwarg may contain `testcase_name` and
      if not, the test case name is constructed as `str(kwarg)`.
      We sanitize the test names to work with -k test filters. See
      `sanitize_test_name`.
    one_containing: If given, then leave the test name unchanged, and use
      only one `kwargs` whose `testcase_name` includes `one_containing`.
  """
  # Ensure that all kwargs contain a testcase_name
  kwargs_with_testcase_name: Sequence[dict[str, Any]]
  if testcase_name is not None:
    kwargs_with_testcase_name = [
      dict(testcase_name=sanitize_test_name(str(testcase_name(kw))), **kw)
      for kw in kwargs]
  else:
    for kw in kwargs:
      testcase_name = kw.get("testcase_name")
      if testcase_name is None:
        testcase_name = "_".join(f"{k}={kw[k]}"  # type: ignore
                                 for k in sorted(kw.keys()))
      kw["testcase_name"] = sanitize_test_name(testcase_name)  # type: ignore

    kwargs_with_testcase_name = kwargs
  if one_containing is not None:
    filtered = tuple(kw for kw in kwargs_with_testcase_name
                     if one_containing in kw["testcase_name"])
    assert filtered, (
      f"No testcase_name contains '{one_containing}'. "
      "The testcase_name values are\n  " +
      "\n  ".join(kw["testcase_name"] for kw in kwargs_with_testcase_name))
    kw = filtered[0]
    kw["testcase_name"] = ""
    return parameterized.named_parameters([kw])
  else:
    return parameterized.named_parameters(*kwargs_with_testcase_name)

@contextmanager
def register_event_duration_listener(callback):
  """Manages registering/unregistering an event duration listener callback."""
  try:
    monitoring.register_event_duration_secs_listener(callback)
    yield
  finally:
    monitoring._unregister_event_duration_listener_by_callback(callback)


@contextmanager
def set_env(**kwargs):
  """Context manager to temporarily set/unset one or more environment variables.

  Example:

    >>> import os
    >>> os.environ['my_var'] = 'original'

    >>> with set_env(my_var=None, other_var='some_value'):
    ...   print("my_var is set:", 'my_var' in os.environ)
    ...   print("other_var =", os.environ['other_var'])
    ...
    my_var is set: False
    other_var = some_value

    >>> os.environ['my_var']
    'original'
    >>> 'other_var' in os.environ
    False
  """
  original = {key: os.environ.pop(key, None) for key in kwargs}
  os.environ.update({k: v for k, v in kwargs.items() if v is not None})
  try:
    yield
  finally:
    _ = [os.environ.pop(key, None) for key in kwargs]
    os.environ.update({k: v for k, v in original.items() if v is not None})

def fwd_bwd_jaxprs(f, *example_args):
  fwd_jaxpr, (y_shape, res_shape) = jax.make_jaxpr(
      lambda *args: jax.vjp(f, *args), return_shape=True)(*example_args)
  bwd_jaxpr = jax.make_jaxpr(lambda res, outs: res(outs))(res_shape, y_shape)
  return fwd_jaxpr, bwd_jaxpr


def numpy_vecdot(x, y, axis):
  """Implementation of numpy.vecdot for testing on numpy < 2.0.0"""
  if numpy_version() >= (2, 0, 0):
    raise ValueError("should be calling vecdot directly on numpy 2.0.0")
  x = np.moveaxis(x, axis, -1)
  y = np.moveaxis(y, axis, -1)
  x, y = np.broadcast_arrays(x, y)
  return np.matmul(np.conj(x[..., None, :]), y[..., None])[..., 0, 0]
