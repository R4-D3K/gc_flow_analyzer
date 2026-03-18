"""Encrypted org profile loader."""
import os
import yaml
import logging
from typing import Dict, List, Optional
from cryptography.fernet import Fernet, InvalidToken
from app import config

logger = logging.getLogger(__name__)

_orgs_cache: List[Dict] = []

# All 16 primary Genesys Cloud environments
GC_ENVIRONMENTS = [
    {"name": "Americas (US East)",            "domain": "mypurecloud.com"},
    {"name": "Americas (US West)",            "domain": "usw2.pure.cloud"},
    {"name": "Americas (Canada)",             "domain": "cac1.pure.cloud"},
    {"name": "Americas (Mexico)",             "domain": "mxc1.pure.cloud"},
    {"name": "South America (São Paulo)",     "domain": "sae1.pure.cloud"},
    {"name": "Americas (US East 2, FedRAMP)", "domain": "use2.us-gov-pure.cloud"},
    {"name": "EMEA (Dublin)",                 "domain": "mypurecloud.ie"},
    {"name": "EMEA (Frankfurt)",              "domain": "mypurecloud.de"},
    {"name": "EMEA (London)",                 "domain": "euw2.pure.cloud"},
    {"name": "EMEA (Zurich)",                 "domain": "euc2.pure.cloud"},
    {"name": "Asia Pacific (Tokyo)",          "domain": "mypurecloud.jp"},
    {"name": "Asia Pacific (Sydney)",         "domain": "mypurecloud.com.au"},
    {"name": "Asia Pacific (Seoul)",          "domain": "apne2.pure.cloud"},
    {"name": "Asia Pacific (Osaka)",          "domain": "apne3.pure.cloud"},
    {"name": "Asia Pacific (Mumbai)",         "domain": "aps1.pure.cloud"},
    {"name": "Middle East (UAE)",             "domain": "mec1.pure.cloud"},
]


def _fernet() -> Fernet:
    if not config.FC_ENCRYPTION_KEY:
        raise EnvironmentError("FC_ENCRYPTION_KEY is not set")
    return Fernet(config.FC_ENCRYPTION_KEY.encode())


def load_orgs() -> List[Dict]:
    """Load and decrypt org profiles from YAML. Caches in memory."""
    global _orgs_cache
    if not os.path.exists(config.ORGS_FILE):
        logger.warning("Orgs file not found: %s", config.ORGS_FILE)
        _orgs_cache = []
        return []

    f = _fernet()
    with open(config.ORGS_FILE, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}

    result = []
    for org in data.get("orgs", []):
        try:
            result.append({
                "name":          org["name"],
                "environment":   org["environment"],
                "client_id":     f.decrypt(org["client_id"].encode()).decode(),
                "client_secret": f.decrypt(org["client_secret"].encode()).decode(),
            })
        except (InvalidToken, KeyError) as e:
            logger.error("Failed to decrypt org '%s': %s", org.get("name", "?"), e)

    _orgs_cache = result
    logger.info("Loaded %d org profile(s)", len(result))
    return result


def get_orgs() -> List[Dict]:
    return _orgs_cache


def get_org(name: str) -> Optional[Dict]:
    return next((o for o in _orgs_cache if o["name"] == name), None)
