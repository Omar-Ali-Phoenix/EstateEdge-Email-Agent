#!/usr/bin/env python3
"""EstateEdge deploy script.

Reads deployment.config, validates it, runs `sam build` and `sam deploy`,
renders web/config.js and build/create_public_view.sql, uploads the static
site, offers to activate the SES receipt rule set, and prints the stack
outputs and the remaining manual steps.

Usage:
    python scripts/deploy.py [--config PATH] [--dry-run] [--yes] [--skip-web] [--destroy]
"""
from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "deployment.config"
EXAMPLE_CONFIG_PATH = REPO_ROOT / "deployment.config.example"
TEMPLATE_PATH = REPO_ROOT / "template.yaml"
SAM_BUILD_DIR = REPO_ROOT / ".aws-sam" / "build"
WEB_DIR = REPO_ROOT / "web"
BUILD_DIR = REPO_ROOT / "build"

REQUIRED_KEYS = [
    "AWS_REGION", "STACK_NAME", "EMAIL_DOMAIN", "SES_SENDER", "SALES_TEAM_EMAIL",
    "BEDROCK_MODEL_ID", "DATA_API_CLUSTER_ARN", "DATA_API_SECRET_ARN", "DB_NAME", "DB_TABLE",
    "CURRENCY_CODE",
]

# Config key -> CloudFormation parameter name
PARAM_MAP = {
    "EMAIL_DOMAIN": "EmailDomain",
    "SES_SENDER": "SesSender",
    "SES_SENDER_NAME": "SesSenderName",
    "SES_CONFIGURATION_SET": "SesConfigurationSet",
    "SALES_TEAM_EMAIL": "SalesTeamEmail",
    "BEDROCK_MODEL_ID": "BedrockModelId",
    "DATA_API_CLUSTER_ARN": "DataApiClusterArn",
    "DATA_API_SECRET_ARN": "DataApiSecretArn",
    "DATA_API_ROLE_ARN": "DataApiRoleArn",
    "DATA_API_EXTERNAL_ID": "DataApiExternalId",
    "DB_NAME": "DbName",
    "DB_TABLE": "DbTable",
    "BRAND_NAME": "BrandName",
    "COMPANY_NAME": "CompanyName",
    "COMPANY_URL": "CompanyUrl",
    "CURRENCY_CODE": "CurrencyCode",
    "LANGUAGE_HINT": "LanguageHint",
    "REPLY_TECHNICAL_ERROR": "ReplyTechnicalError",
    "REPLY_RATE_LIMITED": "ReplyRateLimited",
    "REPLY_TOO_LONG": "ReplyTooLong",
    "AGENT_ENGINE": "AgentEngine",
    "ENABLE_WEB_CHANNEL": "EnableWebChannel",
    "WEB_DOMAIN": "WebDomain",
    "WEB_CERTIFICATE_ARN": "WebCertificateArn",
    "ROUTE53_HOSTED_ZONE_ID": "Route53HostedZoneId",
    "CREATE_SES_DOMAIN_IDENTITY": "CreateSesDomainIdentity",
    "WEB_REQUIRE_VERIFIED_RECIPIENT": "WebRequireVerifiedRecipient",
    "RATE_LIMIT_PER_HOUR": "RateLimitPerHour",
    "RATE_LIMIT_PER_DAY": "RateLimitPerDay",
    "MAX_MESSAGE_CHARS": "MaxMessageChars",
}
WEB_COPY_KEYS = ["WEB_HEADLINE", "WEB_HEADLINE_ACCENT", "WEB_INTRO", "WEB_SUBMIT_LABEL",
                 "WEB_EXAMPLE_QUESTION"]  # optional web page copy, rendered into web/config.js
SCRIPT_ONLY_KEYS = ["AWS_REGION", "AWS_PROFILE", "STACK_NAME", "SOURCE_TABLE",
                    "READONLY_DB_USER", "ACTIVATE_RECEIPT_RULE_SET"] + WEB_COPY_KEYS
