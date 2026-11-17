# Lambda container image for the platform (Debian base so LibreOffice is available; AWS runtime client via awslambdaric).
# One image runs both functions: the main app (in the private network) and the sign-in function (AUTH_ONLY=1).
FROM public.ecr.aws/docker/library/python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-writer-nogui fonts-dejavu fonts-liberation \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY portal ./portal
COPY mnda ./mnda
COPY memo ./memo
COPY documents ./documents
ADD https://truststore.pki.rds.amazonaws.com/eu-west-2/eu-west-2-bundle.pem /app/rds-ca.pem
RUN chmod -R a+rX /app && python -m compileall -q /app
# Lambda: only /tmp is writable
ENV PLATFORM_PROD=1 PYTHONUNBUFFERED=1 DB_SSL_CA=/app/rds-ca.pem HOME=/tmp PLATFORM_DATA=/tmp/data
ENTRYPOINT ["python", "-m", "awslambdaric"]
CMD ["portal.lambda_handler.handler"]
