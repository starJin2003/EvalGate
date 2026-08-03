# llama.cpp b10210, built natively for aarch64 ON the k3s node.
#
# Same rule as every other image here: no buildx, no QEMU, no emulation, and
# nothing built on the M1. The node is aarch64 and builds for itself.
#
# The source is the local training/.scratch/llamacpp/src.tar.gz, streamed in as
# build context rather than fetched from GitHub. It is the byte-identical
# b10210 tree that produced the Homebrew-equivalent binaries which ran the local
# 96-case baseline, so the two sides of the local-vs-node score comparison
# differ in hardware and backend only - not in llama.cpp version. Fetching the
# tag at build time would make that equality depend on a network round trip
# resolving to the same bytes.
#
#   nerdctl --namespace k8s.io build -t evalgate-llama-server:b10210 .

FROM ubuntu:24.04 AS builder

# build-essential and cmake only. No git: the source arrives as a tarball, so
# there is no clone, no submodule fetch, and no network in this stage at all.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
    && rm -rf /var/lib/apt/lists/*

# Fail loudly rather than silently producing an emulated build. If this ever
# reports anything but aarch64, the build is running somewhere it should not be.
RUN test "$(uname -m)" = "aarch64" || { echo "FATAL: not aarch64 ($(uname -m)). This image is built on the node."; exit 1; }

WORKDIR /src
COPY src.tar.gz /src/
RUN tar -xzf src.tar.gz && rm src.tar.gz

WORKDIR /src/llama.cpp-b10210

# GGML_NATIVE=ON is correct here and only here: the build machine IS the run
# machine, so -mcpu=native compiles for the node's actual Neoverse-N1 and picks
# up the dotprod instructions that Q4_K_M's int8 path uses. On any other image
# in this repo that flag would be a portability bug; here the image is pinned to
# one node by construction.
#
# BUILD_SHARED_LIBS=OFF so the runtime stage copies two self-contained binaries
# instead of a library tree.
# LLAMA_BUILD_TOOLS=ON is required: llama-server lives under tools/ in this
# version, not examples/, so LLAMA_BUILD_SERVER alone builds nothing.
# LLAMA_CURL=OFF because the model is mounted from a volume and never
# downloaded, and it removes libcurl from both stages.
RUN cmake -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF \
        -DGGML_NATIVE=ON \
        -DGGML_OPENMP=ON \
        -DLLAMA_BUILD_TOOLS=ON \
        -DLLAMA_BUILD_SERVER=ON \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_EXAMPLES=OFF \
        -DLLAMA_CURL=OFF \
    && cmake --build build --config Release -j "$(nproc)" --target llama-server llama-bench

# readelf, not file(1): binutils already comes with build-essential, whereas
# file(1) is not installed on a slim Ubuntu and adding a package just to assert
# an architecture is the wrong trade. The `uname -m` guard above is the real
# emulation check; this confirms the linker agreed.
RUN readelf -h build/bin/llama-server | grep -q 'Machine:.*AArch64' \
        || { echo "FATAL: llama-server is not an aarch64 ELF binary."; exit 1; }


FROM ubuntu:24.04 AS runtime

# libgomp1 is the only runtime dependency: GGML_OPENMP=ON links libgomp
# dynamically even in an otherwise static build.
# coreutils ships sha256sum, which the Deployment's initContainer runs to
# re-verify the weights on every pod start - that check is the reason this image
# is not FROM scratch.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        coreutils \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /src/llama.cpp-b10210/build/bin/llama-server /usr/local/bin/llama-server
COPY --from=builder /src/llama.cpp-b10210/build/bin/llama-bench  /usr/local/bin/llama-bench

# Matches the securityContext in 40-deployment.yaml. Declared here too so the
# image is not silently root-only if it is ever run outside k8s.
USER 10001:10001

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/llama-server"]
