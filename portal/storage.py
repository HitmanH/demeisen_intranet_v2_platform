"""File storage: local data/ folders, or private S3 buckets when FILES_BUCKET is set. Files are produced in the local
working folders first, then pushed to S3. AUDIT_BUCKET is the write-once copy of the audit trail (S3 Object Lock)."""
import json, os, uuid
from datetime import datetime, timezone

BUCKET = os.environ.get("FILES_BUCKET")
AUDIT_BUCKET = os.environ.get("AUDIT_BUCKET")
REGION = os.environ.get("AWS_REGION", "eu-west-2")
_s3 = None


def s3():
    global _s3
    if _s3 is None:
        import boto3
        from botocore.config import Config
        _s3 = boto3.client("s3", region_name=REGION, endpoint_url=f"https://s3.{REGION}.amazonaws.com",
                           config=Config(signature_version="s3v4"))
    return _s3


def put(kind, path):
    """Persist a file that was just written to the local <kind> folder."""
    if BUCKET:
        s3().upload_file(path, BUCKET, f"{kind}/{os.path.basename(path)}")


def download_url(kind, name):
    return s3().generate_presigned_url("get_object", ExpiresIn=300, Params={
        "Bucket": BUCKET, "Key": f"{kind}/{name}",
        "ResponseContentDisposition": f'attachment; filename="{name}"'})


def put_audit(record):
    t = datetime.now(timezone.utc)
    key = f"audit/{t:%Y/%m/%d}/{t:%H%M%S}-{uuid.uuid4().hex[:8]}.json"
    s3().put_object(Bucket=AUDIT_BUCKET, Key=key, Body=json.dumps(record, default=str).encode(),
                    ContentType="application/json", ChecksumAlgorithm="SHA256")
