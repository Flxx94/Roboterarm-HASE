FROM rust:bookworm AS rustctl-build
RUN apt-get update && apt-get install -y --no-install-recommends \
      gcc-aarch64-linux-gnu libc6-dev-arm64-cross ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN rustup target add aarch64-unknown-linux-gnu
WORKDIR /src/rustctl
COPY rustctl/ ./
ENV CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc
RUN cargo build --release --features hardware --target aarch64-unknown-linux-gnu

FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates coreutils e2fsprogs fdisk findutils mount \
      parted python3 python3-yaml rsync systemd util-linux xz-utils \
    && rm -rf /var/lib/apt/lists/*
COPY image-builder/builder/ /opt/image-builder/
COPY --from=rustctl-build /src/rustctl/target/aarch64-unknown-linux-gnu/release/rustctl /opt/image-builder/artifacts/rustctl-aarch64
RUN chmod +x /opt/image-builder/cli.py && ln -s /opt/image-builder/cli.py /usr/local/bin/image-builder
WORKDIR /workspace
ENTRYPOINT ["image-builder"]
