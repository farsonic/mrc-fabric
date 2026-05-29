# mrc-fabric controller — Flask + SSE + telemetry overlay.
# Build context = repo root so gen_fabric can read the lab topology.
FROM python:3.11-slim
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY controller/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY controller/ /app/
# bake fabric.json from the lab topology at build time so the image always
# matches the topology shipped alongside it.
COPY lab/topology.clab.yaml /tmp/topology.clab.yaml
RUN python3 gen_fabric.py --topo /tmp/topology.clab.yaml --out /app/fabric.json \
 && cat /app/fabric.json | python3 -c "import sys,json;d=json.load(sys.stdin);print('fabric.json:',len(d['spines']),'spines,',len(d['leaves']),'leaves,',len(d['hosts']),'hosts')"

ENV FABRIC_JSON=/app/fabric.json
EXPOSE 9810
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s \
  CMD curl -sf http://localhost:9810/healthz || exit 1
CMD ["python3", "app.py"]
