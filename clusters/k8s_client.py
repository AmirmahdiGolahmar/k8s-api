import os

import yaml
from kubernetes import client, config


def _load_config_dict(cluster=None):
    """Return the parsed kubeconfig dict for a Cluster, or the local default."""
    if cluster is not None and cluster.kubeconfig:
        return yaml.safe_load(cluster.kubeconfig)
    default_path = os.path.expanduser(os.environ.get('KUBECONFIG', '~/.kube/config'))
    with open(default_path) as f:
        return yaml.safe_load(f)


def _build_configuration(cluster=None):
    """Build a client.Configuration for the given Cluster record.

    If the cluster has no stored kubeconfig (or no cluster is given), fall
    back to the local default kubeconfig / in-cluster config.
    """
    configuration = client.Configuration()
    if cluster is not None and cluster.kubeconfig:
        config.load_kube_config_from_dict(_load_config_dict(cluster), client_configuration=configuration)
    else:
        config.load_kube_config(client_configuration=configuration)
    # Configuration.retries defaults to None, which leaves urllib3 to fall
    # back to its own default of 3 retries -- each one getting its own fresh
    # _request_timeout window rather than sharing one budget, so a call can
    # take several times longer than its stated timeout before finally
    # failing. Disable retries so a passed _request_timeout is the real bound.
    configuration.retries = 0
    return configuration


def get_core_v1_client(cluster=None):
    """Build a CoreV1Api client for the given Cluster record."""
    return client.CoreV1Api(client.ApiClient(_build_configuration(cluster)))


def get_apps_v1_client(cluster=None):
    """Build an AppsV1Api client for the given Cluster record."""
    return client.AppsV1Api(client.ApiClient(_build_configuration(cluster)))


def resolve_api_server(cluster=None):
    """Extract the API server address (e.g. https://1.2.3.4:6443) that
    get_core_v1_client(cluster) would actually connect to.
    """
    try:
        config_dict = _load_config_dict(cluster)
        current_context_name = config_dict['current-context']
        context = next(c['context'] for c in config_dict['contexts'] if c['name'] == current_context_name)
        cluster_cfg = next(c['cluster'] for c in config_dict['clusters'] if c['name'] == context['cluster'])
        return cluster_cfg['server']
    except (KeyError, StopIteration, FileNotFoundError, yaml.YAMLError):
        return ''
