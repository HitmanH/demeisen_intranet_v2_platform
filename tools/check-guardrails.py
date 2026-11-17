#!/usr/bin/env python3
"""Fails (exit 1) if a data template has lost any of the protections that keep data safe.
The same file is in every Demeisen v2 repo that holds data; keep the copies identical.
Usage: python3 tools/check-guardrails.py <template.yaml> [...]

Every resource of these types must have:
  RDS instance     DeletionPolicy and UpdateReplacePolicy Snapshot or Retain, DeletionProtection set (true, or a
                   condition that is true in the live phase), StorageEncrypted true, BackupRetentionPeriod >= 7
  S3 bucket        DeletionPolicy and UpdateReplacePolicy Retain, versioning enabled, public access blocked
  Cognito pool     DeletionPolicy and UpdateReplacePolicy Retain, DeletionProtection ACTIVE
  Cognito domain   DeletionPolicy and UpdateReplacePolicy Retain
"""
import sys, yaml


class Loader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):
    if isinstance(node, yaml.ScalarNode): v = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode): v = loader.construct_sequence(node, deep=True)
    else: v = loader.construct_mapping(node, deep=True)
    return {"Fn::" + suffix if suffix not in ("Ref", "Condition") else suffix: v}


Loader.add_multi_constructor("!", _tag)


def check(path):
    t = yaml.load(open(path, encoding="utf-8"), Loader=Loader) or {}
    errs = []
    for name, r in (t.get("Resources") or {}).items():
        typ, p = r.get("Type"), r.get("Properties") or {}
        dp, urp = r.get("DeletionPolicy"), r.get("UpdateReplacePolicy")
        need = lambda ok, msg: ok or errs.append(f"{path}: {name} ({typ}): {msg}")
        if typ == "AWS::RDS::DBInstance":
            need(dp in ("Snapshot", "Retain") and urp in ("Snapshot", "Retain"), "DeletionPolicy/UpdateReplacePolicy must be Snapshot or Retain")
            need("DeletionProtection" in p and p["DeletionProtection"] is not False, "DeletionProtection must be set")
            need(p.get("StorageEncrypted") is True, "StorageEncrypted must be true")
            br = p.get("BackupRetentionPeriod")
            need(isinstance(br, int) and br >= 7 or isinstance(br, dict), "BackupRetentionPeriod must be >= 7")
        elif typ == "AWS::S3::Bucket":
            need(dp == "Retain" and urp == "Retain", "DeletionPolicy/UpdateReplacePolicy must be Retain")
            need((p.get("VersioningConfiguration") or {}).get("Status") == "Enabled", "versioning must be enabled")
            pab = p.get("PublicAccessBlockConfiguration") or {}
            need(all(pab.get(k) is True for k in ("BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets")), "public access must be blocked")
        elif typ == "AWS::Cognito::UserPool":
            need(dp == "Retain" and urp == "Retain", "DeletionPolicy/UpdateReplacePolicy must be Retain")
            need(p.get("DeletionProtection") == "ACTIVE", "DeletionProtection must be ACTIVE")
        elif typ == "AWS::Cognito::UserPoolDomain":
            need(dp == "Retain" and urp == "Retain", "DeletionPolicy/UpdateReplacePolicy must be Retain")
    return errs


if __name__ == "__main__":
    errors = [e for f in sys.argv[1:] for e in check(f)]
    print("\n".join(errors) or f"Guardrails OK: {', '.join(sys.argv[1:])}")
    sys.exit(1 if errors else 0)
