# Johan's code sandbox. Runs model-written Python with no network, a
# read-only root, no capabilities, capped CPU/RAM/PIDs (see johan_sandbox.py
# for the run flags — the image alone is not the security boundary).
FROM python:3.12-slim
RUN pip install --no-cache-dir numpy pandas matplotlib \
 && rm -rf /root/.cache
ENV MPLBACKEND=Agg PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /work
LABEL johan="sandbox"
