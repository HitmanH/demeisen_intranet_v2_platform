"""File storage: local data/ folders, private S3 buckets (AWS: FILES_BUCKET / AUDIT_BUCKET), or private Azure Blob
containers (Azure: AZURE_STORAGE_ACCOUNT, containers `files` and `audit`). Files are produced in the local working
folders first, then pushed to the cloud copy. The audit copy is the tamper-evident record of the audit trail
(S3 Object Lock on AWS). On Azure the app signs in to storage with its managed identity: no storage key exists."""
import json, os, uuid
from datetime import datetime, timezone

BUCKET = os.environ.get("FILES_BUCKET")
AUDIT_BUCKET = os.environ.get("AUDIT_BUCKET")
AZURE_ACCOUNT = os.environ.get("AZURE_STORAGE_ACCOUNT")
AZURE_AUDIT_CONTAINER = os.environ.get("AUDIT_CONTAINER", "audit")   # write-once (version-level immutability) on Azure
REGION = os.environ.get("AWS_REGION", "eu-west-2")
REMOTE = bool(BUCKET or AZURE_ACCOUNT)            # files live in the cloud, not in local folders
AUDIT_REMOTE = bool(AUDIT_BUCKET or AZURE_ACCOUNT)
_s3 = None
_blob = None


def s3():
    global _s3
    if _s3 is None:
        import boto3
        from botocore.config import Config
        _s3 = boto3.client("s3", region_name=REGION, endpoint_url=f"https://s3.{REGION}.amazonaws.com",
                           config=Config(signature_version="s3v4"))
    return _s3


def blob():
    global _blob
    if _blob is None:
        from azure.identity import ManagedIdentityCredential
        from azure.storage.blob import BlobServiceClient
        _blob = BlobServiceClient(f"https://{AZURE_ACCOUNT}.blob.core.windows.net",
                                  credential=ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID")))
    return _blob


def put(kind, path):
    """Persist a file that was just written to the local <kind> folder."""
    key = f"{kind}/{os.path.basename(path)}"
    if AZURE_ACCOUNT:
        with open(path, "rb") as f:
            blob().get_blob_client("files", key).upload_blob(f, overwrite=True)
    elif BUCKET:
        s3().upload_file(path, BUCKET, key)


def download_url(kind, name):
    return s3().generate_presigned_url("get_object", ExpiresIn=300, Params={
        "Bucket": BUCKET, "Key": f"{kind}/{name}",
        "ResponseContentDisposition": f'attachment; filename="{name}"'})


def download_response(kind, name):
    """A Flask response that gives the signed-in person the file: a short-lived S3 link on AWS; on Azure the app
    streams it (no shareable link is created)."""
    from flask import abort, redirect, Response
    if not AZURE_ACCOUNT:
        return redirect(download_url(kind, name))
    from azure.core.exceptions import ResourceNotFoundError
    try:
        stream = blob().get_blob_client("files", f"{kind}/{name}").download_blob()
    except ResourceNotFoundError:
        abort(404)
    return Response(stream.chunks(), mimetype="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "private, no-store",
        "Content-Length": str(stream.properties.size)})


def put_audit(record):
    t = datetime.now(timezone.utc)
    key = f"audit/{t:%Y/%m/%d}/{t:%H%M%S}-{uuid.uuid4().hex[:8]}.json"
    body = json.dumps(record, default=str).encode()
    if AZURE_ACCOUNT:
        from azure.storage.blob import ContentSettings
        blob().get_blob_client(AZURE_AUDIT_CONTAINER, key).upload_blob(body, overwrite=False,
                                                         content_settings=ContentSettings(content_type="application/json"))
    else:
        s3().put_object(Bucket=AUDIT_BUCKET, Key=key, Body=body, ContentType="application/json", ChecksumAlgorithm="SHA256")
