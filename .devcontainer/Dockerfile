FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN useradd -m vscode
USER vscode

RUN echo '[[ -d .venv ]] && source .venv/bin/activate' >> /home/vscode/.bashrc
