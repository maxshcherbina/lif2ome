## lif2ome in a container: the folder with the .lif files is mounted at /data
FROM python:3.13-slim
COPY pyproject.toml README.md /src/
COPY src /src/src
## ponytail: pip takes the newest liffile and tifffile, not the versions in uv.lock; pin them here if a new one breaks
RUN pip install --no-cache-dir /src
WORKDIR /data
ENTRYPOINT ["lif2ome"]
