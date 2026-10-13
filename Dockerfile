FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY deploy/fetch_rum_sdk.py /tmp/fetch_rum_sdk.py
COPY shopsim ./shopsim
# The storefront serves the Motadata RUM browser SDK itself (pinned, integrity-checked).
RUN python /tmp/fetch_rum_sdk.py /app/shopsim/storefront/vendor

# One image, five roles: control | catalog | orders | payments | web
ENTRYPOINT ["python", "-m", "shopsim"]
CMD ["control"]
