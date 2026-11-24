"""CLI 与输入校验测试：用 subprocess 在隔离模式下端到端跑 plan/verify。"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "archivecut_cli.py")
FIX = os.path.join(ROOT, "fixtures")


def run_cli(*args):
    proc = subprocess.run(
        [sys.executable, "-I", ENTRY, *args],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    return proc.returncode, proc.stdout, proc.stderr


class CliTest(unittest.TestCase):
    def test_plan_and_verify_feasible_fixture(self):
        path = os.path.join(FIX, "case_migration_proof.json")
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "result.json")
            code, stdout, stderr = run_cli("plan", path, "-o", out)
            self.assertEqual(code, 0, stderr)
            with open(out, encoding="utf-8") as fh:
                result = json.load(fh)
            self.assertEqual(result["status"], "feasible")
            self.assertIn("objectives", result)
            self.assertIn("batch_reports", result)
            vout = os.path.join(td, "verify.json")
            vcode, _, verr = run_cli("verify", path, out, "-o", vout)
            self.assertEqual(vcode, 0, verr)
            with open(vout, encoding="utf-8") as fh:
                report = json.load(fh)
            self.assertTrue(report["verified"])
            self.assertTrue(report["is_optimal"])

    def test_plan_infeasible_exit_code_and_relaxation(self):
        path = os.path.join(FIX, "case_infeasible_relax.json")
        code, stdout, _ = run_cli("plan", path)
        self.assertEqual(code, 1)
        result = json.loads(stdout)
        self.assertEqual(result["status"], "infeasible")
        effective = result["relaxation_remove_one_work_order"]["effective"]
        self.assertEqual([r["work_order"] for r in effective],
                         ["WO-RETIRE-Q"])

    def test_verify_detects_tampered_result(self):
        path = os.path.join(FIX, "case_demo_transient.json")
        with tempfile.TemporaryDirectory() as td:
            out = os.path.join(td, "result.json")
            code, _, _ = run_cli("plan", path, "-o", out)
            self.assertEqual(code, 0)
            with open(out, encoding="utf-8") as fh:
                result = json.load(fh)
            # 篡改总费用：verify 必须识别出不一致。
            result["objectives"]["total_cost"] += 100
            tampered = os.path.join(td, "tampered.json")
            with open(tampered, "w", encoding="utf-8") as fh:
                json.dump(result, fh, ensure_ascii=False)
            vcode, stdout, _ = run_cli("verify", path, tampered)
            self.assertEqual(vcode, 1)
            report = json.loads(stdout)
            self.assertFalse(report["verified"])

    def test_input_error_returns_code_2(self):
        with tempfile.TemporaryDirectory() as td:
            bad = os.path.join(td, "bad.json")
            with open(bad, "w", encoding="utf-8") as fh:
                json.dump({"case_id": "x"}, fh)  # 缺必填字段
            code, stdout, _ = run_cli("plan", bad)
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(stdout)["status"], "input_error")

    def test_output_is_utf8_with_chinese_case_id(self):
        path = os.path.join(FIX, "case_migration_proof.json")
        code, stdout, _ = run_cli("plan", path)
        self.assertEqual(code, 0)
        # stdout 字节必须能按 UTF-8 解码且 JSON 可解析。
        raw = stdout.encode("utf-8")
        json.loads(raw.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
