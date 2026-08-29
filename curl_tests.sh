#!/usr/bin/env bash
# Ad-hoc curl commands for exercising the k8s-api endpoints by hand.
#
# Usage:
#   ./curl_tests.sh <command> [args...]
#
# Set BASE_URL to point elsewhere (default: http://127.0.0.1:8000).
#
# Commands:
#   list-clusters
#   get-cluster        <cluster_id>
#   create-cluster      <name> [is_default(true/false)] [kubeconfig_file]
#   update-cluster      <cluster_id> <json_body>
#   delete-cluster      <cluster_id>
#
#   list-namespaces     <cluster_id>
#   create-namespace    <cluster_id> <name>
#   get-namespace        <name> [cluster_id]
#   patch-namespace     <name> <json_body> [cluster_id]
#   delete-namespace    <db_id>
#
#   list-apps            <cluster_id> [namespace]
#   create-app           <cluster_id> <namespace> <name> [image] [replicas]
#   delete-app           <db_id>
#
#   list-backups
#   create-backup
#   get-backup           <backup_id>
#
# Examples:
#   ./curl_tests.sh list-clusters
#   ./curl_tests.sh create-cluster my-cluster true
#   ./curl_tests.sh list-namespaces 8
#   ./curl_tests.sh create-namespace 8 demo-ns
#   ./curl_tests.sh get-namespace demo-ns 8
#   ./curl_tests.sh patch-namespace demo-ns '{"labels":{"team":"platform"}}' 8
#   ./curl_tests.sh delete-namespace 3
#   ./curl_tests.sh list-apps 8
#   ./curl_tests.sh create-app 8 demo-ns my-app
#   ./curl_tests.sh create-app 8 demo-ns my-app nginx:1.27 2
#   ./curl_tests.sh delete-app 4
#   ./curl_tests.sh create-backup
#   ./curl_tests.sh get-backup 1

set -euo pipefail

BASE_URL="${BASE_URL:-http://127.0.0.1:8000}"

# Pretty-print JSON responses when jq is available, otherwise print raw.
show() {
    if command -v jq >/dev/null 2>&1; then
        jq . 2>/dev/null || cat
    else
        cat
    fi
    echo
}

cmd="${1:-}"
shift || true