KNOWN_KEYS = set(PARAM_MAP) | set(SCRIPT_ONLY_KEYS)
BOOL_KEYS = ["CREATE_SES_DOMAIN_IDENTITY", "ENABLE_WEB_CHANNEL", "WEB_REQUIRE_VERIFIED_RECIPIENT"]
INT_KEYS = ["RATE_LIMIT_PER_HOUR", "RATE_LIMIT_PER_DAY", "MAX_MESSAGE_CHARS"]

PLACEHOLDER_RE = re.compile(r"<[^>]+>")
REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")
STACK_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,19}$")
EMAIL_RE = re.compile(r"^[^@\s<>\"]+@([A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+)$")
DOMAIN_RE = re.compile(r"^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")
DB_USER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
CLUSTER_ARN_RE = re.compile(r"^arn:aws[a-z-]*:rds:([a-z0-9-]+):\d{12}:cluster:[A-Za-z0-9-]+$")
SECRET_ARN_RE = re.compile(r"^arn:aws[a-z-]*:secretsmanager:([a-z0-9-]+):\d{12}:secret:.+$")
ROLE_ARN_RE = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/.+$")
CERT_ARN_RE = re.compile(r"^arn:aws[a-z-]*:acm:([a-z0-9-]+):\d{12}:certificate/.+$")
ZONE_RE = re.compile(r"^Z[A-Z0-9]+$")


# --------------------------------------------------------------------- config
def parse_config(path: Path) -> dict[str, str]:
    """KEY=value per line. '#' starts a comment line. Matching outer quotes are removed."""
    config: dict[str, str] = {}
    if not path.exists():
        return config
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        config[key.strip()] = value
    return config


