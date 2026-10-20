ARG CANDIDATE_IMAGE
FROM ${CANDIDATE_IMAGE}
USER root
COPY backend/deepvariant_tools.py /opt/studio/backend/deepvariant_tools.py
COPY backend/deepvariant_runtime.py /opt/studio/backend/deepvariant_runtime.py
COPY scripts/qualify_deepvariant_runtime.py /opt/studio/qualify_deepvariant_runtime.py
USER 1000:1000
WORKDIR /work
ENTRYPOINT []
CMD ["/usr/bin/python3", "/opt/studio/cloud_worker.py"]
