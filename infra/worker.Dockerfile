FROM mambaorg/micromamba:2.3.2
ARG TOOL_SPEC=samtools=1.21
USER root
RUN mkdir -p /opt/studio /work /inputs && chown -R $MAMBA_USER:$MAMBA_USER /work /inputs /opt/studio
USER $MAMBA_USER
RUN micromamba install -y -n base -c conda-forge -c bioconda python=3.13 boto3 pysam httpx cryptography pyyaml ${TOOL_SPEC} && micromamba clean --all --yes
COPY --chown=$MAMBA_USER:$MAMBA_USER infra/cloud_worker.py /opt/studio/cloud_worker.py
COPY --chown=$MAMBA_USER:$MAMBA_USER backend /opt/studio/backend
ENV PATH=/opt/conda/bin:$PATH PYTHONPATH=/opt/studio
WORKDIR /work
ENTRYPOINT []
CMD ["python", "/opt/studio/cloud_worker.py"]