case "$cmd" in
    list-clusters)
        curl -s "$BASE_URL/cluster/" | show
        ;;

    get-cluster)
        id="${1:?usage: get-cluster <cluster_id>}"
        curl -s "$BASE_URL/cluster/$id/" | show
        ;;

    create-cluster)
        name="${1:?usage: create-cluster <name> [is_default] [kubeconfig_file]}"
        is_default="${2:-false}"
        kubeconfig_file="${3:-}"

        body="$(jq -n --arg name "$name" --argjson is_default "$is_default" \
            '{name: $name, is_default: $is_default}')"

        if [[ -n "$kubeconfig_file" ]]; then
            body="$(jq -n --arg name "$name" --argjson is_default "$is_default" \
                --rawfile kubeconfig "$kubeconfig_file" \
                '{name: $name, is_default: $is_default, kubeconfig: $kubeconfig}')"
        fi

        curl -s -X POST "$BASE_URL/cluster/" \
            -H 'Content-Type: application/json' \
            -d "$body" | show
        ;;

    update-cluster)
        id="${1:?usage: update-cluster <cluster_id> <json_body>}"
        json="${2:?usage: update-cluster <cluster_id> <json_body>}"
        curl -s -X PATCH "$BASE_URL/cluster/$id/" \
            -H 'Content-Type: application/json' \
            -d "$json" | show
        ;;

    delete-cluster)
        id="${1:?usage: delete-cluster <cluster_id>}"
        curl -s -o /dev/null -w '%{http_code}\n' -X DELETE "$BASE_URL/cluster/$id/"
        ;;

    list-namespaces)
        cluster_id="${1:?usage: list-namespaces <cluster_id>}"
        curl -s "$BASE_URL/namespace/?cluster_id=$cluster_id" | show
        ;;

    create-namespace)
        cluster_id="${1:?usage: create-namespace <cluster_id> <name>}"
        name="${2:?usage: create-namespace <cluster_id> <name>}"
        curl -s -X POST "$BASE_URL/namespace/" \
            -H 'Content-Type: application/json' \
            -d "$(jq -n --argjson cluster_id "$cluster_id" --arg name "$name" \
                '{cluster_id: $cluster_id, name: $name}')" | show
        ;;

    get-namespace)
        name="${1:?usage: get-namespace <name> [cluster_id]}"
        cluster_id="${2:-}"
        url="$BASE_URL/namespace/$name/"
        [[ -n "$cluster_id" ]] && url="$url?cluster=$cluster_id"
        curl -s "$url" | show
        ;;

    patch-namespace)
        name="${1:?usage: patch-namespace <name> <json_body> [cluster_id]}"
        json="${2:?usage: patch-namespace <name> <json_body> [cluster_id]}"
        cluster_id="${3:-}"
        url="$BASE_URL/namespace/$name/"
        [[ -n "$cluster_id" ]] && url="$url?cluster=$cluster_id"
        curl -s -X PATCH "$url" \
            -H 'Content-Type: application/json' \
            -d "$json" | show
        ;;

    delete-namespace)
        db_id="${1:?usage: delete-namespace <db_id>}"
        curl -s -o /dev/null -w '%{http_code}\n' -X DELETE "$BASE_URL/namespace/$db_id/"
        ;;

    list-apps)
        cluster_id="${1:?usage: list-apps <cluster_id> [namespace]}"
        namespace="${2:-}"
        url="$BASE_URL/app/?cluster_id=$cluster_id"
        [[ -n "$namespace" ]] && url="$url&namespace=$namespace"
        curl -s "$url" | show
        ;;

    create-app)
        cluster_id="${1:?usage: create-app <cluster_id> <namespace> <name> [image] [replicas]}"
        namespace="${2:?usage: create-app <cluster_id> <namespace> <name> [image] [replicas]}"
        name="${3:?usage: create-app <cluster_id> <namespace> <name> [image] [replicas]}"
        image="${4:-}"
        replicas="${5:-}"

        body="$(jq -n --argjson cluster_id "$cluster_id" --arg namespace "$namespace" --arg name "$name" \
            '{cluster_id: $cluster_id, namespace: $namespace, name: $name}')"
        [[ -n "$image" ]] && body="$(jq --arg image "$image" '. + {image: $image}' <<<"$body")"
        [[ -n "$replicas" ]] && body="$(jq --argjson replicas "$replicas" '. + {replicas: $replicas}' <<<"$body")"

        curl -s -X POST "$BASE_URL/app/" \
            -H 'Content-Type: application/json' \
            -d "$body" | show
        ;;

    delete-app)
        db_id="${1:?usage: delete-app <db_id>}"
        curl -s -o /dev/null -w '%{http_code}\n' -X DELETE "$BASE_URL/app/$db_id/"
        ;;

    list-backups)
        curl -s "$BASE_URL/backups/" | show
        ;;

    create-backup)
        curl -s -X POST "$BASE_URL/backups/create/" | show
        ;;

    get-backup)
        id="${1:?usage: get-backup <backup_id>}"
        curl -s "$BASE_URL/backups/$id/" | show
        ;;

    *)
        echo "Usage: $0 <command> [args...]"
        echo
        echo "Commands: list-clusters, get-cluster, create-cluster, update-cluster, delete-cluster,"
        echo "          list-namespaces, create-namespace, get-namespace, patch-namespace, delete-namespace,"
        echo "          list-apps, create-app, delete-app,"
        echo "          list-backups, create-backup, get-backup"
        echo
        echo "Run '$0' with no args to see this message. See the top of the script for full usage/examples."
        exit 1
        ;;
esac
