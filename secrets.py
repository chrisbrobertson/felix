"""Secret retrieval from macOS Keychain with env-var fallback."""
import getpass
import logging
import subprocess
from typing import Optional

log = logging.getLogger("secrets")
_cache: dict = {}


def get_secret(name: str) -> Optional[str]:
    """Fetch secret 'name' from macOS login Keychain.

    Looks for a generic-password item with service `secondbrain-{name}`
    and account = current user. Returns None if not found.
    Result cached in-process.
    """
    if name in _cache:
        return _cache[name]
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", getpass.getuser(),
             "-s", f"secondbrain-{name}", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            secret = result.stdout.strip()
            _cache[name] = secret
            return secret
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        log.debug("Keychain lookup for %s failed: %s", name, e)
    return None


def get_secret_or_env(name: str, env_var: str) -> Optional[str]:
    """Keychain first, env var fallback.

    If keychain misses, logs a one-time info reminding the user to migrate.
    """
    secret = get_secret(name)
    if secret is not None:
        return secret
    import os
    env_val = os.environ.get(env_var)
    if env_val:
        if name not in _cache:
            log.info("Secret %s loaded from env; run install.sh to migrate to Keychain", name)
            _cache[name] = env_val
        return env_val
    return None
