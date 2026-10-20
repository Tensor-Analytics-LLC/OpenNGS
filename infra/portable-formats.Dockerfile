ARG FORMAT_BASE_IMAGE
FROM ${FORMAT_BASE_IMAGE}
# Reuse exact existing binaries and grant-aware worker. Only the eight newly
# qualified routes may select this digest; inherited tool proofs do not move.
COPY backend/portable.py backend/scientific.py backend/format_operators.py backend/format_contract.py /opt/studio/backend/
COPY scripts/qualify_portable_formats.py /opt/studio/qualify_portable_formats.py
WORKDIR /work
ENTRYPOINT []
CMD ["python", "/opt/studio/cloud_worker.py"]
