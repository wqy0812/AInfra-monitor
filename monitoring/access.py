"""Validate the transport-peer IP allowlist before accepting API traffic."""
import ipaddress


def client_allowlist(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('ALLOWED_CLIENTS must contain explicit client IP addresses')
    addresses = []
    for item in value.split(','):
        address = str(ipaddress.ip_address(item.strip()))
        if address not in addresses:
            addresses.append(address)
    return addresses
