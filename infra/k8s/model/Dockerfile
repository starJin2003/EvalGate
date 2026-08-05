# =============================================================================
# >>> THE BUILD-IDENTITY CHANGE BELOW IS NOT APPLIED TO THE CLUSTER. <<<
#
# As of 2026-08-05 the node still runs the image built from the PREVIOUS version
# of this file, and every run therefore still records `build_info: "b0-unknown"`.
# The change is committed and unbuilt, deliberately.
#
# WHY IT IS HELD: rebuilding means restarting the model server, and a recompile
# is not measured to produce byte-identical outputs -- 79 of 96 answers moved on
# the last backend change. `adversarial` carries ZeroTolerance(max_regressed_
# cases=0), so ONE moved adversarial case fails the gate, reddens the daily run
# and breaks the three-consecutive-green-days streak that P3's exit depends on.
# Same source tarball is an argument for the outputs being stable, not a
# measurement of it.
#
# APPLY AFTER THE STREAK COMPLETES (day 3 is 2026-08-07 08:00 UTC), and outside
# the 08:00-11:00 UTC daily window:
#
#   ./infra/k8s/model/build-on-node.sh
#   KUBECONFIG=~/.kube/evalgate.yaml \
#     EVALGATE_NODE_SSH="ssh -i ~/.ssh/evalgate_ed25519 ubuntu@64.181.195.241" \
#     ./infra/k8s/model/apply.sh
#   kubectl -n evalgate rollout restart deployment/evalgate-model
#   kubectl -n evalgate rollout status deployment/evalgate-model --timeout=600s
#   # expect: weights digest gate OK, and /props build_info = b10210-bd00e9d6f501
#
# THEN, and this is not optional: run the suite once against the rebuilt server
# and diff its outputs against the current baseline. If 96/96 are identical the
# recompile is inert and the existing baseline stays valid; if any moved, the
# baseline must be re-promoted from a post-rebuild run, because a baseline and a
# candidate built by different compilations are the comparison this guard exists
# to refuse. Either way, re-promote so BOTH sides carry a real build_info --
# until they do, the observed half of the guard is inert (it only fires when both
# sides are non-None, and the promoted baseline predates provenance entirely).
# =============================================================================
#
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

