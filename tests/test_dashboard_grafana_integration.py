"""Opt-in validation of the provisioned Grafana plugins and read-only SQL role."""

import base64
import json
import os
from pathlib import Path
import urllib.error
import urllib.request
from uuid import uuid4

import pytest


@pytest.mark.skipif(not os.getenv("MOCKX_GRAFANA_TEST_URL"), reason="isolated Grafana URL required")
def test_provisioned_grafana_plugins_and_reader_cannot_write():
    base = os.environ["MOCKX_GRAFANA_TEST_URL"].rstrip("/")
    user = os.getenv("MOCKX_GRAFANA_TEST_USER", "admin")
    password = os.getenv("MOCKX_GRAFANA_TEST_PASSWORD", "mockx-dashboard")
    authorization = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def api(path, body=None, *, anonymous=False):
        headers = {"Content-Type": "application/json"}
        if not anonymous:
            headers["Authorization"] = authorization
        request = urllib.request.Request(base+path, headers=headers,
            data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    status, plugins = api("/api/plugins")
    assert status == 200
    versions = {plugin["id"]: plugin["info"]["version"] for plugin in plugins}
    assert versions["questdb-questdb-datasource"] == "0.1.8"
    assert versions["nline-plotlyjs-panel"] == "1.8.1"
    assert api("/api/datasources/uid/questdb-native/health")[0] == 200
    status, dashboard = api("/api/dashboards/uid/mockx-native", anonymous=True)
    assert status == 200
    assert dashboard["dashboard"]["refresh"] == "250ms"

    def query(sql, *, ref_id="A"):
        return api("/api/ds/query", {"from": "0", "to": "2000000000000", "queries": [{
            "refId": ref_id, "datasource": {"type": "questdb-questdb-datasource", "uid": "questdb-native"},
            "rawSql": sql, "format": "table", "intervalMs": 250, "maxDataPoints": 1000,
        }]}, anonymous=True)

    status, result = query("SELECT count() FROM dashboard_depth")
    assert status == 200
    assert result["results"]["A"]["frames"]

    # Exercise datasource macros and array serialization, in addition to SQL
    # through the database HTTP interface in the companion integration test.
    status, result = query("SELECT venue_id,run_id,symbol FROM dashboard_depth ORDER BY published_at_ns DESC LIMIT 1")
    assert status == 200
    frame = result["results"]["A"]["frames"][0]
    selected = {field["name"]: values[0] for field, values in zip(frame["schema"]["fields"], frame["data"]["values"])}
    replacements = {f"${{{key}:sqlstring}}": "'"+str(value).replace("'", "''")+"'" for key, value in (
        ("venue", selected["venue_id"]), ("run", selected["run_id"]), ("symbol", selected["symbol"]),
    )}
    provisioned = json.loads((Path(__file__).resolve().parents[1]/"examples/dashboard/dashboards/mockx-native.json").read_text())
    for mode in ("Live", "Historical"):
        for panel in provisioned["panels"]:
            sql = panel["targets"][0]["rawSql"].replace("${mode:sqlstring}", f"'{mode}'")
            for variable, value in replacements.items():
                sql = sql.replace(variable, value)
            status, result = query(sql)
            assert status == 200, (panel["title"], result)
            assert result["results"]["A"]["frames"], panel["title"]

    status, result = query(f"CREATE TABLE mockx_readonly_probe_{uuid4().hex} (x LONG)")
    assert status >= 400
    assert "write permission denied" in json.dumps(result).lower()
