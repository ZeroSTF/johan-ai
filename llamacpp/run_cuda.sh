#!/bin/bash
# Run a llama.cpp CUDA tool (default llama-server) in an Ubuntu 24.04 container:
# the release build needs glibc 2.38, Pop!_OS 22.04 has 2.35. The GPU comes in as
# bind-mounted /dev/nvidia* + the host driver's libcuda; the CUDA 12.8 runtime libs
# are borrowed from venv-comfy's nvidia wheels. Host network, port 8080 by default.
#   ./run_cuda.sh [tool] args...      e.g. ./run_cuda.sh llama-server -m /models/x.gguf
H=/home/zeros/johan
NV=$H/venv-comfy/lib/python3.10/site-packages/nvidia
TOOL=llama-server; case "$1" in llama-*) TOOL=$1; shift;; esac
LIBS=$(for d in cuda_runtime cublas; do printf '%s:' /nv/$d/lib; done)
exec docker run --rm --name "johan-llama-$TOOL" --label johan=llama --network host \
  --cpuset-cpus 8-15 \
  --device /dev/nvidia0 --device /dev/nvidiactl --device /dev/nvidia-uvm --device /dev/nvidia-uvm-tools \
  -v /usr/lib/x86_64-linux-gnu/libcuda.so.1:/usr/lib/x86_64-linux-gnu/libcuda.so.1:ro \
  -v /usr/lib/x86_64-linux-gnu/libnvidia-ptxjitcompiler.so.1:/usr/lib/x86_64-linux-gnu/libnvidia-ptxjitcompiler.so.1:ro \
  -v /usr/lib/x86_64-linux-gnu/libgomp.so.1:/usr/lib/x86_64-linux-gnu/libgomp.so.1:ro \
  -v $H/llamacpp/cuda/llama-b11829:/llama:ro -v $NV:/nv:ro -v $H/llamacpp/models:/models:ro \
  -v $H/llamacpp/cuda-cache:/cache \
  -e LD_LIBRARY_PATH=/llama:${LIBS%:} -e CUDA_CACHE_PATH=/cache -e CUDA_CACHE_MAXSIZE=2147483648 \
  ubuntu:24.04 /llama/$TOOL "$@"
