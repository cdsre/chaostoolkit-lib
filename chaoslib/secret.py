# -*- coding: utf-8 -*-
import os
import requests
from typing import Any, Dict

from logzero import logger
try:
    import hvac
    HAS_HVAC = True
except ImportError:
    HAS_HVAC = False

from chaoslib.exceptions import InvalidExperiment
from chaoslib.types import Configuration, Secrets

__all__ = ["load_secrets", "create_vault_client"]


def load_secrets(secrets_info: Dict[str, Dict[str, str]],
                 configuration: Configuration = None) -> Secrets:
    """
    Takes the the secrets definition from an experiment and tries to load
    the secrets whenever they relate to external sources such as environmental
    variables (or in the future from vault secrets).

    Here is an example of what it looks like:

    ```
    {
        "target_1": {
            "mysecret_1": "some value"
        },
        "target_2": {
            "mysecret_2": {
                "type": "env",
                "key": "SOME_ENV_VAR"
            }
        },
        "target_3": {
            "mysecret_3": {
                "type": "vault",
                "key": "secrets/some/key"
            }
        }
    }
    ```

    Loading this secrets definition will generate the following:

    ```
    {
        "target_1": {
            "mysecret_1": "some value"
        },
        "target_2": {
            "mysecret_2": "some other value"
        },
        "target_3": {
            "mysecret_3": "some alternate value"
        }
    }
    ```

    You can refer to those from your experiments:

    ```
    {
        "type": "probe",
        "provider": {
            "secret": ["target_1", "target_2"]
        }
    }
    ```
    """
    logger.debug("Loading secrets...")

    loaders = (
        load_inline_secrets,
        load_secrets_from_env,
        load_secrets_from_vault,
    )

    secrets = {}
    for loader in loaders:
        for key, value in loader(secrets_info, configuration).items():
            if key not in secrets:
                secrets[key] = {}
            secrets[key].update(value)

    logger.debug("Secrets loaded")

    return secrets


def load_inline_secrets(secrets_info: Dict[str, Dict[str, str]],
                        configuration: Configuration = None) -> Secrets:
    """
    Load secrets that are inlined in the experiments.
    """
    secrets = {}

    for (target, keys) in secrets_info.items():
        secrets[target] = {}

        for (key, value) in keys.items():
            if not isinstance(value, dict):
                secrets[target][key] = value
            elif value.get("type") not in ("env", "vault"):
                secrets[target][key] = value

        if not secrets[target]:
            secrets.pop(target)

    return secrets


def load_secrets_from_env(secrets_info: Dict[str, Dict[str, str]],
                          configuration: Configuration = None) -> Secrets:
    env = os.environ
    secrets = {}

    for (target, keys) in secrets_info.items():
        secrets[target] = {}

        for (key, value) in keys.items():
            if isinstance(value, dict) and value.get("type") == "env":
                env_key = value["key"]
                if env_key not in env:
                    raise InvalidExperiment(
                        "Secrets make reference to an environment key "
                        "that does not exist: {}".format(env_key))
                secrets[target][key] = env.get(env_key)

        if not secrets[target]:
            secrets.pop(target)

    return secrets


def load_secrets_from_vault(secrets_info: Dict[str, Dict[str, str]],
                            configuration: Configuration = None) -> Secrets:
    """
    Load secrets from Vault KV secrets store

    In your experiment:

    ```
    {
        "k8s": {
            "mykey": {
                "type": "vault",
                "path": "foo/bar"
            }
        }
    }
    ```

    This will read the Vault secret at path `secret/foo/bar`
    (or `secret/data/foo/bar` if you use Vault KV version 2) and store its
    entirely payload into Chaos Toolkit `mykey`. This means, that all kays
    under that path will be available as-is. For instance, this could be:

    ```
    {
        "mypassword": "shhh",
        "mylogin": "jane
    }
    ```

    You may be more specific as follows:

    ```
    {
        "k8s": {
            "mykey": {
                "type": "vault",
                "path": "foo/bar",
                "key": "mypassword"
            }
        }
    }
    ```

    In that case, `mykey` will be set to the value at `secret/foo/bar` under
    the Vault secret key `mypassword`.
    """
    secrets = {}

    client = create_vault_client(configuration)

    for (target, keys) in secrets_info.items():
        secrets[target] = {}

        for (key, value) in keys.items():
            if isinstance(value, dict) and value.get("type") == "vault":
                if not HAS_HVAC:
                    logger.error(
                        "Install the `hvac` package to fetch secrets "
                        "from Vault: `pip install chaostoolkit-lib[vault]`.")
                    return {}

                path = value.get("path")
                if path is None:
                    logger.warning(
                        "Missing Vault secret path for '{}'".format(key))
                    continue

                # see https://github.com/chaostoolkit/chaostoolkit/issues/98
                kv = client.secrets.kv
                is_kv1 = kv.default_kv_version == "1"
                if is_kv1:
                    vault_payload = kv.v1.read_secret(path=path)
                else:
                    vault_payload = kv.v2.read_secret_version(path=path)

                if not vault_payload:
                    logger.warning(
                        "No Vault secret found at path: {}".format(path))
                    continue

                if is_kv1:
                    data = vault_payload.get("data")
                else:
                    data = vault_payload.get("data", {}).get("data")

                if "key" in value:
                    vault_key = value["key"]
                    if vault_key not in data:
                        logger.warning(
                            "No Vault key '{}' at secret path '{}'".format(
                                vault_key, path))
                        continue

                    secrets[target][key] = data.get(vault_key)
                else:
                    secrets[target][key] = data

        if not secrets[target]:
            secrets.pop(target)

    return secrets


###############################################################################
# Internals
###############################################################################
def create_vault_client(configuration: Configuration = None):
    """
    Initialize a Vault client from either a token or an approle.
    """
    client = None
    if HAS_HVAC:
        url = configuration.get("vault_addr")
        client = hvac.Client(url=url)

        client.secrets.kv.default_kv_version = str(configuration.get(
            "vault_kv_version", "2"))
        logger.debug(
            "Using Vault secrets KV version {}".format(
                client.secrets.kv.default_kv_version))

        if "vault_token" in configuration:
            client.token = configuration.get("vault_token")
        elif "vault_role_id" in configuration and \
             "vault_role_secret" in configuration:
            role_id = configuration.get("vault_role_id")
            role_secret = configuration.get("vault_role_secret")

            try:
                app_role = client.auth_approle(role_id, role_secret)
            except Exception as ve:
                raise InvalidExperiment(
                    "Failed to connect to Vault with the AppRole: {}".format(
                        str(ve)))

            client.token = app_role['auth']['client_token']
    return client
