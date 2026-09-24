# The baseline archive is verified against ../image-lock.json and loaded first.
FROM persesdev/perses@sha256:6def46a662a22e790b49c01865c80f547d11d16a83213ac102a0e722d27ece13
COPY --chmod=755 perses /bin/perses
ARG PERF_VERSION=0.54.0-perf.3
LABEL monitoring.patch="perses-${PERF_VERSION}"
