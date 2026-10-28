"""Aurora MySQL access.

One public function: ``run_query(sql, params=None) -> list[dict]``.

DB_MODE=data_api  (default, production)
    Calls the RDS Data API over HTTPS. If DATA_API_ROLE_ARN is set, the code first
    assumes that role (this is how Account A reads Aurora in Account B). If it is
    empty, the Lambda's own credentials are used (same-account setup).
    No VPC, no NAT, no DB driver, no DB password in the Lambda.

DB_MODE=mysql  (development only)
    Direct pymysql connection (DB_HOST / DB_USER / DB_PASSWORD) for laptops that
    can reach Aurora through a tunnel. Not used in the AWS architecture.

Named parameters are written ``:name`` in both modes.
"""
import json
import logging
import re
import threading
from datetime import datetime, timedelta, timezone

import boto3

from src.config import Config, data_api_client_config

logger = logging.getLogger(__name__)

_ROLE_SESSION_NAME = "estateedge-agent"
_REFRESH_MARGIN = timedelta(minutes=5)

_lock = threading.Lock()
_rds_data = None          # cached rds-data client built from assumed-role credentials
_rds_data_expiry = None   # when those credentials expire (None = never, i.e. own credentials)

# ------------------------------------------------------------------ Data API mode
def _build_rds_data_client():
    """Return (client, expiry). Assumes DATA_API_ROLE_ARN when configured."""
    if not Config.DATA_API_ROLE_ARN:
        return boto3.client("rds-data", region_name=Config.AWS_REGION, config=data_api_client_config()), None

    params = {
        "RoleArn": Config.DATA_API_ROLE_ARN,
        "RoleSessionName": _ROLE_SESSION_NAME,
        "DurationSeconds": 3600,
    }
    if Config.DATA_API_EXTERNAL_ID:
        params["ExternalId"] = Config.DATA_API_EXTERNAL_ID
    creds = boto3.client("sts", region_name=Config.AWS_REGION).assume_role(**params)["Credentials"]
    client = boto3.client(
        "rds-data",
        region_name=Config.AWS_REGION,
        config=data_api_client_config(),
        aws_access_key_id=creds["AccessKeyId"],
        aws_secret_access_key=creds["SecretAccessKey"],
        aws_session_token=creds["SessionToken"],
    )
    return client, creds["Expiration"]

def _data_api_client():
    global _rds_data, _rds_data_expiry
    with _lock:
        now = datetime.now(timezone.utc)
        stale = _rds_data is None or (
            _rds_data_expiry is not None and now >= _rds_data_expiry - _REFRESH_MARGIN
        )
        if stale:
            _rds_data, _rds_data_expiry = _build_rds_data_client()
        return _rds_data

def _to_field(value) -> dict:
    if value is None:
        return {"isNull": True}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int):
        return {"longValue": value}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}

def _query_data_api(sql: str, params: dict | None) -> list[dict]:
    Config.require("DATA_API_CLUSTER_ARN", "DATA_API_SECRET_ARN", "DB_NAME")
    kwargs = dict(
        resourceArn=Config.DATA_API_CLUSTER_ARN,
        secretArn=Config.DATA_API_SECRET_ARN,
        database=Config.DB_NAME,
        sql=sql,
        formatRecordsAs="JSON",
    )
    if params:
        kwargs["parameters"] = [{"name": k, "value": _to_field(v)} for k, v in params.items()]
    resp = _data_api_client().execute_statement(**kwargs)
    return json.loads(resp.get("formattedRecords") or "[]")

# ----------------------------------------------------------------- pymysql (dev)
def _query_mysql(sql: str, params: dict | None) -> list[dict]:
    import pymysql  # imported lazily: not needed (or packaged) in data_api mode

    Config.require("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")
    kwargs = dict(
        host=Config.DB_HOST,
        port=Config.DB_PORT or 3306,
        user=Config.DB_USER,
        password=Config.DB_PASSWORD,
        database=Config.DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5,
        read_timeout=15,
        autocommit=True,
    )
    if Config.DB_SSL_CA:
        kwargs["ssl"] = {"ca": Config.DB_SSL_CA}

    conn = pymysql.connect(**kwargs)
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute("SET SESSION max_execution_time = 8000")  # ms, SELECT only
            if params:  # translate ':name' -> '%(name)s' for pymysql
                sql = re.sub(r":(\w+)", r"%(\1)s", sql)
            cur.execute(sql, params)
            return list(cur.fetchall())
    finally:
        conn.close()

# ------------------------------------------------------------------------ public
def run_query(sql: str, params: dict | None = None) -> list[dict]:
    """Run one SQL statement and return the rows as a list of dicts."""
    if Config.DB_MODE == "mysql":
        return _query_mysql(sql, params)
    return _query_data_api(sql, params)