def validate(config: dict[str, str]) -> tuple[list[str], list[str]]:
    """Return (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []
    get = lambda k: config.get(k, "").strip()  # noqa: E731

    for key in REQUIRED_KEYS:
        if not get(key):
            errors.append(f"Missing required key: {key}")
    for key, val in config.items():
        if key not in KNOWN_KEYS:
            warnings.append(f"Unknown key ignored: {key} (typo?)")
        if PLACEHOLDER_RE.search(val):
            errors.append(f"{key} still contains a placeholder: {val}")
        if '"' in val or "\n" in val:
            errors.append(f"{key} must not contain double quotes or line breaks")

    region = get("AWS_REGION")
    if region and not REGION_RE.match(region):
        errors.append(f"AWS_REGION does not look like a region (for example us-east-1): {region}")
    if get("STACK_NAME") and not STACK_NAME_RE.match(get("STACK_NAME")):
        errors.append("STACK_NAME must start with a letter and use lowercase letters, digits and hyphens only (max 20).")

    domain = get("EMAIL_DOMAIN").lower()
    if domain and not DOMAIN_RE.match(domain):
        errors.append(f"EMAIL_DOMAIN is not a valid domain name: {domain}")
    sender = get("SES_SENDER")
    m = EMAIL_RE.match(sender) if sender else None
    if sender and not m:
        errors.append(f"SES_SENDER is not a valid email address: {sender}")
    elif m and domain and not (m.group(1).lower() == domain or m.group(1).lower().endswith("." + domain)):
        errors.append(f"SES_SENDER ({sender}) must be inside EMAIL_DOMAIN ({domain}).")
    if get("SALES_TEAM_EMAIL") and not EMAIL_RE.match(get("SALES_TEAM_EMAIL")):
        errors.append(f"SALES_TEAM_EMAIL is not a valid email address: {get('SALES_TEAM_EMAIL')}")

    model = get("BEDROCK_MODEL_ID")
    if model and re.search(r"\s", model):
        errors.append("BEDROCK_MODEL_ID must not contain spaces (use the model ID, inference-profile ID or ARN).")

    for key, rx, label in (("DATA_API_CLUSTER_ARN", CLUSTER_ARN_RE, "an Aurora cluster ARN"),
                           ("DATA_API_SECRET_ARN", SECRET_ARN_RE, "a Secrets Manager secret ARN"),
                           ("DATA_API_ROLE_ARN", ROLE_ARN_RE, "an IAM role ARN")):
        val = get(key)
        if not val:
            continue
        mm = rx.match(val)
        if not mm:
            errors.append(f"{key} is not {label}: {val}")
        elif mm.groups() and region and mm.group(1) != region:
            errors.append(f"{key} is in region {mm.group(1)} but AWS_REGION is {region}. "
                          "The Data API is called in the Lambda region, so they must match.")

    if get("DB_NAME") and not IDENT_RE.match(get("DB_NAME")):
        errors.append("DB_NAME may only contain letters, digits and underscores.")
    if get("DB_TABLE") and not IDENT_RE.match(get("DB_TABLE")):
        errors.append("DB_TABLE may only contain letters, digits and underscores.")
    if get("SOURCE_TABLE") and ("`" in get("SOURCE_TABLE") or "'" in get("SOURCE_TABLE")):
        errors.append("SOURCE_TABLE must not contain quotes or backticks.")
    if get("READONLY_DB_USER") and not DB_USER_RE.match(get("READONLY_DB_USER")):
        errors.append("READONLY_DB_USER may only contain letters, digits, '_', '.' and '-'.")

    for key in BOOL_KEYS:
        if get(key) and get(key).lower() not in ("true", "false"):
            errors.append(f"{key} must be true or false.")
    if get("ACTIVATE_RECEIPT_RULE_SET") and get("ACTIVATE_RECEIPT_RULE_SET").lower() not in ("true", "false"):
        errors.append("ACTIVATE_RECEIPT_RULE_SET must be true, false or blank.")
    if get("CURRENCY_CODE") and not re.match(r"^[A-Za-z]{3}$", get("CURRENCY_CODE")):
        errors.append("CURRENCY_CODE must be a 3-letter currency code (ISO 4217), for example USD.")
    for key in INT_KEYS:
        if get(key) and not (get(key).isdigit() and int(get(key)) > 0):
            errors.append(f"{key} must be a positive whole number.")
    if get("AGENT_ENGINE") and get("AGENT_ENGINE").lower() not in ("strands", "bedrock"):
        errors.append("AGENT_ENGINE must be 'strands' or 'bedrock'.")

    if get("ENABLE_WEB_CHANNEL").lower() != "false":
        wd, cert = get("WEB_DOMAIN"), get("WEB_CERTIFICATE_ARN")
        if wd and not DOMAIN_RE.match(wd):
            errors.append(f"WEB_DOMAIN is not a valid domain name: {wd}")
        if wd and not cert:
            errors.append("WEB_DOMAIN is set but WEB_CERTIFICATE_ARN is blank (CloudFront needs an ACM certificate).")
        if cert and not wd:
            warnings.append("WEB_CERTIFICATE_ARN is set but WEB_DOMAIN is blank; the certificate is ignored.")
        if cert:
            mm = CERT_ARN_RE.match(cert)
            if not mm:
                errors.append(f"WEB_CERTIFICATE_ARN is not an ACM certificate ARN: {cert}")
            elif mm.group(1) != "us-east-1":
                errors.append("WEB_CERTIFICATE_ARN must be in us-east-1 (a CloudFront requirement).")
    if get("ROUTE53_HOSTED_ZONE_ID") and not ZONE_RE.match(get("ROUTE53_HOSTED_ZONE_ID")):
        errors.append("ROUTE53_HOSTED_ZONE_ID should look like Z0123456789ABC (no /hostedzone/ prefix).")
    return errors, warnings


# ------------------------------------------------------------------ utilities
def _run(cmd: list[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    print("$ " + " ".join(cmd))
    return subprocess.run(cmd, check=check, text=True, capture_output=capture)


def _aws_cmd(region: str, profile: str) -> list[str]:
    cmd = ["aws"]
    if region:
        cmd += ["--region", region]
    if profile:
        cmd += ["--profile", profile]
    return cmd


def _sam_opts(region: str, profile: str) -> list[str]:
    """--region/--profile are options of the sam SUBCOMMAND, not of `sam` itself."""
    opts: list[str] = []
    if region:
        opts += ["--region", region]
    if profile:
        opts += ["--profile", profile]
    return opts


# ------------------------------------------------------------------ rendering
def render_web_config(config: dict[str, str]) -> None:
    brand = config.get("BRAND_NAME", "").strip() or "Property Assistant"
    company = config.get("COMPANY_NAME", "").strip()
    company_url = config.get("COMPANY_URL", "").strip()
    currency = config.get("CURRENCY_CODE", "").strip().upper()
    pick = lambda key, default: config.get(key, "").strip() or default  # noqa: E731
    esc = html.escape
    link = ""
    if company and company_url.lower().startswith(("http://", "https://")):
        link = f' by <a href="{esc(company_url, quote=True)}" target="_blank" rel="noopener">{esc(company)}</a>'
    payload = {
        "brandName": brand,
        "companyName": company,
        "companyUrl": company_url,
        "currencyCode": currency,
        "title": f"{brand} — Property Inquiry",
        "description": (f"Ask {brand} about properties. Describe what you're looking for "
                        "and our AI assistant emails you matching listings."),
        "headerTag": "Property Intelligence",
        "logoAlt": brand,
        "heroTitle": pick("WEB_HEADLINE", "Your property search,"),
        "heroAccent": pick("WEB_HEADLINE_ACCENT", "answered"),
        "heroText": pick("WEB_INTRO", "Describe what you're after — area, budget, bedrooms, sale or rent — "
                         f"and {brand} emails you real, matching listings in minutes."),
        "exampleQuestion": pick("WEB_EXAMPLE_QUESTION", ""),
        "formTitle": f"Ask {brand}",
        "submitLabel": pick("WEB_SUBMIT_LABEL", "Get matching properties"),
        "unverifiedMessage": ("This is a preview, so replies can only be sent to pre-approved email "
                              "addresses. Please contact the team to have your address added."),
        "footerHtml": f"<strong>{esc(brand)}</strong> — an AI solution{link}. Built on AWS.",
    }
    out = WEB_DIR / "config.js"
    out.write_text("// Generated by deploy.py — do not edit.\nwindow.ESTATEEDGE_CONFIG = "
                   + json.dumps(payload, ensure_ascii=False, indent=2) + ";\n", encoding="utf-8")
    print(f"Rendered {out.relative_to(REPO_ROOT)}")


def render_sql(config: dict[str, str]) -> None:
    tpl = REPO_ROOT / "db" / "create_public_view.sql.tpl"
    if not tpl.exists():
        print(f"SQL template missing: {tpl.relative_to(REPO_ROOT)} - skipping")
        return
    subs = {
        "{{DB_NAME}}": config.get("DB_NAME", "").strip(),
        "{{SOURCE_TABLE}}": config.get("SOURCE_TABLE", "").strip(),
        "{{VIEW_NAME}}": config.get("DB_TABLE", "").strip(),
        "{{READONLY_DB_USER}}": config.get("READONLY_DB_USER", "").strip(),
    }
    missing = [k.strip("{}") for k, v in subs.items() if not v]
    if missing:
        print(f"Skipped build/create_public_view.sql: set {', '.join(missing)} in the config to render it.")
        return
    BUILD_DIR.mkdir(exist_ok=True)
    text = tpl.read_text(encoding="utf-8")
    for key, value in subs.items():
        text = text.replace(key, value)
    out = BUILD_DIR / "create_public_view.sql"
    out.write_text(text, encoding="utf-8")
    print(f"Rendered {out.relative_to(REPO_ROOT)}")


# ---------------------------------------------------------------- aws helpers
def sts_identity(region: str, profile: str) -> dict:
    res = _run(_aws_cmd(region, profile) + ["sts", "get-caller-identity", "--output", "json"], capture=True)
    return json.loads(res.stdout)


def cfn_outputs(region: str, profile: str, stack: str) -> dict:
    cmd = _aws_cmd(region, profile) + ["cloudformation", "describe-stacks", "--stack-name", stack,
                                       "--query", "Stacks[0].Outputs", "--output", "json"]
    res = _run(cmd, capture=True)
    items = json.loads(res.stdout or "[]") or []
    return {item["OutputKey"]: item["OutputValue"] for item in items}


def empty_bucket(bucket: str, region: str, profile: str) -> None:
    _run(_aws_cmd(region, profile) + ["s3", "rm", f"s3://{bucket}", "--recursive"], check=False)


def upload_web(region: str, profile: str, bucket: str) -> None:
    for name in ("index.html", "config.js", "logo.png", "favicon.ico", "favicon-32.png", "apple-touch-icon.png"):
        path = WEB_DIR / name
        if not path.exists():
            print(f"  (skip) web/{name} not found")
            continue
        cache = "no-cache" if name in ("config.js", "index.html") else "public, max-age=3600"
        _run(_aws_cmd(region, profile) + ["s3", "cp", str(path), f"s3://{bucket}/{name}", "--cache-control", cache])


def invalidate_cloudfront(profile: str, dist_id: str) -> None:
    cmd = ["aws"] + (["--profile", profile] if profile else [])  # CloudFront is global: no --region
    _run(cmd + ["cloudfront", "create-invalidation", "--distribution-id", dist_id, "--paths", "/*"], check=False)


def active_rule_set(region: str, profile: str) -> str:
    cmd = _aws_cmd(region, profile) + ["ses", "describe-active-receipt-rule-set",
                                       "--query", "Metadata.Name", "--output", "text"]
    res = _run(cmd, check=False, capture=True)
    name = (res.stdout or "").strip()
    return "" if name in ("None", "null") else name


# --------------------------------------------------------------------- phases
def sam_build(region: str, profile: str) -> None:
    _run(["sam", "build", "--template-file", str(TEMPLATE_PATH), "--build-dir", str(SAM_BUILD_DIR)]
         + _sam_opts(region, profile))


def sam_deploy(config: dict[str, str], region: str, profile: str, stack: str) -> None:
    overrides = []
    for key, param in PARAM_MAP.items():
        value = config.get(key, "").strip()
        if not value:
            continue
        if key in BOOL_KEYS or key == "AGENT_ENGINE":
            value = value.lower()
        if key == "CURRENCY_CODE":
            value = value.upper()
        # Values are double-quoted: SAM silently truncates unquoted values at the first space.
        overrides.append(f'{param}="{value}"')
    cmd = ["sam", "deploy"] + _sam_opts(region, profile) + [
        "--template-file", str(SAM_BUILD_DIR / "template.yaml"),
        "--stack-name", stack,
        "--capabilities", "CAPABILITY_IAM", "CAPABILITY_AUTO_EXPAND",
        "--resolve-s3", "--no-confirm-changeset", "--no-fail-on-empty-changeset",
        "--parameter-overrides", *overrides,
    ]
    _run(cmd)


def maybe_activate_ses(region: str, profile: str, rule_set: str, config: dict[str, str], assume_yes: bool) -> None:
    want = config.get("ACTIVATE_RECEIPT_RULE_SET", "").strip().lower()
    if want == "false":
        return
    current = active_rule_set(region, profile)
    if current == rule_set:
        print(f"SES receipt rule set '{rule_set}' is already active.")
        return
    if want == "true":
        confirm = True
    elif assume_yes:
        confirm = False
    else:
        print("\nSES receipt rule set")
        print("  Activating a receipt rule set REPLACES the currently active rule set in this region.")
        print(f"  Currently active: {current or 'none'}")
        print("  If you already receive mail with SES, merge the rules by hand first.")
        confirm = input(f"Activate '{rule_set}' now? [y/N] ").strip().lower() == "y"
    if not confirm:
        print("Skipped. Activate later with:")
        print(f"  aws ses set-active-receipt-rule-set --rule-set-name {rule_set} --region {region}")
        return
    _run(_aws_cmd(region, profile) + ["ses", "set-active-receipt-rule-set", "--rule-set-name", rule_set])
    print(f"Activated receipt rule set {rule_set}")


def print_outputs_and_next_steps(outputs: dict, config: dict[str, str], region: str, stack: str) -> None:
    print("\n" + "=" * 70)
    print(f"Stack {stack} deployed in {region}")
    print("=" * 70)
    labels = {
        "WebUrl": "Web URL", "WebApiUrl": "Web API (HTTP API behind /api)",
        "InboundQueueUrl": "Inbound SQS queue URL", "InboundBucketName": "Inbound S3 bucket",
        "WebBucketName": "Web S3 bucket", "ReceiptRuleSetName": "SES receipt rule set",
        "IngestRoleArn": "Ingest role ARN", "WorkerRoleArn": "Worker role ARN",
        "WebInquiryRoleArn": "Web role ARN",
    }
    for key, label in labels.items():
        if key in outputs:
            print(f"  {label}: {outputs[key]}")

    zone_set = bool(config.get("ROUTE53_HOSTED_ZONE_ID", "").strip())
    print("\nDNS records you may need to add manually:")
    if zone_set:
        print("  (ROUTE53_HOSTED_ZONE_ID is set: DKIM, MX and the web alias were created for you.)")
    else:
        for i in (1, 2, 3):
            name, value = outputs.get(f"DkimRecord{i}Name", ""), outputs.get(f"DkimRecord{i}Value", "")
            if name and value and "already exists" not in name:
                print(f"  CNAME  {name}  ->  {value}")
        mx = outputs.get("MxRecordTarget", "")
        if mx:
            print(f"  MX     {config.get('EMAIL_DOMAIN', '')}  ->  {mx}")
        if config.get("WEB_DOMAIN", "").strip() and outputs.get("WebUrl"):
            print(f"  Web address {config['WEB_DOMAIN']} -> alias/CNAME to your CloudFront distribution "
                  "(see the CloudFront console for its domain name)")

    print("\nRemaining manual steps:")
    print("  1. Add the DNS records above (skip if ROUTE53_HOSTED_ZONE_ID is set).")
    print("  2. Verify SALES_TEAM_EMAIL and any test addresses in SES (sandbox only).")
    print("  3. Run build/create_public_view.sql in Aurora as the master user (if it was rendered).")
    if config.get("WEB_REQUIRE_VERIFIED_RECIPIENT", "true").lower() == "true":
        print("  4. After SES production access, set WEB_REQUIRE_VERIFIED_RECIPIENT=false and redeploy.")
    if config.get("DATA_API_ROLE_ARN", "").strip():
        print("  5. Cross-account: add the Worker and Web role ARNs above to the trust policy of your "
              "Aurora role (see README).")
    print("  6. Send a test email and submit the web form to verify both channels.")


def do_destroy(config: dict[str, str], args, region: str, profile: str, stack: str) -> None:
    print(f"Destroy stack {stack} in {region}.")
    print("S3 buckets will be emptied. Aurora, DNS records and SES identities are not touched.")
    if not args.yes:
        if input(f"Type the stack name ({stack}) to confirm: ").strip() != stack:
            print("Aborted.")
            sys.exit(0)
    try:
        outputs = cfn_outputs(region, profile, stack)
    except subprocess.CalledProcessError:
        print("Stack not found (or you are not authorised to describe it). Nothing to do.")
        return
    # CloudFormation cannot delete an ACTIVE receipt rule set, so deactivate ours first.
    rule_set = outputs.get("ReceiptRuleSetName", "")
    if rule_set and active_rule_set(region, profile) == rule_set:
        print(f"The receipt rule set '{rule_set}' is active and blocks stack deletion.")
        if args.yes or input("Deactivate it now (inbound email stops)? [y/N] ").strip().lower() == "y":
            _run(_aws_cmd(region, profile) + ["ses", "set-active-receipt-rule-set"])
        else:
            print("Aborted: deactivate the rule set, then run --destroy again.")
            sys.exit(1)
    for key in ("InboundBucketName", "WebBucketName"):
        bucket = outputs.get(key, "")
        if bucket and bucket != "web channel disabled":
            print(f"Emptying s3://{bucket}/")
            empty_bucket(bucket, region, profile)
    _run(["sam", "delete"] + _sam_opts(region, profile) + ["--stack-name", stack, "--no-prompts"])
    print("Stack deleted.")


# ------------------------------------------------------------------------ main
def main() -> None:
    parser = argparse.ArgumentParser(description="Deploy EstateEdge.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--dry-run", action="store_true", help="validate and render files only")
    parser.add_argument("--yes", action="store_true", help="skip confirmation prompts")
    parser.add_argument("--skip-web", action="store_true", help="do not upload the web page")
    parser.add_argument("--destroy", action="store_true", help="delete the stack")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(f"Config file not found: {config_path}")
        print(f"Copy {EXAMPLE_CONFIG_PATH.name} to {DEFAULT_CONFIG_PATH.name} and fill it in.")
        sys.exit(1)

    config = parse_config(config_path)
    errors, warnings = validate(config)
    for w in warnings:
        print(f"Warning: {w}")
    if errors:
        print("Configuration errors (fill in the placeholders and fix these):")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)

    region = config["AWS_REGION"].strip()
    profile = config.get("AWS_PROFILE", "").strip()
    stack = config["STACK_NAME"].strip()

    if not args.destroy:
        render_web_config(config)
        render_sql(config)
        if args.dry_run:
            print("\nDry run complete. Config is valid. No AWS changes were made.")
            print("Next: remove --dry-run to run sam build, sam deploy and the post-deploy steps.")
            return

    for tool, hint in (("sam", "Install the AWS SAM CLI."), ("aws", "Install AWS CLI v2.")):
        if not shutil.which(tool):
            print(f"{tool} not found. {hint}")
            sys.exit(1)

    if args.destroy:
        do_destroy(config, args, region, profile, stack)
        return

    try:
        identity = sts_identity(region, profile)
    except (subprocess.CalledProcessError, ValueError):
        print("Could not call AWS. Check your credentials / profile / region.")
        sys.exit(1)
    print(f"\nAccount: {identity.get('Account')}  ({identity.get('Arn')})")
    print(f"Region:  {region}")
    print(f"Stack:   {stack}")
    if not args.yes and input("Deploy? [y/N] ").strip().lower() != "y":
        print("Aborted.")
        sys.exit(0)

    sam_build(region, profile)
    sam_deploy(config, region, profile, stack)
    outputs = cfn_outputs(region, profile, stack)

    if not args.skip_web and config.get("ENABLE_WEB_CHANNEL", "true").lower() != "false":
        bucket = outputs.get("WebBucketName", "")
        if bucket and bucket != "web channel disabled":
            print(f"\nUploading web/ to s3://{bucket}/")
            upload_web(region, profile, bucket)
            dist = outputs.get("WebDistributionId", "")
            if dist and dist != "web channel disabled":
                print(f"Invalidating CloudFront distribution {dist}")
                invalidate_cloudfront(profile, dist)

    rule_set = outputs.get("ReceiptRuleSetName", "")
    if rule_set:
        maybe_activate_ses(region, profile, rule_set, config, args.yes)
    print_outputs_and_next_steps(outputs, config, region, stack)


if __name__ == "__main__":
    main()
