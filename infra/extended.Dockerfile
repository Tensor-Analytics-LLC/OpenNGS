ARG CORE_IMAGE
FROM ${CORE_IMAGE}
USER root
# The existing core environment is unchanged. Extension entrypoints carry their
# own interpreter shebangs; the worker continues to use the reviewed core Python.
COPY infra/extended-packages.txt /opt/studio/extended-packages.txt
RUN python -c "from pathlib import Path; x=Path('/opt/studio/extended-packages.txt').read_text().splitlines(); Path('/tmp/extended-main.txt').write_text('\n'.join(v for v in x if not v.startswith('cnvkit='))); Path('/tmp/extended-cnvkit.txt').write_text('\n'.join(v for v in x if v.startswith('cnvkit=')))" && micromamba create -y -p /opt/extended -c conda-forge -c bioconda --strict-channel-priority python=3.12 --file /tmp/extended-main.txt && micromamba list -p /opt/extended --explicit > /opt/studio/extended-explicit.lock && micromamba clean --all --yes
# CNVkit's R libraries have a different libdeflate ABI from the pinned SV tools.
RUN micromamba create -y -p /opt/cnvkit -c conda-forge -c bioconda --strict-channel-priority python=3.12 --file /tmp/extended-cnvkit.txt && micromamba list -p /opt/cnvkit --explicit > /opt/studio/cnvkit-explicit.lock && micromamba clean --all --yes
ENV PATH=/opt/conda/bin:/opt/extended/bin:/opt/cnvkit/bin:$PATH
COPY backend /opt/studio/backend
COPY infra/*.py /opt/studio/
COPY demo-data /opt/studio/demo-data
COPY scripts/qualify_extended_runtime.py /opt/studio/qualify_extended_runtime.py
RUN mkdir -p /qualification /work /inputs && chown -R 1000:1000 /qualification /work /inputs
USER 1000:1000
RUN python /opt/studio/qualify_extended_runtime.py --output /qualification/report.json
WORKDIR /work
ENTRYPOINT []
CMD ["python", "/opt/studio/cloud_worker.py"]
