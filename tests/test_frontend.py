"""Runs the real dashboard JavaScript against a live server in a fake DOM (needs Node.js; skipped without it)."""
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
import app as A  # noqa: E402
import hospital_agents.auth as _auth  # noqa: E402

_auth.ITERATIONS = 2000   # tests only: keep sign-ins fast; production uses 200 000

NODE = shutil.which("node")


def api(port, path, body=None, token=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {token}"} if token else {})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


@unittest.skipUnless(NODE, "Node.js not installed")
class Dashboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.srv = A.make_server(ROOT / "data", 0, runtime=cls.tmp / "rt", policy_dir=cls.tmp / "pol")
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        A.Handler.app.auth.create_user("mgr", "dashboard-pw-1", "operations_manager")
        cls.token = api(cls.port, "/api/login", {"username": "mgr", "password": "dashboard-pw-1"})["token"]
        for u in ("ICU", "WARD"):
            api(cls.port, "/api/policies", {"unit": u, "value": 40}, cls.token)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        shutil.rmtree(cls.tmp)

    def test_scripts_are_syntactically_valid(self):
        for f in ("i18n.js", "charts.js", "app.js", "dom_smoke.js"):
            path = ROOT / ("tests" if f.startswith("dom") else "static") / f
            r = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_every_page_renders_in_both_languages_and_the_main_flows_work(self):
        r = subprocess.run([NODE, str(ROOT / "tests" / "dom_smoke.js"), str(self.port), self.token], capture_output=True, text=True, timeout=180)
        report = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertNotIn("crash", report, report.get("crash"))
        self.assertEqual(report["errors"], [])
        self.assertEqual(len(report["pages"]), 26)                                     # 13 pages x 2 languages
        for name, p in report["pages"].items():
            self.assertGreater(p["chars"], 80, name)
            self.assertEqual(p["junk"], 0, f"{name} shows undefined/NaN/[object Object]")
        for name in ("overview", "beds", "er", "staff", "or", "forecast"):
            self.assertGreaterEqual(report["pages"][f"en:{name}"]["svgs"], 2, name)         # real charts were drawn (the ER page has two)
        self.assertEqual(report["langToggle"], "ltr")
        self.assertGreaterEqual(report["kpiCards"], 8)
        self.assertEqual(report["twinZones"], 7)
        self.assertEqual(report["twinBeds"], 60)                                      # ER 12 + ICU 12 + ward 36, from the beds table
        self.assertTrue(report["twinFilter"])
        self.assertTrue(report["assistant"])
        self.assertTrue(report["llmTest"])
        self.assertTrue(report["agentsCard"])
        self.assertTrue(report["simulator"])
        self.assertGreaterEqual(report["decisionCards"], 1)
        self.assertTrue(report["drawerOpen"])
        self.assertTrue(report["approveEnabled"])
        self.assertTrue(report["appliedAfterApprove"])
        self.assertEqual(report["policyInputs"], 3)
        self.assertTrue(report["sourcesResults"])

    def test_every_ui_text_key_exists_in_both_languages(self):
        import re
        js = (ROOT / "static/app.js").read_text() + (ROOT / "dashboard.html").read_text()
        out = subprocess.run([NODE, "-e", "eval(require('fs').readFileSync('static/i18n.js','utf8').replace('const I18N','globalThis.I18N'));"
                              "console.log(JSON.stringify({en:Object.keys(I18N.en),ar:Object.keys(I18N.ar)}))"], cwd=ROOT, capture_output=True, text=True).stdout
        d = json.loads(out)
        keys = set(re.findall(r'\bt\("([a-z0-9_]+)"', js)) | set(re.findall(r'data-tp?="([a-z0-9_]+)"', js)) | {"applied_ok", "rejected_ok"}
        families = {k for k in keys if k.endswith("_")}                                # data-driven prefixes
        for lang in ("en", "ar"):
            self.assertEqual(sorted(k for k in keys - families if k not in d[lang]), [], lang)
        self.assertEqual(set(d["en"]), set(d["ar"]))


if __name__ == "__main__":
    unittest.main()
