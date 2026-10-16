# Raspberry Pi OS image builder

This project customizes a Raspberry Pi OS `.img` or `.img.xz` file inside a Linux container.
The host only needs Docker; Linux filesystems and loop devices are handled in
the container. The input image is copied to a working file and is never edited.
Compressed `.xz` input is automatically decompressed; output is an uncompressed `.img`.

## Quick start

Put the source image (`.img` or `.img.xz`) in `image-builder/input/`, review
`image-builder/examples/config.yaml`, then run:

```sh
docker compose -f image-builder/compose.yaml run --rm builder \
  build --input /input/raspios.img --config /project/examples/config.yaml \
  --output /output/raspios-custom.img
```

For a safer first run, use `image-builder/basic-config.yaml`. It updates the
system, installs and enables `rustctl`, and preserves the existing package
set. Package pruning is opt-in (`packages.prune: true`); when enabled, every
package you want to keep, including tools such as `sudo`, must be listed in
`packages.keep` or `packages.extra`. The deliberately aggressive example is
`image-builder/minimal-config.yaml`.

Add `--test` to pause before finalizing and open an interactive shell in the
target root filesystem. `exit` resumes the build. Hooks run in the builder
container, outside the target chroot, with `ROOTFS` set to the mounted target.

See [the example configuration](examples/config.yaml) and the hook, Netplan,
and systemd examples in this directory. Configuration source paths are
relative to the YAML file. Netplan files should declare the same renderer as
`netplan.renderer`; the builder installs the corresponding backend package.

`packages.keep` and `packages.extra` define the package roots retained in the
result. APT installs missing roots and their dependencies. The builder marks
installed packages automatic and those roots manual, then purges packages
that APT considers no longer needed. Essential and never-autoremove packages
remain protected by APT.

Hooks run as Bash scripts in the builder container, outside the target chroot.
They receive `ROOTFS` and `BUILD_STAGE`. `ROOTFS` is valid only during that
hook invocation and points to the mounted image root. The supported stages
are `after_packages` and `before_finalize`.

## Platform notes

The service needs loop devices and mount privileges (`privileged: true`). On
Docker Desktop these operations run inside Docker's Linux VM, so the input and
output are ordinary bind-mounted files and no host ext4 driver is needed. On
Linux, QEMU user-mode/binfmt support may be required to run ARM package scripts
from an x86 host. Native ARM hosts do not need CPU emulation for arm64 images.
The builder reports missing prerequisites before changing the working image.

The package allowlist marks the installed packages automatic, marks the
configured roots manual, and asks APT to purge packages no longer needed by
those roots. APT still protects essential and never-autoremove packages. Keep
boot, firmware, and hardware-support packages in the list when the chosen image
does not pull them in as dependencies.
