ARG CORE_IMAGE
FROM ${CORE_IMAGE}
# Reuse the immutable reviewed environments; refresh only application code and
# rerun every native execution qualification before publishing a new digest.
COPY backend /opt/studio/backend
COPY infra/*.py /opt/studio/
COPY demo-data /opt/studio/demo-data
COPY scripts/qualify_extended_runtime.py /opt/studio/qualify_extended_runtime.py
RUN python /opt/studio/qualify_extended_runtime.py --output /qualification/report.json
WORKDIR /work
ENTRYPOINT []
CMD ["python", "/opt/studio/cloud_worker.py"]
