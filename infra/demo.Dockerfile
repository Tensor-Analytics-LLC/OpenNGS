ARG PYTHON_IMAGE
FROM ${PYTHON_IMAGE}
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/opt/studio NGS_STATE_DIR=/work/state NGS_DATA_DIR=/work/data NGS_DEMO=1 TMPDIR=/tmp HOME=/work OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
RUN apt-get update && apt-get install -y --no-install-recommends fastqc fastp minimap2 samtools bcftools bowtie2 hisat2 freebayes ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements.lock /opt/studio/requirements.lock
RUN pip install --no-cache-dir -r /opt/studio/requirements.lock
COPY backend /opt/studio/backend
COPY infra/demo_worker.py /opt/studio/demo_worker.py
COPY demo-data /opt/studio/demo-data
RUN dpkg-query -W -f='${Package}\t${Version}\n' fastqc fastp minimap2 samtools bcftools bowtie2 hisat2 freebayes > /opt/studio/tool-versions.tsv
RUN mkdir -p /work && chown 65534:65534 /work && chmod 1777 /tmp
USER 65534:65534
VOLUME ["/work", "/tmp"]
WORKDIR /work
ENTRYPOINT ["python", "/opt/studio/demo_worker.py"]
