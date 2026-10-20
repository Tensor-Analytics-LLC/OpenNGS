ARG VEP_IMAGE=ensemblorg/ensembl-vep@sha256:ed6b660e278458109afc00d5e730aee240a3bf77e817796386033e1a18963152
FROM ${VEP_IMAGE}
USER root
# Only the worker's Python interpreter is added. The official Perl runtime and
# Ensembl APIs remain those in the pinned scientific base image.
COPY infra/vep-worker-apt.lock /opt/studio/vep-worker-apt.lock
RUN apt-get update && xargs -a /opt/studio/vep-worker-apt.lock apt-get install -y --no-install-recommends && rm -rf /var/lib/apt/lists/*
COPY infra/vep-requirements.lock /opt/studio/vep-requirements.lock
COPY wheels /tmp/openngs-wheels
RUN /usr/bin/python3 -m pip install --no-index --find-links /tmp/openngs-wheels --require-hashes --no-deps --target /opt/openngs -r /opt/studio/vep-requirements.lock && rm -rf /tmp/openngs-wheels && mkdir -p /work /inputs /qualification && chown -R 1000:1000 /work /inputs /qualification && dpkg-query -W > /opt/studio/vep-worker-packages.lock
COPY backend /opt/studio/backend
COPY infra/cloud_worker.py /opt/studio/cloud_worker.py
COPY infra/vep-runtime.json /opt/studio/vep-runtime.json
COPY production-cache-members.json /opt/studio/vep-cache-members.json
COPY fixture-manifest.json /opt/studio/vep-fixture-manifest.json
COPY scripts/qualify_vep_runtime.py /opt/studio/qualify_vep_runtime.py
ENV PYTHONPATH=/opt/openngs:/opt/studio PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 NGS_STATE_DIR=/work/state NGS_DATA_DIR=/work/data HOME=/work TMPDIR=/tmp
USER 1000:1000
WORKDIR /work
VOLUME ["/work", "/inputs", "/tmp"]
ENTRYPOINT []
CMD ["/usr/bin/python3", "/opt/studio/cloud_worker.py"]
