ARG CORE_IMAGE
FROM ${CORE_IMAGE}
USER root
RUN rm -rf /work/.mamba /work/.cache && chown -R 1000:1000 /work /inputs
COPY backend /opt/studio/backend
COPY infra/*.py /opt/studio/
COPY scripts/qualify_secondary_runtime.py /opt/studio/qualify_secondary_runtime.py
USER 1000:1000
RUN python /opt/studio/qualify_secondary_runtime.py /qualification/secondary-report.json
WORKDIR /work
CMD ["python", "/opt/studio/cloud_worker.py"]
