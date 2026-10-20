ARG MAMBA_IMAGE
FROM ${MAMBA_IMAGE}
USER root
RUN mkdir -p /opt/studio /work /inputs /tmp && chmod 1777 /tmp && chown -R 1000:1000 /work /inputs
USER $MAMBA_USER
RUN micromamba install -y -n base -c conda-forge -c bioconda --strict-channel-priority python=3.12 bwa-mem2=2.2.1 gatk4=4.6.2.0 picard=3.4.0 samtools=1.21 bcftools=1.21 fastp=0.24.0 fastqc=0.12.1 minimap2=2.28 freebayes=1.3.6 && micromamba list -n base --explicit > /tmp/conda-explicit.lock && micromamba clean --all --yes
USER root
COPY requirements.lock /opt/studio/requirements.lock
ENV PATH=/opt/conda/bin:$PATH PYTHONPATH=/opt/studio PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 NGS_STATE_DIR=/work/state NGS_DATA_DIR=/work/data HOME=/work TMPDIR=/tmp
RUN /opt/conda/bin/python -m pip install --no-cache-dir -r /opt/studio/requirements.lock && cp /tmp/conda-explicit.lock /opt/studio/environment-explicit.lock && python --version && samtools --version | head -2 && bcftools --version | head -2 && gatk --version && test -f /opt/studio/environment-explicit.lock
COPY backend /opt/studio/backend
COPY infra/cloud_worker.py /opt/studio/cloud_worker.py
USER 1000:1000
VOLUME ["/work", "/inputs", "/tmp"]
WORKDIR /work
ENTRYPOINT []
CMD ["python", "/opt/studio/cloud_worker.py"]
