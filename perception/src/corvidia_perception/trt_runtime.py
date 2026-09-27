"""Minimal TensorRT runner: engine loading and zero-copy I/O buffers, via ctypes.

No PyTorch, PyCUDA, or cuda-python: the CUDA runtime is called through ctypes.

Buffers are pinned host memory mapped into the GPU's address space
(cudaHostAllocMapped). On Jetson the CPU and GPU share DRAM, so this is zero-copy.
Not CUDA managed memory: the Orin reports concurrentManagedAccess = 0, so the
CPU must not touch any managed allocation while any kernel runs in the process.
With two engines on two threads (YOLO and depth) that segfaulted; mapped pinned
memory has no such restriction.
"""

from __future__ import annotations

import ctypes
import json
import os
import struct

import numpy as np

_HOST_ALLOC_MAPPED = 2


class _CudaRuntime:
    def __init__(self) -> None:
        self.lib = ctypes.CDLL(os.environ.get("CUDART_PATH", "/usr/local/cuda/lib64/libcudart.so"))

    def check(self, err: int, what: str) -> None:
        if err != 0:
            self.lib.cudaGetErrorString.restype = ctypes.c_char_p
            raise RuntimeError(f"{what} failed: {self.lib.cudaGetErrorString(err).decode()}")

    def host_alloc_mapped(self, nbytes: int) -> tuple[int, int]:
        """Pinned host memory mapped for the GPU; returns (host pointer, device pointer)."""
        host = ctypes.c_void_p()
        self.check(self.lib.cudaHostAlloc(ctypes.byref(host), ctypes.c_size_t(nbytes),
                                          ctypes.c_uint(_HOST_ALLOC_MAPPED)), "cudaHostAlloc")
        dev = ctypes.c_void_p()
        self.check(self.lib.cudaHostGetDevicePointer(ctypes.byref(dev), host, ctypes.c_uint(0)),
                   "cudaHostGetDevicePointer")
        return host.value, dev.value

    def free_host(self, ptr: int) -> None:
        self.lib.cudaFreeHost(ctypes.c_void_p(ptr))

    def stream_create(self) -> int:
        stream = ctypes.c_void_p()
        self.check(self.lib.cudaStreamCreate(ctypes.byref(stream)), "cudaStreamCreate")
        return stream.value or 0

    def stream_sync(self, stream: int) -> None:
        self.check(self.lib.cudaStreamSynchronize(ctypes.c_void_p(stream)), "cudaStreamSynchronize")

    def stream_destroy(self, stream: int) -> None:
        self.lib.cudaStreamDestroy(ctypes.c_void_p(stream))


def read_engine(path: str) -> tuple[bytes, dict]:
    """Return the TensorRT plan and metadata, stripping an Ultralytics header if present."""
    data = open(path, "rb").read()
    n = struct.unpack("<I", data[:4])[0]
    if 0 < n < 1 << 20 and len(data) > 4 + n:
        try:
            meta = json.loads(data[4:4 + n])
            return data[4 + n:], meta
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
    return data, {}


class TrtEngine:
    """One engine with one execution context, stream, and mapped buffer per tensor.

    `buffers[name]` is a NumPy view of that tensor's memory. Not thread-safe:
    use one TrtEngine per thread.
    """

    def __init__(self, path: str) -> None:
        import tensorrt as trt

        self.path = os.path.expanduser(path)
        plan, self.metadata = read_engine(self.path)
        self._logger = trt.Logger(trt.Logger.WARNING)
        self._runtime = trt.Runtime(self._logger)
        self._engine = self._runtime.deserialize_cuda_engine(plan)
        if self._engine is None:
            raise RuntimeError(f"cannot deserialize TensorRT engine {self.path}")
        self._context = self._engine.create_execution_context()
        self._cuda = _CudaRuntime()
        self._stream = self._cuda.stream_create()
        self._ptrs: dict[str, int] = {}
        self.buffers: dict[str, np.ndarray] = {}
        self.input_names: list[str] = []
        self.output_names: list[str] = []
        for i in range(self._engine.num_io_tensors):
            name = self._engine.get_tensor_name(i)
            shape = tuple(self._engine.get_tensor_shape(name))
            dtype = np.dtype(trt.nptype(self._engine.get_tensor_dtype(name)))
            nbytes = int(np.prod(shape)) * dtype.itemsize
            host, dev = self._cuda.host_alloc_mapped(nbytes)
            view = np.ctypeslib.as_array((ctypes.c_byte * nbytes).from_address(host))
            self._ptrs[name] = host
            self.buffers[name] = view.view(dtype).reshape(shape)
            self._context.set_tensor_address(name, dev)
            if self._engine.get_tensor_mode(name) == trt.TensorIOMode.INPUT:
                self.input_names.append(name)
            else:
                self.output_names.append(name)

    def execute(self) -> None:
        if not self._context.execute_async_v3(self._stream):
            raise RuntimeError(f"TensorRT execution failed ({self.path})")
        self._cuda.stream_sync(self._stream)

    def close(self) -> None:
        if not self._ptrs:
            return
        self._cuda.stream_sync(self._stream)
        for ptr in self._ptrs.values():
            self._cuda.free_host(ptr)
        self._ptrs.clear()
        self.buffers.clear()
        self._cuda.stream_destroy(self._stream)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass
