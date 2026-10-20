ARG DEEPVARIANT_IMAGE=google/deepvariant@sha256:eb223b3c487be43d34cc7b08c906b9c558d195716e10672db1bd6e910dc3a00a
FROM ${DEEPVARIANT_IMAGE}
USER root
COPY infra/deepvariant-requirements.lock /opt/studio/deepvariant-requirements.lock
COPY wheels /tmp/openngs-wheels
# --target does not modify DeepVariant's tested TensorFlow interpreter packages.
RUN /usr/bin/python3 -m pip install --no-index --find-links /tmp/openngs-wheels --require-hashes --no-deps --target /opt/openngs -r /opt/studio/deepvariant-requirements.lock && rm -rf /tmp/openngs-wheels && mkdir -p /work /inputs /qualification && chown -R 1000:1000 /work /inputs /qualification
COPY backend /opt/studio/backend
COPY infra/cloud_worker.py /opt/studio/cloud_worker.py
COPY infra/deepvariant-runtime.json /opt/studio/deepvariant-runtime.json
COPY scripts/qualify_deepvariant_runtime.py /opt/studio/qualify_deepvariant_runtime.py
COPY fixtures /qualification/fixtures
ENV PYTHONPATH=/opt/openngs:/opt/studio PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 NGS_STATE_DIR=/work/state NGS_DATA_DIR=/work/data HOME=/work TMPDIR=/tmp
USER 1000:1000
WORKDIR /work
RUN /usr/bin/python3 /opt/studio/qualify_deepvariant_runtime.py /qualification/deepvariant-report.json
VOLUME ["/work", "/inputs", "/tmp"]
ENTRYPOINT []
CMD ["/usr/bin/python3", "/opt/studio/cloud_worker.py"]
