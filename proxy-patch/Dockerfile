# The local pipeline packages the same payload without requiring a Docker daemon.
FROM gcr.io/distroless/static@sha256:2293b36c7c9082bf4115aab724b4d2cddec82c8eba39bf27ac0517e159acf150
COPY --chown=65532:65532 --chmod=755 coder /usr/local/bin/coder
COPY --chown=65532:65532 licenses /usr/share/licenses
COPY --chown=65532:65532 sources.lock.json /usr/share/coder-proxy/sources.lock.json
USER 65532:65532
ENV HOME=/home/nonroot
ENTRYPOINT ["/usr/local/bin/coder"]
CMD ["--help"]
