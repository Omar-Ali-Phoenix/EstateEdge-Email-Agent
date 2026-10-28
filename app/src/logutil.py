"""Small helpers that keep customer data out of the logs."""
from email.utils import parseaddr

def mask_email(address) -> str:
    """'john.smith@example.com' -> 'j***@example.com'.

    Keeps the first character of the local part and the domain (enough to correlate one
    conversation and spot a bad domain, not enough to identify a person). Accepts a display-name
    form ('John <john@example.com>'). Anything that is not an email address becomes '***'.
    """
    addr = parseaddr(str(address or ""))[1]
    local, sep, domain = addr.rpartition("@")
    if not sep or not local or not domain:
        return "***"
    return f"{local[0]}***@{domain.lower()}"