# BUILD IDENTITY, derived from the tarball and from nothing else.
#
# llama.cpp takes its build number and commit from git (cmake/build-info.cmake),
# and this source arrives as a tarball with no `.git`. So both fell back to their
# defaults and every served run reported `build_info: "b0-unknown"` -- which the
# harness dutifully recorded, making the OBSERVED half of the cross-build diff
# guard compare "b0-unknown" to "b0-unknown" and never fire. Only the DECLARED
# `--backend` half was doing any work, and a wrong declaration was undetectable.
# That matters here specifically: swapping the inference engine alone was
# measured to change 79 of 96 outputs and move the suite score 0.004272.
#
# Both values are computed here, at build time, from the artifact:
#
#   number  the `b<N>` suffix of the tarball's own top-level directory
#           (`llama.cpp-b10210/`), which is what the upstream release archive is
#           named after. A future tarball for b12345 extracts to
#           `llama.cpp-b12345/` and this picks up 12345 with no edit.
#   commit  the first 12 hex of sha256(src.tar.gz). The tarball carries no git
#           sha, and inventing one would be worse than useless. This identifies
#           the exact bytes that were compiled, which is the question the guard
#           actually asks. It is NOT a git commit and cannot be fed to `git show`.
#
# Result at /props and in every run artifact: `b10210-<12 hex>`. Nothing in this
# file is hand-typed; replacing the tarball changes both halves automatically.
RUN set -eu; \
    SRC_SHA="$(sha256sum src.tar.gz | cut -c1-12)"; \
    tar -xzf src.tar.gz; \
    rm src.tar.gz; \
    SRC_DIR="$(ls -d llama.cpp-*/ | head -1)"; SRC_DIR="${SRC_DIR%/}"; \
    BUILD_NUM="$(printf '%s' "${SRC_DIR#llama.cpp-b}")"; \
    case "$BUILD_NUM" in ''|*[!0-9]*) \
        echo "FATAL: cannot read a build number from directory '${SRC_DIR}'." >&2; \
        echo "Expected the tarball to extract to llama.cpp-b<number>/." >&2; exit 1;; \
    esac; \
    printf '%s' "$SRC_DIR"   > /src/.src_dir; \
    printf '%s' "$BUILD_NUM" > /src/.build_number; \
    printf '%s' "$SRC_SHA"   > /src/.build_commit; \
    echo "build identity: b${BUILD_NUM}-${SRC_SHA} from ${SRC_DIR}"

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
# -DLLAMA_BUILD_NUMBER / -DLLAMA_BUILD_COMMIT are what carry the identity above
# into the binary. They work because the root CMakeLists guards each with
# `if (NOT DEFINED ...)` before falling back to build-info.cmake's git lookup, and
# a `-D` cache variable IS defined -- so the command line wins and the git path is
# skipped entirely. `llama_build_info()` then returns "b<number>-<commit>", which
# is the exact string llama-server serves at /props.
#
# The binaries are copied to /out so the runtime stage has a fixed path to read:
# the source directory name is now derived, so it cannot be hardcoded downstream.
RUN set -eu; \
    cd "/src/$(cat /src/.src_dir)"; \
    cmake -B build \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF \
        -DGGML_NATIVE=ON \
        -DGGML_OPENMP=ON \
        -DLLAMA_BUILD_TOOLS=ON \
        -DLLAMA_BUILD_SERVER=ON \
        -DLLAMA_BUILD_TESTS=OFF \
        -DLLAMA_BUILD_EXAMPLES=OFF \
        -DLLAMA_CURL=OFF \
        -DLLAMA_BUILD_NUMBER="$(cat /src/.build_number)" \
        -DLLAMA_BUILD_COMMIT="$(cat /src/.build_commit)"; \
    cmake --build build --config Release -j "$(nproc)" --target llama-server llama-bench; \
    mkdir -p /out; \
    cp build/bin/llama-server build/bin/llama-bench /out/

# readelf, not file(1): binutils already comes with build-essential, whereas
# file(1) is not installed on a slim Ubuntu and adding a package just to assert
# an architecture is the wrong trade. The `uname -m` guard above is the real
# emulation check; this confirms the linker agreed.
RUN readelf -h /out/llama-server | grep -q 'Machine:.*AArch64' \
        || { echo "FATAL: llama-server is not an aarch64 ELF binary."; exit 1; }

# Assert the identity actually reached the binary, in the build rather than at
# 03:00 Chicago. `--version` prints "version: <number> (<commit>)"; a build that
# silently fell back to 0/unknown fails here instead of shipping a server whose
# provenance field is a constant.
RUN set -eu; \
    VER="$(/out/llama-server --version 2>&1 || true)"; \
    echo "$VER"; \
    EXPECT="$(cat /src/.build_number)"; \
    echo "$VER" | grep -q "version: ${EXPECT} " \
        || { echo "FATAL: llama-server reports no build number ${EXPECT}; got: ${VER}" >&2; exit 1; }


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

# /out, not /src/llama.cpp-b<version>/build/bin: the source directory name is
# derived from the tarball now, so a hardcoded path here would break the next
# time the tarball changes -- which is precisely the case this change exists for.
COPY --from=builder /out/llama-server /usr/local/bin/llama-server
COPY --from=builder /out/llama-bench  /usr/local/bin/llama-bench

# Matches the securityContext in 40-deployment.yaml. Declared here too so the
# image is not silently root-only if it is ever run outside k8s.
USER 10001:10001

EXPOSE 8080

ENTRYPOINT ["/usr/local/bin/llama-server"]
