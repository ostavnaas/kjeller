FROM python:3.10-slim as base
ENV LANG C.UTF-8
ENV LC_ALL C.UTF.8


from base AS python-deps
RUN pip install pipenv
COPY Pipfile .
COPY Pipfile.lock .
RUN PIPENV_VENV_IN_PROJECT=1 pipenv install --deploy


FROM base AS runtime
COPY --from=python-deps /.venv /.venv

RUN groupadd -r python && useradd --no-log-init -rm -d /home/python -g python python
RUN mkdir /opt/code && chown -R python:python /opt/code
ENV PATH="/.venv/bin/:$PATH"

WORKDIR /opt/code
USER python

COPY kjeller/ .

ENTRYPOINT ["python", "main.py"]
