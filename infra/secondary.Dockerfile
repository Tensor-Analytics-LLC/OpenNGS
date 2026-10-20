ARG CORE_IMAGE
FROM ${CORE_IMAGE}
USER root
COPY infra/secondary-environments.json /opt/studio/secondary-environments.json
COPY scripts/install_secondary_environments.py /opt/studio/install_secondary_environments.py
RUN python /opt/studio/install_secondary_environments.py && rm -rf /work/.mamba /work/.cache && chown -R 1000:1000 /work /inputs
ENV PATH=/opt/conda/bin:/opt/extended/bin:/opt/cnvkit/bin:/opt/annotation/bin:/opt/happy/bin:/opt/legacy/bin:$PATH
ENV NUMBA_CACHE_DIR=/tmp/numba MPLCONFIGDIR=/tmp/matplotlib
COPY backend /opt/studio/backend
COPY infra/*.py /opt/studio/
COPY scripts/qualify_secondary_runtime.py /opt/studio/qualify_secondary_runtime.py
USER 1000:1000
RUN python /opt/studio/qualify_secondary_runtime.py /qualification/secondary-report.json
WORKDIR /work
CMD ["python", "/opt/studio/cloud_worker.py"]
